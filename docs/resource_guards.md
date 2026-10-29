# Garde-fous de ressources — lot B

`src/services/resource_guards.py` estime le coût d'une analyse **avant** de la lancer (sans
Streamlit ni réseau) et motive chaque refus. Le module n'est pas encore appelé par les pages
(lot de sélection territoriale) : aucun résultat existant n'est modifié.

**Statut des seuils : provisoires**, issus du diagnostic du 23/10/2026 et calibrés sur un seul
poste Windows. Ils ne sont validés ni scientifiquement ni opérationnellement.

## Estimations

| Contrôle | Méthode | Prudence |
|---|---|---|
| Cellules raster | grille UTM à 10 m couvrant l'**emprise** de la zone, par zone UTM et hémisphère | les traitements allouent l'emprise, pas seulement le polygone |
| Mémoire | base après imports + cellules × octets par cellule + octets JSON OSM × facteur d'expansion | facteurs majorés (voir calibration) ; avertissement au-delà de 50 % et refus au-delà de 90 % de la mémoire libre de l'hôte |
| Tuiles Sentinel-2 | carrés MGRS de 100 km touchés, par zone UTM et hémisphère (`EPSG:326xx/327xx`) | majorant : les tuiles se recouvrent de 9,8 km |
| OSM | emprise ≤ 0,5° (requête Overpass unique) ; volume estimé (`out count`) quand il est connu | volume inconnu ⇒ statut « inconnu », jamais supposé faible |
| Import GeoJSON | taille du fichier **avant lecture**, puis validation stricte, sommets, parties | fichier trop gros refusé sans être analysé |

**Mémoire libre (L4c, 29/10/2026)** : `available_memory_bytes` retient le minimum de la mémoire
libre de l'hôte (`psutil`) et de la mémoire restante sous la limite cgroup du conteneur
(`/sys/fs/cgroup/memory.max` − (`memory.current` − `inactive_file`) en v2, fichiers
`memory.limit_in_bytes` / `memory.usage_in_bytes` / `total_inactive_file` en v1). Dans un conteneur,
la mémoire de l'hôte ignore la limite du conteneur. Sans limite (`max`, valeur v1 géante) ou sans
fichier lisible (Windows, macOS), seule la mémoire de l'hôte compte. Non vérifié sur un conteneur
réel (Streamlit Cloud) : testé avec des fichiers cgroup simulés.

Statuts : `ok`, `avertissement`, `inconnu`, `refus`. Une analyse est autorisée tant qu'aucun
contrôle n'est en refus ; les avertissements et inconnues sont à afficher.

## Seuils provisoires (`ResourceLimits`, surchargeables par `GEONAF_LIMIT_<CHAMP>`)

| Ressource | Avertissement | Refus |
|---|---|---|
| Cellules (10 m, emprise) | > 4 000 000 | > 16 000 000 |
| Mémoire estimée | > 1 Gio, ou > 50 % de la mémoire libre | > 2 Gio, ou > 90 % de la mémoire libre |
| Tuiles Sentinel-2 | > 1 | > 4 |
| Volume OSM estimé | > 32 Mio | > 64 Mio |
| Emprise OSM | — | > 0,5° (découpage en tuiles non disponible) |
| Import GeoJSON | — | > 5 Mio, > 50 000 sommets, > 1 000 parties |

Les limites incohérentes (avertissement > refus, valeurs négatives ou non numériques) sont
refusées. Leur empreinte (`fingerprint`) accompagne chaque évaluation.

## Benchmark mémoire (`scripts/benchmark_resource_memory.py`, manuel, local, non versionné)

Un processus Python par scénario. Sentinel-2 local du 25/01/2025 (T32NNF). Départements de
Libreville lus en mémoire depuis geoBoundaries (≈ 650 Ko), écrits dans un dossier temporaire.

| Scénario | Cellules de grille | `tracemalloc` (pic) | Processus (pic) | Au-delà de la base (124 Mio) | Octets / cellule |
|---|---|---|---|---|---|
| NDVI 1 date, Akanda | 1 442 401 | 51 Mio | 215 Mio | 91 Mio | 66 |
| NDVI 1 date, Libreville gbOpen | 3 745 814 | 122 Mio | 292 Mio | 168 Mio | 47 |
| NDVI 1 date, Libreville gbHumanitarian | 4 798 382 | 156 Mio | 336 Mio | 212 Mio | 46 |
| Paire de dates, Akanda | 1 442 401 | 103 Mio | 267 Mio | 143 Mio | 104 |
| Chiffres clés, Akanda (WorldCover + OSM 24,3 Mo) | — | 175 Mio | 526 Mio | 402 Mio | — |

- `tracemalloc` ne voit qu'environ 55 % de la mémoire réelle des traitements raster (tampons
  natifs GDAL/rasterio non tracés) : seule la mémoire du processus sert à la calibration.
- L'analyse du JSON Overpass domine les chiffres clés : ≈ 16,5 × la taille du JSON.
- Calibration retenue : 156 o/cellule (paire × 1,5), base 160 Mio, OSM × 20.

## Application aux zones réelles (estimation seule)

| Zone | Cellules | Mémoire estimée | Tuiles S2 | Emprise OSM | Décision |
|---|---|---|---|---|---|
| Akanda (zone d'acquisition) | 1,44 M | 838 Mio (OSM inclus ; mesuré 526) | 1 | 0,11° | autorisée |
| Libreville, gbOpen | 3,75 M (grille réelle 3,746 M) | 718 Mio hors OSM (mesuré 292) | 1 | 0,2° | autorisée, OSM inconnu |
| Libreville, gbHumanitarian | 4,80 M (réelle 4,798 M) | 874 Mio hors OSM (mesuré 336) | 1 | 0,2° | avertissement cellules |
| Komo-Mondah, gbOpen | 37,9 M | 5,8 Gio | 2 | 0,65° | refusée |
| Komo-Mondah, gbHumanitarian | 94,5 M | 14,2 Gio | 4 | 1,2° | refusée |

## Limites

- Calibration sur un poste, une date, une tuile ; autres OS, versions de GDAL ou analyses
  (multisource complet, séries temporelles) non mesurés.
- Le benchmark du département de Libreville ne couvre que le NDVI d'une date (pas de couches
  WorldCover/OSM/Sentinel-1 récupérées pour le département : aucun téléchargement massif).
- Le décompte des tuiles est un majorant ; le volume OSM reste inconnu sans requête de comptage.
- Traitement par fenêtres et découpage OSM non implémentés ; la médiane NDVI reste exacte.
