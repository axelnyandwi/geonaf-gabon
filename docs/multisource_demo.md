# Démonstrateur multisource — lot 4 (Akanda)

> **Note du 26/10/2026** : les valeurs Sentinel-2 de ce document (empreinte `b0356911…`, couvertures, pixels comparables) ont été obtenues avant la correction du découpage AOI avec perte. Valeurs corrigées : [lossless_aoi_clip.md](lossless_aoi_clip.md).

Page Streamlit `pages/3_Multisource.py`. Le moteur ne dépend pas de Streamlit :
`src/services/multisource_demo.py`, `multisource_map.py`, `sentinel1_layer.py`,
`external_layers.py` et `external_retrieval.py`.

## Architecture

```
ingestion bornée           stockage                     moteur (sans UI)              interface
external_http ─┐     LayerFileStore (fichiers,     ndvi_observation (S2, 1 date)   components/multisource.py
remote_window ─┼──▶  write-once, clés relatives) ─▶ multisource_demo (croisements) ─▶ maplibre/multisource.js
worldcover     │     FileLayerRepository (JSON    sentinel1_layer (radar natif)
openstreetmap  │     par empreinte = future table multisource_map (affichage)
sentinel1     ─┘     external_layers PostGIS)
```

- **Fichiers** (`data/external/<source>/…`) : immuables, publiés de façon atomique,
  jamais écrasés ; un rafraîchissement crée une nouvelle clé.
- **Métadonnées** (`data/external/catalogue/<empreinte>.json`) : descripteur complet
  (`descriptor_to_record`) + `storage_key` relative. `FileLayerRepository.find_with_storage`
  reproduit `ST_Intersects(extent, bbox) ORDER BY écart temporel`. Migration PostGIS :
  table `external_layers` du lot 3 + colonne `storage_key text`; aucun chemin absolu
  n'est stocké. Les fichiers raster resteraient sur disque/objet (COG), seules les
  métadonnées et emprises passent en base.

## Récupération autorisée (réseau, sur clic uniquement)

| Source | Accès | Plafond | Cache | Contrôles |
|---|---|---|---|---|
| WorldCover v200 | HTTPS Range sur le bucket ESA (hôte en liste blanche) | 4 Mo | par AOI et version | estimation exacte par l'index des tuiles COG avant lecture ; budget d'octets ; empreinte SHA-256 |
| OSM | 1 requête POST Overpass (`overpass-api.de`) | 32 Mo, `[maxsize:64 Mo]`, timeout 60 s | par empreinte de requête, 7 jours | intervalle minimal 10 s ; réponse avec `remark` d'erreur rejetée et jamais stockée |
| Sentinel-1 | STAC CDSE puis S3 Range (client S3 GEONAF) | 16 Mo par polarisation | par item, polarisation et AOI | estimation par l'en-tête COG avant lecture ; refus si dépassement |

`ExternalHttpClient` réutilise `secure_session` (TLS vérifié, HTTP refusé), sans
redirection, avec `run_with_retry` (429/5xx, `Retry-After`), un User-Agent GEONAF,
une limite de taille et un compteur d'octets par hôte. GDAL/curl n'accède jamais au
réseau : il lit un objet fichier Python alimenté par des requêtes Range.

## Géométries OSM

Chemins (`way`) et relations `type=multipolygon` (`out geom`). Les anneaux sont
assemblés par `polygonize_full` ; un objet est **rejeté** (jamais réparé) si un membre
n'a pas de géométrie, si un sommet manque, si un anneau ne se ferme pas, ou si la
géométrie est invalide. Les compteurs `rejected_incomplete`, `rejected_invalid`,
`rejected_unsupported` sont dans la provenance. Rôle vide traité comme `outer`
(convention OSM). Les étiquettes libres (noms) ne sont pas transmises au navigateur ;
les attributs affichés sont insérés par `textContent`.

## Analyse

