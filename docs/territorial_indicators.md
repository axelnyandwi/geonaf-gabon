# Chiffres clés du territoire — lot 6, module 1

Première section de la page d'accueil (`app.py`), avant les analyses scientifiques. Le moteur
`src/services/territorial_indicators.py` ne dépend pas de Streamlit ; l'affichage est dans
`src/components/territorial_indicators.py`. Aucun accès réseau : seules les couches déjà
stockées (`data/external/`, catalogue vérifié par empreinte SHA-256) sont lues.

La zone d'étude est le carré d'acquisition de 12 × 12 km (« Zone d'étude Akanda »), **pas une
limite administrative**. Les AOI voisines se recouvrent.

## Organisation du code (`src/indicators/`, depuis le 28/10/2026)

`src/services/territorial_indicators.py` reste le point d'import historique et réexporte les noms
publics. L'implémentation est répartie par responsabilité, sans changement de résultat (sorties
complètes identiques octet pour octet avant et après l'extraction, sur les trois zones locales et le
paquet v3) :

| Module | Rôle |
|---|---|
| `model` | enregistrements (`TerritorialIndicator`…), statuts et niveaux de fiabilité |
| `profiles` | profils versionnés (`AFRICA_DEFAULT_V1`, empreinte figée par test) |
| `zone` | zone d'étude = fichier de géométrie polygonale : empreinte, projection UTM locale, aire équivalente |
| `landcover`, `osm` | mesures (surfaces, longueurs, comptages) sur couches chargées |
| `provenance` | dates, sources et limites tirées des descripteurs |
| `quality` | règles de fiabilité et comparabilité temporelle (`assess_change`) |
| `inventory` | inventaire hors ligne des couches d'une zone (`indicator_inputs`, `inputs_for_zone`) |
| `assembly` | construction de l'ensemble d'indicateurs |

`inputs_for_zone(data_dir, zone_id, chemin)` accepte un fichier de zone quelconque (par exemple une
géométrie du registre des territoires) : seules les couches enregistrées pour cet identifiant et
cette géométrie sont utilisées ; sinon les indicateurs sont « indisponibles ». L'interface ne
propose encore que les zones d'acquisition ; la limite « carré d'acquisition, pas une limite
administrative » de la surface de zone reste propre à ces zones (intégration des autres natures de
zones : V1.1).

## Contrat de données (`TerritorialIndicator`)

| Champ | Contenu |
|---|---|
| `indicator_id`, `label`, `value`, `unit` | `value=None` si la source manque (statut `indisponible`) ; jamais d'estimation |
| `aoi_id`, `aoi_sha256` | identité exacte de la géométrie utilisée |
| `dates` | trois dates distinctes : **observation** (période observée ; `None` si inconnue, cas OSM), **référentiel** (version ou état de la base), **ingestion** (récupération par GEONAF) |
| `source` | identifiant, produit, version, nature (*Classification externe*, *Base collaborative*, *Géométrie de référence*), attribution, empreinte du descripteur |
| `coverage_percent` | part de la zone d'étude couverte par la source (raster) |
| `method`, `limits` | méthode en clair ; limites propres à l'indicateur |
| `reliability`, `reliability_reasons` | niveau qualitatif à règles explicites (ci-dessous) |
| `shares`, `breakdown` | parts par dénominateur ; ventilation (catégories, regroupements) |
| `profile_fingerprint` | empreinte du profil d'indicateurs |

## Nature des résultats (`result_kind`)

Chaque indicateur porte explicitement la nature de sa valeur, fixée par l'assemblage
(`src/indicators/assembly.py`) et jamais déduite par l'interface :

| Nature | Sens | Indicateurs |
|---|---|---|
| Mesure géométrique | calcul exact sur la géométrie de la zone d'étude | surface de la zone |
| Observation d'une base cartographique | contenu d'OpenStreetMap à la date de l'instantané ; ni relevé de terrain ni total réel | voies, nombre et emprise des bâtiments, densité sur toute la zone |
| Estimation | sortie d'un modèle ou d'une classification, exactitude non vérifiée localement | surfaces WorldCover (terres, végétation, bâti), densité sur la surface terrestre (dénominateur classifié) |
| Prédiction | projection d'un état futur | aucune dans cette version |

Un indicateur indisponible garde la nature qu'aurait sa valeur.

## Présentation (`src/indicators/presentation.py`, `src/components/territorial_indicators.py`)

