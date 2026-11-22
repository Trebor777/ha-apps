# Changelog

## 1.1.0

- Compteurs de marche pour chaque relais (`rl*`) : durée cumulée en heures et
  nombre de démarrages, en `total_increasing`. Home Assistant en garde les
  statistiques sans limite de durée (heures de brûleur par jour, par saison…).
- Option `runtime_keys` : doter d'un compteur des grandeurs qui ne sont pas des
  relais (en marche dès que la valeur est > 0).
- Les compteurs sont conservés dans `/data/cache/counters.json` et survivent aux
  redémarrages. Une absence de relevé de plus de trois intervalles n'est pas
  comptée comme du temps de marche.

## 1.0.1

- Passage à l'API de callbacks v2 de paho-mqtt (supprime l'avertissement
  `Callback API version 1 is deprecated`). paho-mqtt 2.x désormais requis.
- Ajout de `build.yaml` : le Supervisor ne fournit plus d'image de base par défaut.

## 1.0.0

- Version d'origine, récupérée de la sauvegarde du 18/09/2026.
