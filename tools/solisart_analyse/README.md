# Analyse des exports SolisArt

Compare deux périodes d'exports CSV du portail MySolisArt (SolisConfort) :
froid (degrés-jours), temps de brûleur, démarrages, solaire, consignes, courbe
de chauffe.

```sh
python3 solisart_analyse.py exports/ \
  --periode-a 2025-10-22:2025-11-13 \
  --periode-b 2026-10-22:2026-11-13 \
  --graphique comparaison.png --details
```

Accepte des `.csv`, des `.zip` et des dossiers. Dépendances : `pandas`, et
`matplotlib` pour `--graphique`.

**Les exports sont des données privées** : les placer dans `exports/`, qui est
exclu de git (ainsi que les `.png`/`.csv` produits ici).

Hypothèses sur les colonnes : voir l'en-tête du script (`chdr1` = relais du
brûleur, 0/100 ; deux lignes par minute, soit 30 s par ligne).

Points d'attention :
- la courbe de chauffe est donnée brûleur en marche / arrêté : le départ vaut
  ~53 °C brûleur en marche et ~33 °C à l'arrêt, donc une moyenne globale suit
  surtout la part de temps brûleur ;
- vérifier la colonne `jours` : une tranche de température froide peut ne
  couvrir qu'un ou deux jours ;
- les débitmètres `Deb*` sont à 0 sur cette installation : le débit solaire
  n'est pas mesurable à partir des exports.