- **Cartes** : valeur principale (km² pour les surfaces), valeur secondaire en hectares (affichage
  seulement, 1 km² = 100 ha), puis pourcentages avec dénominateur nommé : « de la zone d'étude (eaux
  comprises) » ou « de la surface terrestre (hors eaux permanentes) ». Badges : nature du résultat et
  fiabilité ; date visible (période observée, sinon état du référentiel). Valeur absente : « Non
  disponible » et son motif.
- **Densité de bâtiments sur la surface terrestre** : la carte nomme ses deux sources avec leurs
  propres dates, lues dans les métadonnées : numérateur OpenStreetMap (date de l'instantané de la
  base), dénominateur WorldCover (année de la carte), et précise que ce rapport ne décrit pas une
  même année d'observation. Formule et valeurs inchangées.
- **Section repliable « Provenance et fiabilité »** par carte : nature, source et version, attribution
  et licence, trois dates, couverture de la source, motifs du niveau de fiabilité, méthode, limites.
- **Attributions** vérifiées le 28/10/2026 sur les pages des producteurs et affichées sous les cartes :
  - ESA WorldCover : « © ESA WorldCover project [année] / Contains modified Copernicus Sentinel data
    ([année]) processed by ESA WorldCover consortium », CC BY 4.0, DOI 10.5281/zenodo.7254221 (v200)
    ou 10.5281/zenodo.5571936 (v100) — https://esa-worldcover.org/en/data-access. L'année vient de
    la carte ; l'attribution enregistrée dans les descripteurs (sans année) n'est pas modifiée.
  - OpenStreetMap : « © OpenStreetMap contributors », ODbL 1.0, lien
    https://www.openstreetmap.org/copyright.
  - Toute autre source : texte du descripteur, signalé « non revérifié ».
- **Glossaire des voies** (affichage seulement ; les catégories et l'empreinte du profil sont
  inchangées) : « Voies de desserte » s'affiche « Rues de desserte locale » (rues résidentielles,
  zones de rencontre, voies mineures `unclassified`, type non précisé `road`) ; « Voies de service »
  s'affiche « Voies d'accès et de service » (accès à un site, un bâtiment, un parking, une
  installation). Le statut d'accès des voies `service` et le revêtement des pistes `track` ne sont
  **pas** déduits : ils dépendent d'attributs OSM (`access`, `surface`, `tracktype`) non retenus par
  l'ingestion actuelle. Les valeurs `highway` sont traduites, valeur brute entre parenthèses.

## Profil d'indicateurs versionné

`AFRICA_DEFAULT_V1` (`geonaf-indicateurs-afrique` 1.0.0) contient tous les regroupements. Un
autre pays ou écosystème reçoit un **nouveau** profil (nouvelle empreinte), jamais une
modification de celui-ci. La projection métrique des vecteurs est la zone UTM de l'AOI
(`estimate_utm_crs`), valable partout en Afrique.

**Végétation (ESA WorldCover)** — classes 10, 20, 30, 40, 90, 95 :

| Regroupement | Classes |
|---|---|
| Végétation naturelle | 10 Couvert arboré, 20 Arbustes, 30 Prairies, 90 Zones humides herbacées |
| Cultures | 40 Cultures |
| Mangroves | 95 Mangroves |

Bâti : classe 50 (bâtiments, routes et surfaces artificialisées selon le producteur).
Surface terrestre : surface classée hors 80 (Eaux permanentes).

**Voies (OSM `highway`)** :

| Catégorie | Valeurs |
|---|---|
| Axes principaux | motorway, trunk, primary (+ `_link`) |
| Axes secondaires | secondary, secondary_link |
| Axes tertiaires | tertiary, tertiary_link |
| Voies de desserte | residential, unclassified, living_street, road |
| Voies de service | service |
| Pistes | track |
| Chemins piétons et cyclables | footway, path, pedestrian, cycleway, steps, bridleway |
| Autres voies | toute autre valeur |
| Exclues | proposed, construction, abandoned, disused, razed, platform, raceway, bus_stop, corridor, elevator, rest_area, services, emergency_bay |

## Méthodes

- **Surfaces WorldCover** : grille native du produit (EPSG:4326, 1/12 000°), poids exacts
  cellule/AOI en aire équivalente EPSG:6933 (`prepare_pixel_area_weights`), **toutes** les
  cellules classées. Le masque NDVI/SCL Sentinel-2 n'est jamais utilisé : la surface ne dépend
  pas des nuages d'une date. Plusieurs tuiles : mosaïque sur le même maillage, sans
  ré-échantillonnage de classe ; versions mélangées refusées.
