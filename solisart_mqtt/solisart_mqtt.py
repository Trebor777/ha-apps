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
grandeur, un `binary_sensor` par relais (clés `rl*`). Le `device_class` et le
`state_class` sont déduits de l'unité renvoyée par le portail, ce qui donne
l'historique long terme sans configuration YAML.

Configuration par variables d'environnement :
    SOLISART_LOGIN / SOLISART_PASSWORD / SOLISART_ID   obligatoires
    SOLISART_INTERVAL      secondes entre deux relevés (défaut 120)
    SOLISART_CODES         plage de codes, ex. 584-628 (défaut) ou "all"
    SOLISART_NAMES         chemin d'un JSON {"flT1": "Température capteurs"}
    MQTT_HOST / MQTT_PORT / MQTT_USERNAME / MQTT_PASSWORD / MQTT_TLS
    MQTT_DISCOVERY_PREFIX  défaut "homeassistant"

Dans un add-on Home Assistant, les paramètres MQTT sont récupérés
automatiquement auprès du Supervisor : rien à saisir.

Dépendance : paho-mqtt (1.6 ou 2.x).
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
    """paho 2.x impose CallbackAPIVersion ; paho 1.6 ne le connaît pas."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    except AttributeError:  # paho-mqtt 1.x
        return mqtt.Client(client_id=client_id)


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

    def _on_connect(self, client, userdata, flags, rc, properties=None) -> None:
        if rc != 0:
            LOG.error("Connexion MQTT refusée (code %s)", rc)
            return
        LOG.info("Connecté à %s:%s", self.mqtt_cfg.host, self.mqtt_cfg.port)
        client.publish(self.avail_topic, "online", qos=1, retain=True)
        # Une reconnexion peut suivre un redémarrage du broker : on republie
        # la découverte, sinon les entités restent orphelines côté HA.
        self.published.clear()

    def _on_disconnect(self, client, userdata, rc, properties=None, reason=None) -> None:
        LOG.warning("Déconnecté du broker (code %s), reconnexion automatique", rc)

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
                payload.update({"payload_on": "on", "payload_off": "off"})
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

    def _publish_state(self, reading: Reading) -> None:
        payload = dict(reading.values)
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
        self._publish_state(reading)
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
