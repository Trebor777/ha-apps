#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
solisart.py — passerelle MySolisArt → Home Assistant (V05)

Dérivé du script V04 de J&J_Ian1515 (GPL, usage commercial exclu) :
https://github.com/jean1492/SolisArt-HomeAssistant — distribué sous GPL-2.0.
Même contrat de sortie : un objet JSON sur stdout, consommable par un
`command_line` sensor.

Ce qui change par rapport à V04 :
  - timeouts réseau et retries (un blocage réseau ne gèle plus le sensor) ;
  - session HTTP mise en cache sur disque : on ne se relogue plus à chaque scan ;
  - dictionnaire code→nom découvert dynamiquement (l'URL du JS est versionnée
    côté Solisart et finit toujours par changer) + cache disque 7 jours ;
  - dernier relevé conservé en cache : en cas de panne on renvoie les valeurs
    précédentes marquées `stale`, au lieu d'un objet vide ;
  - erreurs typées et journalisées sur stderr, jamais sur stdout ;
  - identifiants hors du code (variables d'environnement ou fichier secrets) ;
  - correction du nettoyage des valeurs (`"on"` devenait `"o"` en V04) ;
  - déduplication : pour un même code, on garde l'échantillon le plus récent.

Bibliothèque standard uniquement : tourne tel quel dans le conteneur Home
Assistant (HAOS / Container / Supervised), sans pip install.

Usage :
    python3 solisart.py                 # JSON compact sur stdout
    python3 solisart.py --pretty        # JSON indenté (mise au point)
    python3 solisart.py --all           # tous les codes, pas seulement 584-628
    python3 solisart.py --list-dict     # dictionnaire code → nom
    python3 solisart.py --debug         # journal détaillé sur stderr
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger("solisart")

DOMAIN = "https://my.solisart.fr"
URL_DATA = "/admin/divers/ajax/lecture_valeurs_donnees.php"
URL_DICT_FALLBACK = "/admin/divers/js/solisart/commun-donnees.1702435180.js"
DICT_PATTERN = re.compile(r"/admin/divers/js/solisart/commun-donnees\.\d+\.js")
DEF_PATTERN = re.compile(r"const\s+donnee_(\w+)\s*=\s*(\d+)\s*;")
NUMERIC_PATTERN = re.compile(r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*([^\d]*?)\s*$")

USER_AGENT = "solisart-ha/5.0 (+https://github.com/jean1492/SolisArt-HomeAssistant)"


class SolisartError(RuntimeError):
    """Erreur métier : identifiants, session, ou réponse inexploitable."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    login: str
    password: str
    installation_id: str
    domain: str = DOMAIN
    code_min: int = 584
    code_max: int = 628
    timeout: float = 15.0
    retries: int = 2
    session_ttl: int = 1800          # 30 min
    dict_ttl: int = 7 * 24 * 3600    # 7 jours
    cache_dir: Path = field(default_factory=lambda: _default_cache_dir())

    @classmethod
    def load(cls) -> "Config":
        """Identifiants depuis l'environnement, sinon depuis un fichier JSON.

        Ordre de recherche du fichier :
          $SOLISART_SECRETS, puis ./solisart_secrets.json à côté du script.
        Format attendu : {"login": "...", "password": "...", "id": "..."}
        """
        raw: dict[str, Any] = {}
        candidates = []
        env_path = os.environ.get("SOLISART_SECRETS")
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(Path(__file__).resolve().parent / "solisart_secrets.json")

        for path in candidates:
            if path.is_file():
                LOG.debug("Secrets lus depuis %s", path)
                raw = json.loads(path.read_text(encoding="utf-8"))
                break

        login = os.environ.get("SOLISART_LOGIN") or raw.get("login", "")
        password = os.environ.get("SOLISART_PASSWORD") or raw.get("password", "")
        inst = os.environ.get("SOLISART_ID") or raw.get("id", "")

        if not (login and password and inst):
            raise SolisartError(
                "Identifiants manquants. Définir SOLISART_LOGIN / SOLISART_PASSWORD "
                "/ SOLISART_ID, ou créer solisart_secrets.json à côté du script."
            )

        cfg = cls(login=login, password=password, installation_id=inst)

        if raw.get("domain"):
            cfg.domain = raw["domain"]
        codes = os.environ.get("SOLISART_CODES") or raw.get("codes")
        if codes:
            lo, _, hi = str(codes).partition("-")
            cfg.code_min, cfg.code_max = int(lo), int(hi or lo)
        cache = os.environ.get("SOLISART_CACHE") or raw.get("cache_dir")
        if cache:
            cfg.cache_dir = Path(cache)
        return cfg


def _default_cache_dir() -> Path:
    """/config/.storage sur HAOS, sinon à côté du script, sinon /tmp."""
    for candidate in (Path("/config"), Path(__file__).resolve().parent, Path("/tmp")):
        try:
            target = candidate / ".solisart_cache"
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".w"
            probe.touch()
            probe.unlink()
            return target
        except OSError:
            continue
    return Path("/tmp")


# --------------------------------------------------------------------------- #
# Cache disque
# --------------------------------------------------------------------------- #


class Cache:
    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def read(self, name: str, ttl: int | None = None) -> Any | None:
        path = self._path(name)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if ttl is not None and time.time() - payload.get("ts", 0) > ttl:
            LOG.debug("Cache %s périmé", name)
            return None
        return payload.get("data")

    def read_any_age(self, name: str) -> tuple[Any | None, float]:
        """Renvoie (donnée, âge en secondes) sans vérifier le TTL."""
        path = self._path(name)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, 0.0
        return payload.get("data"), max(0.0, time.time() - payload.get("ts", 0))

    def write(self, name: str, data: Any) -> None:
        path = self._path(name)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps({"ts": time.time(), "data": data}), encoding="utf-8"
            )
            tmp.replace(path)          # écriture atomique
            path.chmod(0o600)
        except OSError as exc:
            LOG.warning("Écriture du cache %s impossible : %s", name, exc)


# --------------------------------------------------------------------------- #
# Client HTTP
# --------------------------------------------------------------------------- #


class SolisartClient:
    def __init__(self, cfg: Config, cache: Cache) -> None:
        self.cfg = cfg
        self.cache = cache
        self._cookie: str | None = None

    # -- bas niveau ---------------------------------------------------------- #

    def _request(
        self, path: str, data: dict[str, Any] | None = None, cookie: str | None = None
    ) -> bytes:
        url = f"{self.cfg.domain}{path}"
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
        req.add_header("User-Agent", USER_AGENT)
        if body:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if cookie:
            req.add_header("Cookie", cookie)
        LOG.debug("%s %s", req.get_method(), url)
        with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
            return resp.read()

    # -- session ------------------------------------------------------------- #

    def _login(self) -> str:
        payload = {
            "id": self.cfg.login,
            "pass": self.cfg.password,
            "ihm": "admin",
            "connexion": "Se Connecter",
        }
        body = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(f"{self.cfg.domain}/", data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("User-Agent", USER_AGENT)

        with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
            set_cookie = resp.getheader("Set-Cookie")
        if not set_cookie:
            raise SolisartError("Pas de cookie de session : identifiants refusés ?")
        cookie = set_cookie.split(";")[0]

        # Le portail exige un second POST porteur du cookie pour valider la session.
        req.add_header("Cookie", cookie)
        with urllib.request.urlopen(req, timeout=self.cfg.timeout):
            pass

        LOG.debug("Nouvelle session obtenue")
        self.cache.write("session", cookie)
        return cookie

    def _session(self, force_new: bool = False) -> str:
        if force_new:
            self._cookie = self._login()
        elif self._cookie is None:
            cached = self.cache.read("session", ttl=self.cfg.session_ttl)
            self._cookie = cached if isinstance(cached, str) else self._login()
        return self._cookie

    # -- dictionnaire code → nom --------------------------------------------- #

    def data_dictionary(self) -> dict[int, str]:
        cached = self.cache.read("dictionary", ttl=self.cfg.dict_ttl)
        if isinstance(cached, dict) and cached:
            return {int(k): v for k, v in cached.items()}

        try:
            mapping = self._fetch_dictionary()
            self.cache.write("dictionary", {str(k): v for k, v in mapping.items()})
            return mapping
        except (urllib.error.URLError, SolisartError, OSError) as exc:
            stale, age = self.cache.read_any_age("dictionary")
            if isinstance(stale, dict) and stale:
                LOG.warning(
                    "Dictionnaire non rafraîchi (%s), réutilisation du cache (%.0f h)",
                    exc, age / 3600,
                )
                return {int(k): v for k, v in stale.items()}
            raise

    def _fetch_dictionary(self) -> dict[int, str]:
        """Le nom du JS embarque un cache-buster : on le retrouve dans la page."""
        url_dict = URL_DICT_FALLBACK
        try:
            home = self._request("/").decode("utf-8", "replace")
            found = DICT_PATTERN.search(home)
            if found:
                url_dict = found.group(0)
                LOG.debug("Dictionnaire découvert : %s", url_dict)
        except (urllib.error.URLError, OSError) as exc:
            LOG.debug("Découverte impossible (%s), URL de repli", exc)

        raw = self._request(url_dict).decode("utf-8", "replace")
        mapping = {int(code): _dict_name(name) for name, code in DEF_PATTERN.findall(raw)}
        if not mapping:
            raise SolisartError(f"Aucune définition trouvée dans {url_dict}")
        LOG.debug("%d entrées de dictionnaire", len(mapping))
        return mapping

    # -- relevés -------------------------------------------------------------- #

    def raw_values(self) -> bytes:
        payload = {
            "id": base64.b64encode(self.cfg.installation_id.encode()).decode(),
            "heure": 0,
            "periode": 5,
        }
        last_exc: Exception | None = None
        for attempt in range(self.cfg.retries + 1):
            try:
                data = self._request(URL_DATA, payload, self._session(force_new=attempt > 0))
                if b"<" not in data[:200]:
                    # Réponse non XML : session expirée côté serveur.
                    raise SolisartError("Réponse non XML (session expirée ?)")
                return data
            except (urllib.error.URLError, SolisartError, OSError) as exc:
                last_exc = exc
                LOG.warning("Tentative %d/%d échouée : %s",
                            attempt + 1, self.cfg.retries + 1, exc)
                if attempt < self.cfg.retries:
                    time.sleep(2 ** attempt)
        raise SolisartError(f"Lecture impossible après {self.cfg.retries + 1} essais: {last_exc}")


# --------------------------------------------------------------------------- #
# Transformation
# --------------------------------------------------------------------------- #


def _dict_name(raw: str) -> str:
    """`fl_T1` → `flT1`, pour rester compatible avec les noms produits par V04."""
    return "".join(raw.split("_"))


# Écritures rencontrées pour une même unité. `dC` est la notation SolisArt du
# degré Celsius — ce n'est pas un décidegré : la valeur est déjà à l'échelle.
UNIT_ALIASES = {
    "C": "°C",
    "dC": "°C",
    "degC": "°C",
    "°c": "°C",
    "\ufffdC": "°C",   # ° mal décodé selon l'encodage renvoyé
}

# Unités rendues telles quelles, sans conversion.
KNOWN_UNITS = {
    "°C", "K", "%", "W", "kW", "Wh", "kWh", "bar", "l", "L",
    "l/min", "L/min", "l/h", "m3", "m³", "min", "h", "d", "j", "s",
}

# Préfixes SI, appliqués uniquement si SOLISART_SCALE_SI=1. Désactivé par
# défaut : sur cette installation le portail renvoie déjà des valeurs à
# l'échelle, et une conversion non désirée fausserait tout l'historique.
SI_PREFIXES = {
    "da": 10.0, "h": 100.0, "k": 1e3, "M": 1e6,
    "d": 0.1, "c": 0.01, "m": 1e-3, "µ": 1e-6, "u": 1e-6, "n": 1e-9, "p": 1e-12,
}


def normalise(value: float, unit: str) -> tuple[float, str]:
    """Uniformise l'écriture de l'unité, sans toucher à la valeur par défaut."""
    unit = UNIT_ALIASES.get(unit, unit)
    if unit in KNOWN_UNITS or not os.environ.get("SOLISART_SCALE_SI"):
        return value, unit
    for prefix, factor in sorted(SI_PREFIXES.items(), key=lambda kv: -len(kv[0])):
        base = unit[len(prefix):]
        if unit.startswith(prefix) and UNIT_ALIASES.get(base, base) in KNOWN_UNITS:
            return round(value * factor, 6), UNIT_ALIASES.get(base, base)
    return value, unit


def clean(key: str, value: str) -> tuple[Any, str | None]:
    """Sépare la valeur de son unité. Renvoie (valeur, unité ou None).

    V04 faisait `value.rstrip(" Cdlmnp")`, ce qui rognait aussi les lettres
    finales des valeurs textuelles : `"on"` devenait `"o"`. On sépare ici le
    nombre de son unité par expression régulière, sans toucher au texte.
    L'unité est conservée : c'est elle qui permet de déduire `device_class`
    et `state_class` lors de la découverte MQTT.
    """
    value = value.strip()
    if key.startswith("rl"):          # relais : "on" / "off", laissé tel quel
        return value, None
    match = NUMERIC_PATTERN.match(value)
    if match:
        try:
            number = float(match.group(1).replace(",", "."))
        except ValueError:
            return value, None
        unit = match.group(2).strip()
        if unit:
            number, unit = normalise(number, unit)
        return number, unit or None
    return value, None


@dataclass
class Reading:
    """Un relevé complet : valeurs, unités détectées, horodatage source."""

    values: dict[str, Any] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    newest_hour: int | None = None

    def __bool__(self) -> bool:
        return bool(self.values)


def parse_values(
    xml_data: bytes, dictionary: dict[int, str], wanted: range | None
) -> Reading:
    """Extrait les relevés, en gardant l'échantillon le plus récent par code."""
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        raise SolisartError(f"XML illisible : {exc}") from exc

    latest: dict[int, tuple[int, str]] = {}
    seen: set[int] = set()
    # root.iter() descend dans tout l'arbre : les mesures ne sont pas
    # nécessairement des enfants directs de la racine.
    for node in root.iter():
        if "donnee" not in node.attrib:
            continue
        try:
            code = int(node.attrib["donnee"])
            hour = int(node.attrib.get("heure", 0))
        except ValueError:
            continue
        seen.add(code)
        if wanted is not None and code not in wanted:
            continue
        if code not in latest or hour > latest[code][0]:
            latest[code] = (hour, node.attrib.get("valeur", ""))

    if not latest:
        if seen:
            LOG.warning(
                "%d codes reçus (%d…%d) mais aucun dans la plage demandée : %s",
                len(seen), min(seen), max(seen),
                ",".join(str(c) for c in sorted(seen)[:20]),
            )
        else:
            LOG.warning(
                "Aucun attribut 'donnee' dans la réponse. Racine <%s>, %d nœuds. "
                "Vérifier l'identifiant d'installation.",
                root.tag, sum(1 for _ in root.iter()),
            )

    reading = Reading(newest_hour=max((h for h, _ in latest.values()), default=None))
    for code, (_, encoded) in sorted(latest.items()):
        name = dictionary.get(code, f"code{code}").strip()
        try:
            decoded = base64.b64decode(encoded.encode()).decode("utf-8", "replace")
        except ValueError:
            LOG.debug("Valeur non décodable pour le code %d", code)
            continue
        value, unit = clean(name, decoded)
        reading.values[name] = value
        if unit:
            reading.units[name] = unit
    return reading


# --------------------------------------------------------------------------- #
# Sortie
# --------------------------------------------------------------------------- #


def build_payload(values: dict[str, Any], newest_hour: int | None) -> dict[str, Any]:
    payload: dict[str, Any] = dict(values)
    payload["LastUpdate"] = time.strftime("%Y-%m-%d %H:%M:%S")
    payload["available"] = True
    payload["stale"] = False
    payload["age_seconds"] = 0
    if newest_hour and newest_hour > 1_000_000_000:
        payload["MeasureTime"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(newest_hour)
        )
    return payload


def fallback_payload(cache: Cache, error: str) -> dict[str, Any]:
    """En cas d'échec, on republie le dernier relevé connu, marqué `stale`.

    Les entités gardent ainsi leur dernière valeur pour l'historique ; c'est
    l'attribut `available` qui doit piloter `availability_template` côté HA.
    """
    previous, age = cache.read_any_age("last")
    payload: dict[str, Any] = dict(previous) if isinstance(previous, dict) else {}
    payload["available"] = False
    payload["stale"] = True
    payload["age_seconds"] = round(age)
    payload["error"] = error
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Passerelle MySolisArt → Home Assistant")
    parser.add_argument("--pretty", action="store_true", help="JSON indenté")
    parser.add_argument("--all", action="store_true", help="tous les codes disponibles")
    parser.add_argument("--list-dict", action="store_true", help="affiche code → nom")
    parser.add_argument("--debug", action="store_true", help="journal détaillé (stderr)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cache: Cache | None = None
    try:
        cfg = Config.load()
        cache = Cache(cfg.cache_dir)
        client = SolisartClient(cfg, cache)
        dictionary = client.data_dictionary()

        if args.list_dict:
            for code, name in sorted(dictionary.items()):
                print(f"{code}\t{name}")
            return 0

        wanted = None if args.all else range(cfg.code_min, cfg.code_max + 1)
        reading = parse_values(client.raw_values(), dictionary, wanted)
        if not reading:
            raise SolisartError("Aucune donnée dans la plage de codes demandée")

        payload = build_payload(reading.values, reading.newest_hour)
        cache.write("last", reading.values)
        print(json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=False))
        return 0

    except Exception as exc:  # noqa: BLE001 — stdout doit rester du JSON valide
        LOG.error("Échec : %s", exc, exc_info=args.debug)
        payload = (
            fallback_payload(cache, str(exc))
            if cache
            else {"available": False, "stale": True, "error": str(exc)}
        )
        print(json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
