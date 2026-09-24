# ha-apps

Dépôt d'apps (ex-« add-ons ») Home Assistant maison.

| App | Dossier | Rôle |
|---|---|---|
| SolisArt MQTT | [`solisart_mqtt/`](solisart_mqtt/) | Passerelle MySolisArt → MQTT Discovery |

## Pourquoi un dépôt

Une app « locale » (dossier `/addons` du serveur) n'existe que tant que son dossier source
est présent. Lors de la migration VM → M720q (03/09/2026), la restauration a réinstallé
l'image de SolisArt MQTT mais pas son dossier source : le Supervisor l'a alors signalée
`detached_app_removed` et a proposé de la supprimer, ce qui a été fait le 18/09/2026.

Ce dépôt est la source de vérité. Toute modification se fait ici, puis on incrémente
`version` dans le `config.yaml` de l'app pour que Home Assistant propose la mise à jour.

## Installation

Voir le README de chaque app.
