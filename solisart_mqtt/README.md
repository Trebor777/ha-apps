# SolisArt → MQTT

Passerelle entre le portail MySolisArt et Home Assistant, via MQTT Discovery.
Aucune configuration YAML côté Home Assistant : les entités apparaissent seules
sous un appareil « SolisArt ».

## Option A — app Home Assistant OS (recommandé)

Ajouter le dépôt `https://github.com/Trebor777/ha-apps` dans **Paramètres → Apps →
Boutique → ⋮ → Dépôts**, puis installer « SolisArt MQTT ».

## Option A bis — app locale (Home Assistant OS)

1. Copier ce dossier dans le partage `addons` de la machine :

   ```
   /addons/solisart_mqtt/
       config.yaml
       Dockerfile
       run.sh
       solisart.py
       solisart_mqtt.py
   ```

   L'accès au partage se fait par Samba, par l'add-on « Advanced SSH & Web
   Terminal », ou par « Studio Code Server ».

2. **Paramètres → Modules complémentaires → Boutique**, menu ⋮ → **Vérifier les
   mises à jour**. L'add-on apparaît dans la section « Local add-ons ».

3. Installer, renseigner login / mot de passe / identifiant d'installation,
   démarrer. Activer « Démarrer au démarrage » et « Surveillance ».

Le broker est détecté automatiquement auprès du Supervisor : rien à saisir tant
que Mosquitto tourne en add-on. Les identifiants MySolisArt restent dans la
configuration de l'add-on, jamais dans le code.

## Option B — service systemd (Home Assistant Container, LXC, autre machine)

```bash
sudo install -d /opt/solisart
sudo install -m 644 solisart.py solisart_mqtt.py /opt/solisart/
sudo pip3 install paho-mqtt
sudo install -d -m 700 /var/lib/solisart
```

`/etc/solisart.env`, en `chmod 600` :

```ini
SOLISART_LOGIN=...
SOLISART_PASSWORD=...
SOLISART_ID=...
SOLISART_INTERVAL=120
SOLISART_CACHE=/var/lib/solisart
MQTT_HOST=192.168.1.10
MQTT_USERNAME=...
MQTT_PASSWORD=...
```

`/etc/systemd/system/solisart.service` :

```ini
[Unit]
Description=Passerelle SolisArt vers MQTT
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/solisart.env
ExecStart=/usr/bin/python3 /opt/solisart/solisart_mqtt.py
Restart=always
RestartSec=30
DynamicUser=yes
StateDirectory=solisart
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now solisart
journalctl -u solisart -f
```

## Topics publiés

| Topic | Contenu |
|---|---|
| `solisart/<node>/state` | JSON de toutes les grandeurs, retenu |
| `solisart/<node>/status` | `online` / `offline`, retenu, LWT |
| `homeassistant/sensor/<node>/<clé>/config` | découverte, retenue |

`<node>` dérive de l'identifiant d'installation.

## Mise au point

Découvrir les codes disponibles avant de figer la plage :

```bash
SOLISART_LOGIN=... SOLISART_PASSWORD=... SOLISART_ID=... \
  python3 solisart.py --all --pretty
python3 solisart.py --list-dict
```

Le script en une passe (`solisart.py`) reste utilisable seul, en `command_line`
sensor, si le mode démon n'est pas souhaité.

## Entités

Les noms viennent du portail (`flT1`, `rlV3`…). Deux façons de les rendre
lisibles :

- renommer directement dans l'interface Home Assistant — les `unique_id` sont
  stables, le renommage survit aux redémarrages ;
- ou créer `/data/names.json` (add-on) / `$SOLISART_NAMES` (systemd) :
  `{"flT1": "Température capteurs", "rlV3": "Vanne 3 voies"}`.

Les températures reçoivent `device_class: temperature` et
`state_class: measurement`, les compteurs d'énergie `total_increasing` : les
statistiques long terme et le tableau de bord Énergie fonctionnent sans réglage
supplémentaire. Les clés `rl*` deviennent des `binary_sensor`.

## Compteurs de marche

Chaque relais (`rl*`) reçoit deux capteurs supplémentaires :

| Entité | Contenu |
|---|---|
| `sensor.solisart_<clé>_runtime` | durée de marche cumulée, en heures |
| `sensor.solisart_<clé>_starts` | nombre de démarrages cumulé |

Ce sont des compteurs croissants (`total_increasing`) : la carte « Graphique
de statistiques » affiche directement les heures de marche par jour, semaine
ou mois, sur toute la durée de vie de l'installation.

Pour suivre une grandeur qui n'est pas un relais (une consigne de circulateur
en %, par exemple), l'ajouter à l'option `runtime_keys`, séparée par des
virgules : elle est considérée en marche dès que sa valeur est > 0.

Les compteurs sont stockés dans `/data/cache/counters.json`. Un trou de plus de
trois intervalles (portail injoignable, add-on arrêté) n'est pas compté.

### Indicateur de dérive : heures de brûleur par degré-jour

À adapter : `sensor.solisart_flt9` (température extérieure) et
`sensor.solisart_rlchaudiere_runtime` (brûleur) sont des noms d'exemple.

```yaml
template:
  - sensor:
      - name: "Écart au seuil de chauffe"
        unique_id: solisart_ecart_seuil_chauffe
        unit_of_measurement: "°C"
        state: "{{ [18 - states('sensor.solisart_flt9') | float(18), 0] | max }}"

sensor:
  - platform: integration          # °C × jours = degrés-jours cumulés
    source: sensor.ecart_au_seuil_de_chauffe
    name: "Degrés-jours"
    unique_id: solisart_degres_jours
    unit_time: d
    method: left
    round: 2

utility_meter:
  bruleur_jour:
    source: sensor.solisart_rlchaudiere_runtime
    cycle: daily
  degres_jours_jour:
    source: sensor.degres_jours
    cycle: daily
```

Le ratio du jour (`bruleur_jour / degres_jours_jour`) reste à peu près stable
d'une année sur l'autre à réglages constants. S'il dérive durablement à la
hausse, quelque chose a changé : réglage, ensoleillement, ou un défaut (pression
du circuit solaire, sonde, vanne).

## Crédits

Dérivé du script `solisart.py` V04 de **J&J_Ian1515**
([jean1492/SolisArt-HomeAssistant](https://github.com/jean1492/SolisArt-HomeAssistant)),
publié sous GPL, usage commercial exclu. Même licence ici (GPL-2.0), même restriction.
