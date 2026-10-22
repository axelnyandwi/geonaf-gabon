# Lot 5 — modes d'analyse, multituile et trois territoires

## Modes d'analyse (`src/geo/scientific_profile.py`, `src/components/analysis_settings.py`)

- **Analyse standard** (par défaut) : profil de référence choisi automatiquement selon
  le territoire (`REFERENCE_PROFILES`) — Akanda (inchangé, empreinte `c78ff691…`),
  Libreville, Owendo. Les deux derniers reprennent les paramètres d'Akanda (même contexte
  déclaré « Gabon/Estuaire, équatorial ») avec une description d'écosystème propre ; ils
  sont **expérimentaux et non calibrés**.
- **Analyse avancée** : `advanced_configuration(aoi_id, …)` dérive un **nouveau** profil
  (le profil de référence n'est jamais modifié) :
  - seuil ΔNDVI ∈ [0,02 ; 0,50], couverture minimale ∈ [1 ; 100] % ;
  - masques SCL autorisés : `usable_land`, `vegetation`, `bare_soil`. `observation` et
    `land_surface` sont refusés (ils conservent des pixels nuageux, ombragés,
    incertains ou défectueux) ;
  - identifiant `<référence>-avance-<empreinte courte>`, version
    `<version>.advanced-1`, justification citant l'empreinte du profil de référence ;
  - mêmes valeurs → même empreinte (reproductible) ; aucune modification → profil de
    référence renvoyé tel quel ;
  - l'empreinte effective entre dans toutes les clés de cache et exports existants
    (`local_preview_key`, `local_series_key`, `local_group_key`, GeoTIFF, CSV) ;
  - `AnalysisConfiguration.provenance` (mode, versions, empreintes, surcharges,
    `validated: false`) est affichée dans la page multisource.
- Le préréglage « scénario de sensibilité » (0,15 ; 50 %) remplace l'ancien choix direct
  du profil de sensibilité ; `GABON_SENSITIVITY_EXPERIMENTAL` reste défini pour la
  compatibilité des analyses existantes.

Les trois pages d'analyse (paire, série, multisource) utilisent le même composant.

## Multituile (`src/geo/mosaic.py`)

Règles communes à toutes les sources :

1. chaque tuile est alignée sur la grille cible par l'outil propre à la source (NDVI :
   bilinéaire pondéré par le masque ; classes : plus proche voisin ou mode ; radar : plus
   proche voisin) ; hors emprise ou masqué ⇒ invalide ;
2. ordre de priorité explicite : tuiles natives (même CRS et même maillage) puis
   identifiant ; la première tuile valide fournit la valeur ; **aucune moyenne, aucun
   remplissage** ;
3. recouvrements mesurés (cellules, classes en conflit, écart maximal) et provenance par
   tuile (`TileContribution`) ; indice de tuile source par cellule.

| Source | Découverte | Grille cible | Refus |
|---|---|---|---|
| Sentinel-2 | `group_local_observations` : même orbite relative, ≤ 15 min, tuiles MGRS distinctes | AOI dans le CRS et le maillage 10 m du premier produit | orbites/dates différentes, tuile en double |
| WorldCover | `fetch_worldcover_windows` : toutes les tuiles 3° recoupant l'AOI, une fenêtre AOI ∩ tuile chacune | grille Sentinel-2 | produits/versions/nomenclatures différents |
| Sentinel-1 | une mosaïque par tuile MGRS du mois, fenêtre AOI ∩ emprise de l'item | grille native 20 m de la première fenêtre | polarisations ou mois différents |
| OSM | une requête par AOI (≤ 0,5°) | rastérisation sur la grille S2 | — |

Une tuile unique suit exactement le chemin du lot 4 (non-régression vérifiée).

Couverture effective signalée par source : part d'aire exacte de l'AOI (S2, WorldCover),
part de cellules AOI (S1), emprise de requête (OSM). En dessous de 99 % ou du minimum du
profil, un avertissement est affiché ; les sources absentes sont listées et l'analyse se
poursuit avec les autres.

Constat réel : les trois AOI (carrés de 12 km) sont chacune contenues dans une seule
tuile WorldCover (N00E009), Sentinel-2 (T32NNF) et Sentinel-1 (32NNF). Le multituile est
donc vérifié par tests synthétiques ; sur les données réelles, la découverte renvoie
une tuile par source.

## Données réelles récupérées (22/10/2026)

| Territoire | WorldCover | OSM (estimé → reçu) | Sentinel-1 2025-01 VV+VH |
|---|---|---|---|
| Akanda | cache | cache (39 164 objets) | cache |
| Libreville | 0,39 Mo | 49 Mo estimés → 41,3 Mo, 61 497 objets | 4,3 Mo |
| Owendo | 0,52 Mo | 23 Mo estimés → 18,5 Mo, 28 166 objets | 4,3 Mo |

Estimation OSM : requête `out count` (≈ 400 octets) × 800 octets/objet (mesure Akanda :
620). Plafond OSM porté à 64 Mo (Libreville) ; au-delà, la requête n'est pas envoyée.

## Performances (benchmark `scripts/benchmark_multisource_territories.py`, à froid)

| Territoire | NDVI | OSM | Radar | Croisement | Carte | Total | Charge navigateur |
|---|---|---|---|---|---|---|---|
| Akanda | 2,2 s | 2,7 s | 2,1 s | 3,4 s | 0,8 s | 11,2 s | 2,88 Mo (avant : 4,47) |
| Libreville | 2,0 s | 4,5 s | 2,2 s | 5,9 s | 1,2 s | 15,8 s | 3,58 Mo (avant : 5,27) |
| Owendo | 2,0 s | 2,0 s | 2,1 s | 2,6 s | 0,6 s | 9,3 s | 2,28 Mo (avant : 3,66) |

Gains : PNG à palette pour NDVI et radar (NDVI 2,25 → 0,96 Mo, affichage seulement),
vecteurs découpés à l'AOI et arrondis à 1e-5°. Pic mémoire du benchmark : 670 Mo. Les
résultats sont ensuite servis par les caches Streamlit (prise de vue × AOI × profil,
instantané OSM, fenêtres radar, croisement, charge carte).

## Limites

- Profils Libreville/Owendo non calibrés ; paramètres hérités d'Akanda.
- Multituile réel non rencontré sur ces AOI ; seule la validation synthétique existe.
- Parsing OSM à froid de 2 à 5 s et pic mémoire à 780 Mo lors du téléchargement de
  Libreville (JSON de 41 Mo) : un format intermédiaire (GeoParquet) réduirait ce coût.
