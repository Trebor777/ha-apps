#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
solisart_mqtt.py — démon de publication MQTT avec Home Assistant Discovery.

S'appuie sur solisart.py (même dossier) pour la partie portail MySolisArt,
lui-même dérivé du script V04 de J&J_Ian1515 (GPL, usage commercial exclu :
https://github.com/jean1492/SolisArt-HomeAssistant). Distribué sous GPL-2.0.
Interroge le portail toutes les N secondes et publie :

  solisart/<node>/state    JSON de tous les relevés   (retain)
  solisart/<node>/status   online / offline           (retain, LWT)
  homeassistant/<composant>/<node>/<clé>/config       (retain)

Les entités sont créées automatiquement dans Home Assistant : un `sensor` par
grandeur, un `binary_sensor` par relais (clés `rl*`), et pour chaque relais
deux compteurs : durée de marche cumulée (h) et nombre de démarrages. Le `device_class` et le
`state_class` sont déduits de l'unité renvoyée par le portail, ce qui donne
l'historique long terme sans configuration YAML.

Configuration par variables d'environnement :
    SOLISART_LOGIN / SOLISART_PASSWORD / SOLISART_ID   obligatoires
    SOLISART_INTERVAL      secondes entre deux relevés (défaut 120)
    SOLISART_CODES         plage de codes, ex. 584-628 (défaut) ou "all"
    SOLISART_NAMES         chemin d'un JSON {"flT1": "Température capteurs"}
    SOLISART_RUNTIME_KEYS  grandeurs non-relais à doter d'un compteur de marche,
                           séparées par des virgules (les relais `rl*` le sont d'office)
    MQTT_HOST / MQTT_PORT / MQTT_USERNAME / MQTT_PASSWORD / MQTT_TLS
    MQTT_DISCOVERY_PREFIX  défaut "homeassistant"

Dans un add-on Home Assistant, les paramètres MQTT sont récupérés
automatiquement auprès du Supervisor : rien à saisir.

Dépendance : paho-mqtt 2.x.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solisart import Cache, Config, Reading, SolisartClient, SolisartError, parse_values

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    sys.exit("paho-mqtt manquant : pip install paho-mqtt")

LOG = logging.getLogger("solisart.mqtt")

# unité brute → (device_class, unité normalisée, state_class)
UNIT_MAP: dict[str, tuple[str | None, str, str | None]] = {
    "C": ("temperature", "°C", "measurement"),
    "°C": ("temperature", "°C", "measurement"),
    "K": ("temperature", "K", "measurement"),
    "%": (None, "%", "measurement"),
    "W": ("power", "W", "measurement"),
    "kW": ("power", "kW", "measurement"),
    "Wh": ("energy", "Wh", "total_increasing"),
    "kWh": ("energy", "kWh", "total_increasing"),
    "bar": ("pressure", "bar", "measurement"),
    "l": ("volume", "L", "measurement"),
    "L": ("volume", "L", "measurement"),
    "l/min": ("volume_flow_rate", "L/min", "measurement"),
    "L/min": ("volume_flow_rate", "L/min", "measurement"),
    "l/h": ("volume_flow_rate", "L/h", "measurement"),
    "m3": ("volume", "m³", "total_increasing"),
    "m³": ("volume", "m³", "total_increasing"),
    "min": ("duration", "min", "measurement"),
    "h": ("duration", "h", "measurement"),
    "d": ("duration", "d", "measurement"),
    "j": ("duration", "d", "measurement"),
}


# --------------------------------------------------------------------------- #
# Compteurs de marche
# --------------------------------------------------------------------------- #

ON_TEXT = {"on", "1", "true", "marche"}


def is_on(value: Any) -> bool:
    """Relais "on"/"off", ou grandeur numérique : en marche dès qu'elle est > 0."""
    if isinstance(value, (int, float)):
        return value > 0
    return str(value).strip().lower() in ON_TEXT


class RunCounters:
    """Durée de marche cumulée et nombre de démarrages, par grandeur suivie.

    Les compteurs ne font que croître (`total_increasing`) : Home Assistant en
    tire des statistiques long terme, donc des heures de brûleur par jour, par
    semaine ou par saison, conservées sans limite de durée.

    Entre deux relevés, on suppose que l'état n'a pas changé. Si l'écart dépasse
    `max_gap` (portail injoignable, add-on arrêté), l'intervalle n'est pas
    compté : mieux vaut un trou qu'une durée de marche inventée.
    """

    def __init__(self, cache: Cache, max_gap: float) -> None:
        self.cache = cache
        self.max_gap = max_gap
        stored = cache.read("counters")
        self.data: dict[str, dict[str, Any]] = stored if isinstance(stored, dict) else {}

    def update(self, key: str, value: Any, now: float) -> None:
        on = is_on(value)
        entry = self.data.get(key)
        if entry is None:
            # Premier relevé : pas de démarrage compté, l'état d'avant est inconnu.
            self.data[key] = {"on_s": 0.0, "starts": 0, "on": on, "ts": now}
            return
        gap = now - entry["ts"]
        if entry["on"] and 0 < gap <= self.max_gap:
            entry["on_s"] += gap
        if on and not entry["on"] and gap <= self.max_gap:
            entry["starts"] += 1
        entry["on"] = on
        entry["ts"] = now

    def hours(self, key: str) -> float:
        return round(self.data[key]["on_s"] / 3600, 3)

    def starts(self, key: str) -> int:
        return int(self.data[key]["starts"])

    def save(self) -> None:
        self.cache.write("counters", self.data)


# --------------------------------------------------------------------------- #
# Paramètres MQTT
# --------------------------------------------------------------------------- #


class MqttSettings:
    def __init__(self) -> None:
        supervisor = self._from_supervisor()
        self.host = os.environ.get("MQTT_HOST") or supervisor.get("host") or "core-mosquitto"
        self.port = int(os.environ.get("MQTT_PORT") or supervisor.get("port") or 1883)
        self.username = os.environ.get("MQTT_USERNAME") or supervisor.get("username") or ""
        self.password = os.environ.get("MQTT_PASSWORD") or supervisor.get("password") or ""
        tls_env = os.environ.get("MQTT_TLS")
        self.tls = (tls_env.lower() in ("1", "true", "yes")) if tls_env else bool(supervisor.get("ssl"))
        self.discovery_prefix = os.environ.get("MQTT_DISCOVERY_PREFIX", "homeassistant")

    @staticmethod
    def _from_supervisor() -> dict[str, Any]:
        """Dans un add-on, le Supervisor fournit les identifiants du broker."""
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return {}
        req = urllib.request.Request(
            "http://supervisor/services/mqtt",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read()).get("data", {})
            LOG.info("Broker MQTT fourni par le Supervisor : %s", data.get("host"))
            return data
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Service MQTT du Supervisor indisponible : %s", exc)
            return {}


def _make_client(client_id: str) -> "mqtt.Client":
    """API de callbacks v2 (la v1 est dépréciée depuis paho-mqtt 2.0)."""
    if not hasattr(mqtt, "CallbackAPIVersion"):
        sys.exit("paho-mqtt 2.x requis : pip install 'paho-mqtt>=2.0'")
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)


# --------------------------------------------------------------------------- #
# Passerelle
# --------------------------------------------------------------------------- #


class Bridge:
    def __init__(self, cfg: Config, mqtt_cfg: MqttSettings, interval: int, wanted: range | None):
        self.cfg = cfg
        self.mqtt_cfg = mqtt_cfg
        self.interval = interval
        self.wanted = wanted
        self.cache = Cache(cfg.cache_dir)
        self.client_api = SolisartClient(cfg, self.cache)

        self.node = self._node_id(cfg.installation_id)
        self.base = f"solisart/{self.node}"
        self.state_topic = f"{self.base}/state"
        self.avail_topic = f"{self.base}/status"

        self.friendly = self._load_names()
        # Relais (`rl*`) suivis d'office ; SOLISART_RUNTIME_KEYS ajoute d'autres
        # grandeurs, par exemple une consigne de circulateur en %.
        extra = os.environ.get("SOLISART_RUNTIME_KEYS", "")
        # bashio renvoie "null" pour une option vide.
        self.runtime_extra = {k.strip() for k in extra.split(",") if k.strip() not in ("", "null")}
        # Trois intervalles sans relevé : on considère qu'il y a eu une coupure.
        self.counters = RunCounters(self.cache, max_gap=3 * interval)
        self.published: set[str] = set()
        self.failures = 0
        self.stop = threading.Event()

        self.mqtt = _make_client(f"solisart-{self.node}")
        if mqtt_cfg.username:
            self.mqtt.username_pw_set(mqtt_cfg.username, mqtt_cfg.password)
        if mqtt_cfg.tls:
            self.mqtt.tls_set()
        self.mqtt.will_set(self.avail_topic, "offline", qos=1, retain=True)
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_disconnect = self._on_disconnect
        self.mqtt.reconnect_delay_set(min_delay=1, max_delay=120)

    # -- utilitaires ---------------------------------------------------------- #

    @staticmethod
    def _node_id(installation_id: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "", installation_id).lower()
        return slug or "default"

    @staticmethod
    def _load_names() -> dict[str, str]:
        path = os.environ.get("SOLISART_NAMES")
        if path and Path(path).is_file():
            try:
                return json.loads(Path(path).read_text(encoding="utf-8"))
            except ValueError as exc:
                LOG.warning("Fichier de noms illisible : %s", exc)
        return {}

    @property
    def device(self) -> dict[str, Any]:
        return {
            "identifiers": [f"solisart_{self.node}"],
            "name": "SolisArt",
            "manufacturer": "SolisArt",
            "model": "Régulation solaire",
            "configuration_url": self.cfg.domain,
        }

    # -- MQTT ----------------------------------------------------------------- #

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            LOG.error("Connexion MQTT refusée (%s)", reason_code)
            return
        LOG.info("Connecté à %s:%s", self.mqtt_cfg.host, self.mqtt_cfg.port)
        client.publish(self.avail_topic, "online", qos=1, retain=True)
        # Une reconnexion peut suivre un redémarrage du broker : on republie
        # la découverte, sinon les entités restent orphelines côté HA.
        self.published.clear()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        LOG.warning("Déconnecté du broker (%s), reconnexion automatique", reason_code)

    def _publish_discovery(self, reading: Reading) -> None:
        for key, value in reading.values.items():
            if key in self.published:
                continue
            component = "binary_sensor" if key.startswith("rl") else "sensor"
            payload: dict[str, Any] = {
                "name": self.friendly.get(key, key),
                "unique_id": f"solisart_{self.node}_{key}",
                "object_id": f"solisart_{key}",
                "state_topic": self.state_topic,
                "value_template": "{{ value_json['%s'] }}" % key,
                "availability_topic": self.avail_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": self.device,
            }
            if component == "binary_sensor":
                # Le portail renvoie "On"/"Off" : on normalise la casse, sans quoi
                # Home Assistant ne reconnaît aucun des deux états (`unknown`).
                payload.update({
                    "value_template": "{{ value_json['%s'] | string | lower }}" % key,
                    "payload_on": "on",
                    "payload_off": "off",
                })
            else:
                unit = reading.units.get(key)
                device_class, unit_norm, state_class = UNIT_MAP.get(
                    unit, (None, unit, "measurement" if isinstance(value, float) else None)
                )
                if unit_norm:
                    payload["unit_of_measurement"] = unit_norm
                if device_class:
                    payload["device_class"] = device_class
                if state_class and isinstance(value, float):
                    payload["state_class"] = state_class

            topic = f"{self.mqtt_cfg.discovery_prefix}/{component}/{self.node}/{key}/config"
            self.mqtt.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=True)
            self.published.add(key)

        if "LastUpdate" not in self.published:
            topic = f"{self.mqtt_cfg.discovery_prefix}/sensor/{self.node}/last_update/config"
            self.mqtt.publish(
                topic,
                json.dumps(
                    {
                        "name": "Dernière mise à jour",
                        "unique_id": f"solisart_{self.node}_last_update",
                        "object_id": "solisart_last_update",
                        "state_topic": self.state_topic,
                        "value_template": "{{ value_json['LastUpdate'] }}",
                        "device_class": "timestamp",
                        "entity_category": "diagnostic",
                        "availability_topic": self.avail_topic,
                        "payload_available": "online",
                        "payload_not_available": "offline",
                        "device": self.device,
                    },
                    ensure_ascii=False,
                ),
                qos=1,
                retain=True,
            )
            self.published.add("LastUpdate")

        for key in self._tracked(reading):
            label = self.friendly.get(key, key)
            for suffix, extra in (
                ("runtime", {"name": f"{label} — durée de marche", "unit_of_measurement": "h",
                             "device_class": "duration", "icon": "mdi:timer-outline"}),
                ("starts", {"name": f"{label} — démarrages", "icon": "mdi:counter"}),
            ):
                field_key = f"{key}_{suffix}"
                if field_key in self.published:
                    continue
                payload = {
                    "unique_id": f"solisart_{self.node}_{field_key}",
                    "object_id": f"solisart_{field_key}",
                    "state_topic": self.state_topic,
                    "value_template": "{{ value_json['%s'] }}" % field_key,
                    "state_class": "total_increasing",
                    "availability_topic": self.avail_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "device": self.device,
                    **extra,
                }
                topic = f"{self.mqtt_cfg.discovery_prefix}/sensor/{self.node}/{field_key}/config"
                self.mqtt.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=True)
                self.published.add(field_key)

    def _tracked(self, reading: Reading) -> list[str]:
        """Grandeurs dotées d'un compteur : tous les relais, plus les clés demandées."""
        keys = [k for k in reading.values if k.startswith("rl") or k in self.runtime_extra]
        missing = self.runtime_extra - set(reading.values)
        if missing:
            LOG.debug("SOLISART_RUNTIME_KEYS absentes du relevé : %s", ", ".join(sorted(missing)))
        return keys

    def _update_counters(self, reading: Reading) -> dict[str, Any]:
        now = time.time()
        fields: dict[str, Any] = {}
        for key in self._tracked(reading):
            self.counters.update(key, reading.values[key], now)
            fields[f"{key}_runtime"] = self.counters.hours(key)
            fields[f"{key}_starts"] = self.counters.starts(key)
        self.counters.save()
        return fields

    def _publish_state(self, reading: Reading, extra: dict[str, Any] | None = None) -> None:
        payload = dict(reading.values)
        payload.update(extra or {})
        payload["LastUpdate"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        if reading.newest_hour and reading.newest_hour > 1_000_000_000:
            payload["MeasureTime"] = datetime.fromtimestamp(
                reading.newest_hour, tz=timezone.utc
            ).astimezone().isoformat(timespec="seconds")
        self.mqtt.publish(self.state_topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=True)
        LOG.debug("État publié : %d grandeurs", len(reading.values))

    # -- boucle ---------------------------------------------------------------- #

    def _poll_once(self) -> None:
        dictionary = self.client_api.data_dictionary()
        reading = parse_values(self.client_api.raw_values(), dictionary, self.wanted)
        if not reading:
            raise SolisartError("Aucune donnée dans la plage de codes demandée")
        self._publish_discovery(reading)
        self._publish_state(reading, self._update_counters(reading))
        self.cache.write("last", reading.values)
        if self.failures:
            LOG.info("Connexion au portail rétablie")
            self.mqtt.publish(self.avail_topic, "online", qos=1, retain=True)
        self.failures = 0

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)

        self.mqtt.connect_async(self.mqtt_cfg.host, self.mqtt_cfg.port, keepalive=60)
        self.mqtt.loop_start()

        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self._poll_once()
            except Exception as exc:  # noqa: BLE001 — le démon ne doit jamais mourir
                self.failures += 1
                LOG.error("Relevé %d en échec : %s", self.failures, exc)
                # Deux échecs d'affilée : on bascule les entités en `unavailable`
                # plutôt que de laisser HA afficher des valeurs figées.
                if self.failures >= 2:
                    self.mqtt.publish(self.avail_topic, "offline", qos=1, retain=True)
            delay = max(5.0, self.interval - (time.monotonic() - started))
            self.stop.wait(delay)

        LOG.info("Arrêt demandé")
        self.mqtt.publish(self.avail_topic, "offline", qos=1, retain=True)
        self.mqtt.loop_stop()
        self.mqtt.disconnect()
        return 0

    def _signal(self, signum, frame) -> None:  # noqa: ARG002
        self.stop.set()


def main() -> int:
    logging.basicConfig(
        stream=sys.stdout,
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        cfg = Config.load()
    except SolisartError as exc:
        LOG.error("%s", exc)
        return 1

    codes = os.environ.get("SOLISART_CODES", "")
    wanted = None if codes.lower() == "all" else range(cfg.code_min, cfg.code_max + 1)
    interval = max(30, int(os.environ.get("SOLISART_INTERVAL", "120")))

    LOG.info("Démarrage : relevé toutes les %d s, codes %s",
             interval, "tous" if wanted is None else f"{cfg.code_min}-{cfg.code_max}")
    return Bridge(cfg, MqttSettings(), interval, wanted).run()


if __name__ == "__main__":
    sys.exit(main())