- **Voies** : longueur des chemins `highway` découpés à l'AOI, en UTM locale. C'est une
  **longueur de tronçons cartographiés**, pas une longueur de réseau : une chaussée séparée
  cartographiée par sens compte deux fois (pas de dédoublonnage géométrique en V1).
- **Bâtiments** : un bâtiment appartient à la zone si son point représentatif (toujours dans le
  polygone) y est : deux zones voisines ne le comptent jamais deux fois. Emprise : surface
  découpée à l'AOI. Densités : sur la **surface terrestre** (principale) et sur toute la zone.
  La densité terrestre combine OSM (date de la base) et WorldCover (2021), ce qui est signalé.

## Fiabilité (règles)

| Niveau | Règle |
|---|---|
| élevée | géométrie exacte de la zone d'étude |
| moyenne | classification dont l'exactitude est déclarée par le producteur, non vérifiée localement, couverture ≥ 99 % |
| faible | couverture < 99 % (avertissement ; aucune extrapolation) |
| non évaluée | OpenStreetMap : complétude inconnue, valeur minimale cartographiée |

## Évolutions temporelles

`assess_change(avant, après)` ne renvoie une différence que si : même indicateur et unité, même
AOI (empreinte), même profil, même source, même version pour une classification, référentiels
distincts dans l'ordre chronologique, couvertures ≥ 99 %. Sinon : `non comparable` avec les
motifs. Entre deux instantanés OSM, le résultat est qualifié d'**évolution de la cartographie**
(ni nouvelles constructions ni nouvelles voies). WorldCover v100 (2020) et v200 (2021) sont
refusés : algorithmes différents.

État local au 23/10/2026 : une seule version WorldCover (v200) et un seul instantané OSM par
zone ⇒ **aucune évolution affichée**, et l'interface le dit.

## Résultats réels — Zone d'étude Akanda (23/10/2026)

Entrées : WorldCover v200 (carte 2021, ingérée le 22/10/2026), base OSM du 22/10/2026 16:57 UTC.

| Indicateur | Valeur |
|---|---|
| Surface de la zone d'étude | 144,11 km² |
| Surface terrestre | 107,44 km² (74,55 %) ; eaux permanentes 36,56 km² |
| Surface végétalisée | 82,58 km² — 57,30 % de la zone, 76,86 % des terres |
| · Végétation naturelle / Cultures / Mangroves | 63,95 / 0,012 / 18,62 km² |
| Surface bâtie (classe Bâti) | 23,92 km² — 16,60 % de la zone, 22,26 % des terres |
| Voies cartographiées | 635 km de tronçons (desserte 381, service 150, tertiaires 39, principaux 25, secondaires 21, pistes 14, chemins 4,5) |
| Bâtiments cartographiés | 36 006 (point représentatif ; 36 012 intersectent la zone) |
| Densité de bâtiments cartographiés | 335 /km² de terres ; 250 /km² de zone |
| Emprise au sol des bâtiments cartographiés | 4,48 km² (3,11 % de la zone) |

Couverture WorldCover : 99,92 % de la zone. Valeurs figées par
`tests/test_akanda_territorial_indicators.py` (non-régression, pas validation d'exactitude).

## Performances (à froid, `scripts/benchmark_territorial_indicators.py`)

| Zone | Inventaire | Surfaces WorldCover | Lecture OSM | Voies | Bâtiments | Calcul complet |
|---|---|---|---|---|---|---|
| Akanda | 0,27 s | 0,55 s | 2,84 s | 0,11 s | 1,15 s | ≈ 5 s |
| Libreville | 0,20 s | 0,73 s | 4,44 s | 0,15 s | 1,61 s | ≈ 7 s |
| Owendo | 0,15 s | 0,71 s | 1,97 s | 0,10 s | 0,75 s | ≈ 3,5 s |

Le résultat est ensuite servi par `st.cache_resource` (clé : empreinte AOI, empreintes des
couches, empreinte du profil), indépendante du mode d'analyse. L'inventaire (≈ 0,2–0,3 s,
contrôle d'empreinte des fichiers) est refait à chaque exécution de la page.

## Limites

- Exhaustivité OSM inconnue ; bâtiments et voies OSM ≠ total réel ; nouvel objet OSM ≠ nouvelle
  construction.
- WorldCover : classification 10 m de 2021, exactitude non vérifiée localement, nomenclature
  mondiale ; décrit 2021, pas l'état actuel.
- Différences de résolution (10 m raster vs vecteurs) et de dates entre sources.
- L'instantané OSM est relu séparément par la page multisource (deux caches en mémoire).
- Extension démographique (WorldPop/GHSL) : non intégrée, prévue après ce module.
