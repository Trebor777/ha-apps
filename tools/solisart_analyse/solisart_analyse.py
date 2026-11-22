#!/usr/bin/env python3
"""
solisart_analyse.py — comparaison de deux périodes d'exports SolisArt (SolisConfort).

Lit les exports CSV du portail MySolisArt (ou les zip qui les contiennent),
agrège par jour et compare deux périodes : froid (degrés-jours), temps de
brûleur, démarrages, solaire, eau chaude, consignes, courbe de chauffe.

Usage :
    python3 solisart_analyse.py EXPORTS... \
        --periode-a 2025-10-22:2025-11-13 \
        --periode-b 2026-10-22:2026-11-13 \
        [--seuil 18] [--csv-jours jours.csv] [--graphique comparaison.png]

EXPORTS : fichiers .csv ou .zip, ou dossiers (parcourus récursivement).
Les bornes de période sont incluses.

Hypothèses sur les colonnes (export « SolisConfrt VsD.56+6 ») :
  - chdr1    relais du brûleur (0 / 100) : on mesure un temps de marche,
             pas des m³ de gaz ;
  - SOL      pompe solaire, DemECS demande d'eau chaude, C1 / C2 circulateurs
             des zones 1 et 2 (en marche si > 0) ;
  - Text     température extérieure ; TZ1 / TZ2 ambiances ; TdepC / TretC
             départ et retour chauffage ; Tcapt capteurs solaires ;
  - Tcons1 / Tcons2 consignes de zone, TconsECS consigne d'eau chaude.
Un échantillon toutes les 30 s (deux lignes par minute horodatées à la
minute) : la durée de chaque ligne est donc fixée à 30 s.

Dépendances : pandas (et matplotlib pour --graphique).
"""

from __future__ import annotations

import argparse
import io
import sys
import zipfile
from pathlib import Path

import pandas as pd

PAS_S = 30                      # durée représentée par une ligne
COLONNES_DEBIT_SUFFIXE = " l mn"


# --------------------------------------------------------------------------- #
# Lecture
# --------------------------------------------------------------------------- #


def _sources(chemins: list[str]):
    """Renvoie des couples (nom, flux texte) pour chaque CSV trouvé."""
    for brut in chemins:
        p = Path(brut)
        fichiers = sorted(p.rglob("*")) if p.is_dir() else [p]
        for f in fichiers:
            if f.suffix.lower() == ".csv":
                yield f.name, f.open(encoding="latin-1")
            elif f.suffix.lower() == ".zip":
                with zipfile.ZipFile(f) as z:
                    for nom in z.namelist():
                        if nom.lower().endswith(".csv"):
                            texte = z.read(nom).decode("latin-1")
                            yield f"{f.name}:{nom}", io.StringIO(texte)


def charger(chemins: list[str]) -> pd.DataFrame:
    morceaux = []
    for nom, flux in _sources(chemins):
        # Ligne 1 : version du firmware ; ligne 2 : en-têtes.
        d = pd.read_csv(flux, sep=";", skiprows=1, na_values=["dsc"],
                        dtype=str, low_memory=False)
        print(f"lu {nom} : {len(d)} lignes", file=sys.stderr)
        morceaux.append(d)
    if not morceaux:
        sys.exit("Aucun CSV trouvé.")

    df = pd.concat(morceaux, ignore_index=True)
    # Les exports répètent parfois la ligne d'en-têtes au milieu du fichier.
    df = df[df["Date"].str.match(r"\d\d/\d\d/\d\d", na=False)]
    df["Date"] = pd.to_datetime(df["Date"], format="%d/%m/%y %H:%M")
    for c in df.columns[1:]:
        if c.startswith("Deb"):
            df[c] = df[c].str.replace(COLONNES_DEBIT_SUFFIXE, "", regex=False)
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates().sort_values("Date", kind="stable").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# Agrégats
# --------------------------------------------------------------------------- #


def par_jour(df: pd.DataFrame, seuil: float) -> pd.DataFrame:
    h = PAS_S / 3600
    marche = df["chdr1"] > 0
    x = pd.DataFrame({
        "jour": df["Date"].dt.floor("D"),
        "Text": df["Text"], "TZ1": df["TZ1"], "TZ2": df["TZ2"],
        "TdepC": df["TdepC"], "Tcapt": df["Tcapt"], "TbalS": df["TbalS"],
        "bruleur_h": marche * h,
        "demarrages": (marche & ~marche.shift(fill_value=False)).astype(int),
        "solaire_h": (df["SOL"] > 0) * h,
        "ecs_demande_h": (df["DemECS"] > 0) * h,
        "bruleur_pendant_ecs_h": (marche & (df["DemECS"] > 0)) * h,
        "circ_z1_h": (df["C1"] > 0) * h,
        "circ_z2_h": (df["C2"] > 0) * h,
    })
    j = x.groupby("jour").agg(
        Text=("Text", "mean"), TZ1=("TZ1", "mean"), TZ2=("TZ2", "mean"),
        TdepC=("TdepC", "mean"), Tcapt_max=("Tcapt", "max"), TbalS_max=("TbalS", "max"),
        bruleur_h=("bruleur_h", "sum"), demarrages=("demarrages", "sum"),
        solaire_h=("solaire_h", "sum"), ecs_demande_h=("ecs_demande_h", "sum"),
        bruleur_pendant_ecs_h=("bruleur_pendant_ecs_h", "sum"),
        circ_z1_h=("circ_z1_h", "sum"), circ_z2_h=("circ_z2_h", "sum"),
        lignes=("Text", "size"),
    )
    j["DJU"] = (seuil - j["Text"]).clip(lower=0)
    return j