- **NDVI d'une date** (`ndvi_observation.py`) : même chaîne que les séries
  (`prepare_date_ndvi` extrait du lot 3), grille native 10 m de l'acquisition.
- **WorldCover × NDVI** : classes alignées au plus proche voisin sur la grille S2.
- **OSM** : groupes rastérisés (centre de cellule) sur la grille S2, non exclusifs ;
  surfaces et longueurs découpées à l'AOI en UTM 32N. Les mêmes cellules servent à
  l'affichage des bâtiments (ce qui est affiché est ce qui est compté).
- **Sentinel-1** : grille native 20 m, jamais ré-échantillonnée ; WorldCover agrégé
  par mode 10 → 20 m pour les statistiques par classe. **Aucune comparaison radar/NDVI.**
- Chaque source porte sa nature (*observation mesurée*, *classification externe*,
  *base collaborative*), sa date ou validité, l'écart avec l'acquisition S2, sa
  résolution, son CRS, son attribution et ses limites. Les *interprétations* sont des
  phrases générées par règles, présentées comme hypothèses.

## Sentinel-1 V1 : choix du produit

Collections CDSE étudiées le 22/10/2026 : `sentinel-1-grd` (≈1 Go par scène, L1,
calibration et orthorectification à faire), `sentinel-1-slc`, `-ocn`, `-etad`, et
**`sentinel-1-global-mosaics`** retenu : mosaïque mensuelle IW, tuile MGRS (32NNF pour
Akanda), EPSG:32632, 20 m, VV/VH en COG float32, NoData −32768. Algorithme CDSE :
calibration β⁰, bruit thermique retiré, correction radiométrique de terrain (Copernicus
DEM 30 m), orthorectification Range-Doppler, ombres radar exclues, composite pondéré
des deux orbites.

Unités : le STAC dit « amplitude », la documentation ne précise pas l'échelle. Sur
Akanda (janvier 2025), 10·log10 donne forêt VV −6,6 dB / VH −13,1 dB, eau VV −19,0 dB /
VH −29,9 dB (ordres attendus en bande C) ; 20·log10 donnerait eau VH ≈ −60 dB
(invraisemblable). Convention retenue : **intensité, 10·log10**, signalée comme
déduite et non confirmée. Pas d'instant, d'angle d'incidence ni d'orbite uniques par
pixel (composite). Masque qualité : NoData/NaN/≤0 ; repliement non signalé.

## Résultats réels (22/10/2026)

- OSM : 1 requête, 24,3 Mo reçus, base OSM du 22/10/2026 16:57 UTC ; 39 164 objets
  (36 012 bâtiments, 3 035 voies, 17 relations), 0 rejet.
- Sentinel-1 janvier 2025 : estimation 1,85 Mo (9 tuiles internes) pour une fenêtre
  601×601 ; ≈2 Mo reçus par polarisation sur ≈105 Mo par fichier ; 1,3 Mo écrits.
- WorldCover : fenêtre du lot 3 réutilisée, 0 octet réseau.
- S2 25/01/2025 : NDVI médian 0,505, couverture valide 63,78 %. NDVI moyen par classe
  WorldCover : couvert arboré 0,559 (419 924 px), mangroves 0,531, bâti 0,282.
  Écarts signalés : WorldCover +1 120 j, OSM −635 j (OSM postérieur), S1 dans la
  validité mensuelle.

## Limites

- Démonstrateur descriptif ; aucune validation terrain ni indépendante.
- AOI sur une seule tuile WorldCover / MGRS en V1.
- Les instantanés OSM successifs s'accumulent (aucune suppression automatique).
- Ingestion quotidienne S1/S2 non planifiée : les mosaïques S1 sont mensuelles et
  publiées plusieurs mois après (janvier 2025 publié le 19/06/2025).

Lot 5 (modes d'analyse, multituile, trois territoires) : voir `multiterritoire.md` et `sentinel1_radiometry.md`.
