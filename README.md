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

## Crédits et licence

**SolisArt MQTT** est dérivé du script `solisart.py` V04 de **J&J_Ian1515**
([jean1492/SolisArt-HomeAssistant](https://github.com/jean1492/SolisArt-HomeAssistant)),
qui a trouvé comment lire les données du portail MySolisArt (authentification, endpoint
`lecture_valeurs_donnees.php`, dictionnaire des codes). Tout le travail d'origine sur le
protocole est le sien ; ce dépôt n'en est qu'une réécriture en démon MQTT.

Le script d'origine est publié sous GPL avec la mention « usage commercial exclu ». Ce
dépôt est donc distribué sous **GPL-2.0** (voir [`LICENSE`](LICENSE)), et la volonté de
l'auteur est respectée : **aucun usage commercial**.