def _periode(texte: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    a, _, b = texte.partition(":")
    debut = pd.Timestamp(a)
    fin = pd.Timestamp(b) + pd.Timedelta(days=1)    # borne de fin incluse
    return debut, fin


def resume(df: pd.DataFrame, jours: pd.DataFrame, debut, fin, seuil_soleil: float) -> pd.Series:
    j = jours[(jours.index >= debut) & (jours.index < fin)]
    brut = df[(df["Date"] >= debut) & (df["Date"] < fin)]
    marche = brut[brut["chdr1"] > 0]
    heure = marche["Date"].dt.hour
    h = PAS_S / 3600
    br = j["bruleur_h"].sum()
    return pd.Series({
        "jours": len(j),
        "jours incomplets (<90 %)": int((j["lignes"] < 0.9 * 86400 / PAS_S).sum()),
        "T ext moyenne (°C)": j["Text"].mean(),
        "degrés-jours": j["DJU"].sum(),
        "ambiance zone 1 (°C)": j["TZ1"].mean(),
        "ambiance zone 2 (°C)": j["TZ2"].mean(),
        "brûleur (h)": br,
        "brûleur 5h-10h (h)": ((heure >= 5) & (heure < 10)).sum() * h,
        "brûleur hors 5h-10h (h)": ((heure < 5) | (heure >= 10)).sum() * h,
        "brûleur pendant demande ECS (h)": j["bruleur_pendant_ecs_h"].sum(),
        "démarrages": j["demarrages"].sum(),
        "durée moyenne d'un cycle (min)": 60 * br / max(j["demarrages"].sum(), 1),
        "brûleur par degré-jour (h/DJ)": br / j["DJU"].sum() if j["DJU"].sum() else float("nan"),
        "pompe solaire (h)": j["solaire_h"].sum(),
        f"jours ensoleillés (Tcapt max > {seuil_soleil:g} °C)": int((j["Tcapt_max"] > seuil_soleil).sum()),
        "circulateur zone 1 (h)": j["circ_z1_h"].sum(),
        "circulateur zone 2 (h)": j["circ_z2_h"].sum(),
        "consigne zone 1 min / max (°C)": f"{brut['Tcons1'].min():.1f} / {brut['Tcons1'].max():.1f}",
        "consigne zone 2 min / max (°C)": f"{brut['Tcons2'].min():.1f} / {brut['Tcons2'].max():.1f}",
        "consigne ECS (°C, la plus fréquente)": brut["TconsECS"].mode().iloc[0] if len(brut) else None,
    })


def courbe_de_chauffe(df: pd.DataFrame, periodes: dict[str, tuple]) -> pd.DataFrame:
    """Départ chauffage moyen, circulateur zone 1 en marche, par tranche de T ext.

    Séparé selon l'état du brûleur : le départ vaut ~53 °C brûleur en marche et
    ~33 °C à l'arrêt, quelle que soit l'année. Une moyenne tous états confondus
    mesure surtout la part de temps brûleur, pas la courbe de chauffe ; et une
    tranche froide peut ne couvrir qu'un jour ou deux (colonne `jours`).
    """
    tranches = [-20, 0, 4, 6, 8, 10, 12, 14, 16, 30]
    sorties = {}
    for nom, (debut, fin) in periodes.items():
        x = df[(df["Date"] >= debut) & (df["Date"] < fin) & (df["C1"] > 0)].copy()
        x["tranche"] = pd.cut(x["Text"], tranches)
        marche = x["chdr1"] > 0
        g = x.groupby("tranche", observed=True)
        sorties[nom] = pd.DataFrame({
            "jours": g["Date"].agg(lambda s: s.dt.date.nunique()),
            "part brûleur": g["chdr1"].agg(lambda s: (s > 0).mean()),
            "TdepC": g["TdepC"].mean(),
            "TdepC brûleur": x[marche].groupby("tranche", observed=True)["TdepC"].mean(),
            "TdepC sans brûleur": x[~marche].groupby("tranche", observed=True)["TdepC"].mean(),
        })
    return pd.concat(sorties, axis=1).round(2)


def horaires_consignes(df: pd.DataFrame, debut, fin) -> pd.DataFrame:
    """Par jour : heure de passage en confort (première minute à la consigne max)."""
    x = df[(df["Date"] >= debut) & (df["Date"] < fin)].copy()
    x["jour"] = x["Date"].dt.date
    x["h"] = x["Date"].dt.hour + x["Date"].dt.minute / 60
    lignes = {}
    for jour, g in x.groupby("jour"):
        ligne = {}
        for c in ("Tcons1", "Tcons2"):
            haut = g[g[c] >= g[c].max() - 0.05]
            ligne[f"{c} min"] = g[c].min()
            ligne[f"{c} max"] = g[c].max()
            ligne[f"{c} confort dès"] = f"{int(haut['h'].min())}h{int(haut['h'].min() % 1 * 60):02d}"
        lignes[jour] = ligne
    return pd.DataFrame(lignes).T


# --------------------------------------------------------------------------- #
# Sortie
# --------------------------------------------------------------------------- #


def graphique(jours: pd.DataFrame, periodes: dict[str, tuple], chemin: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 4.5))
    for nom, (debut, fin) in periodes.items():
        j = jours[(jours.index >= debut) & (jours.index < fin)]
        ax.scatter(j["DJU"], j["bruleur_h"], label=nom, alpha=0.8)
    ax.set_xlabel("Degrés-jours")
    ax.set_ylabel("Heures de brûleur")
    ax.set_title("Heures de brûleur par jour selon le froid")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(chemin, dpi=130)
    print(f"graphique : {chemin}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("exports", nargs="+", help="fichiers .csv / .zip ou dossiers")
    ap.add_argument("--periode-a", required=True, help="AAAA-MM-JJ:AAAA-MM-JJ (bornes incluses)")
    ap.add_argument("--periode-b", required=True, help="AAAA-MM-JJ:AAAA-MM-JJ (bornes incluses)")
    ap.add_argument("--seuil", type=float, default=18.0, help="seuil des degrés-jours (°C, défaut 18)")
    ap.add_argument("--seuil-soleil", type=float, default=48.0,
                    help="Tcapt max au-delà de laquelle un jour compte comme ensoleillé (défaut 48)")
    ap.add_argument("--csv-jours", help="écrit le tableau journalier dans ce fichier")
    ap.add_argument("--graphique", help="écrit un nuage brûleur / degrés-jours (PNG)")
    ap.add_argument("--details", action="store_true", help="affiche aussi le détail jour par jour")
    args = ap.parse_args()

    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 500)
    pd.set_option("display.max_columns", 50)

    df = charger(args.exports)
    print(f"{len(df)} lignes, du {df['Date'].min()} au {df['Date'].max()}\n", file=sys.stderr)
    jours = par_jour(df, args.seuil)

    periodes = {
        f"A {args.periode_a}": _periode(args.periode_a),
        f"B {args.periode_b}": _periode(args.periode_b),
    }

    print("=== Comparaison des périodes ===")
    tableau = pd.DataFrame({nom: resume(df, jours, d, f, args.seuil_soleil)
                            for nom, (d, f) in periodes.items()})
    a, b = tableau.columns
    num = pd.to_numeric(tableau[a], errors="coerce"), pd.to_numeric(tableau[b], errors="coerce")
    tableau["écart B/A"] = ((num[1] / num[0] - 1) * 100).round(0).map(
        lambda v: "" if pd.isna(v) or abs(v) == float("inf") else f"{v:+.0f} %")
    print(tableau.to_string(float_format=lambda v: f"{v:.2f}"))

    print("\n=== Courbe de chauffe (circulateur zone 1 en marche) ===")
    print(courbe_de_chauffe(df, periodes).to_string())

    for nom, (d, f) in periodes.items():
        print(f"\n=== Consignes, période {nom} ===")
        print(horaires_consignes(df, d, f).to_string())

    if args.details:
        cols = ["Text", "DJU", "TZ1", "TZ2", "bruleur_h", "demarrages", "bruleur_pendant_ecs_h",
                "solaire_h", "Tcapt_max", "TbalS_max", "circ_z1_h", "circ_z2_h", "TdepC"]
        for nom, (d, f) in periodes.items():
            print(f"\n=== Détail journalier, période {nom} ===")
            print(jours.loc[(jours.index >= d) & (jours.index < f), cols].round(1).to_string())

    if args.csv_jours:
        jours.round(3).to_csv(args.csv_jours, sep=";", decimal=",")
        print(f"tableau journalier : {args.csv_jours}", file=sys.stderr)
    if args.graphique:
        graphique(jours, periodes, args.graphique)
    return 0


if __name__ == "__main__":
    sys.exit(main())
