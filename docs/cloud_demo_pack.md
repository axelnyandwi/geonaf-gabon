# Paquet de démonstration et Streamlit Community Cloud

## Objectif

Rendre https://geonaf.streamlit.app autonome avec les vraies données d'Akanda,
sans versionner les données dans Git ni dépendre du PC de développement.

## Contenu du paquet `geonaf-demo-akanda-2025-01` v2

- Zone : carré d'acquisition historique de 12 × 12 km autour d'Akanda. Ce n'est
  ni la commune d'Akanda ni une unité administrative.
- Sentinel-2 L2A, tuile T32NNF, acquisitions des 15, 20 et 25/01/2025 : extraits
  B04/B08 (10 m) et SCL (20 m) sur la grille native, sans rééchantillonnage
  (`src/data/derived_subset.py`), XML de calibration et `manifest.safe` copiés à
  l'identique. Chaque `subset_manifest.json` conserve les empreintes de la tuile
  parente (vérifiée contre STAC à l'ingestion) et celles de l'extrait.
- Le 20/01/2025 a une couverture valide de 2,72 % (profil de référence, minimum
  5 %) : `trend_use = excluded_low_coverage`. La série NDVI l'exclut et la paire
  qui l'utilise est qualifiée « non comparable ».
- WorldCover 2021 v200 (CC-BY-4.0), instantané OpenStreetMap du 22/10/2026
  (ODbL-1.0, attribution « © OpenStreetMap contributors »), mosaïques Sentinel-1
  de janvier 2025 VV/VH, avec leurs descripteurs, licences et limites.
- Non inclus : référentiels geoBoundaries et registre des territoires (la page
  « Territoires » reste sans données sur Cloud) ; les fichiers SCL intermédiaires
  de `data/interim/sentinel2_quality` (encodés avec perte, voir
  `lossless_aoi_clip.md`).

Provenance (`PACK_MANIFEST.json`) : commit du code qui a produit le paquet
(construction refusée si ce code n'est pas commité), empreinte des données
sources (hors Git : empreintes des manifestes et fichiers sources), versions des
logiciels, inventaire SHA-256 et taille de chaque fichier.

Construction : `python scripts/build_demo_pack.py` (aucun écrasement ; écrit le
dossier, l'archive ZIP déterministe et son `.sha256`).

Nombre de fichiers : le v2 comptait 30 fichiers. Le v3 (publié, épinglé dans
`RELEASED_PACK`) y ajoute les deux référentiels geoBoundaries (4 fichiers) et leurs
194 unités du registre des territoires (97 géométries + 97 enregistrements), soit
228 fichiers, 229 membres ZIP avec `PACK_MANIFEST.json`. Les « 30 fichiers » des
mesures ci-dessous concernent le v2 ; la liste « Non inclus » ci-dessus aussi
(le v3 contient les référentiels de limites).

## Paquet `geonaf-demo-estuaire-2025-01` v4 (lot 2, construit localement, non publié)

Trois carrés d'acquisition de 12 × 12 km (Akanda, Libreville, Owendo), jamais des
communes. Chaque territoire a ses propres extraits Sentinel-2 dans
`raw/sentinel2/<produit>__<aoi>/` ; `derivation.aoi` (identifiant et SHA-256 de
`aoi/<aoi>.geojson`) est vérifié par le filtre du lot 1 (`docs/multiterritoire.md`) :
un territoire ne voit jamais les extraits d'un autre.

| Territoire | Dates Sentinel-2 (couverture SCL valide, profil de référence) | Couches |
|---|---|---|
| Akanda | 31/01/2021 (72,93 %), 15/01/2025 (66,73 %), 20/01/2025 (2,72 %, exclue), 25/01/2025 (64,51 %) | WorldCover 2021, OSM 22/10/2026, S1 01/2025 VV/VH |
| Libreville | 31/01/2021 (57,84 %), 15/01/2025 (16,59 %), 25/01/2025 (57,60 %) | idem, fenêtres propres |
| Owendo | 31/01/2021 (49,17 %), 15/01/2025 (0,12 %, exclue), 25/01/2025 (48,27 %) | idem, fenêtres propres |

Owendo n'a aucune paire 2025 exploitable (couverture comparable 15→25/01 : 0,12 %) ;
seule la paire 31/01/2021 → 25/01/2025 (47,73 %) l'est. Les fenêtres externes de
chaque carré sont contrôlées à la construction : la couche doit couvrir l'AOI à un
pixel source près (WorldCover est calé sur sa grille de 1/12 000°).

Mesures (Windows, Python 3.10.11, données locales, 10/11/2026) :

| Mesure | v3 | v4 |
|---|---|---|
| Fichiers / membres ZIP | 228 / 229 | 295 / 296 |
| Décompressé | 34,2 Mo | 112,1 Mo (106,9 Mio) |
| Archive | 18,3 Mo | 59,4 Mo (56,7 Mio) |
| Taux de compression maximal | - | 8,9 |
| Installation à froid / réutilisation | 0,33 s / 0,07 s | 1,79 s / 0,43 s |

Seule la limite du nombre de membres a été relevée, de 256 à **320** (besoin mesuré
296, marge 8 %) ; taille d'archive (64 Mio), taille décompressée (160 Mio) et taux
(50) sont inchangés et tous les contrôles d'extraction restent appliqués.

Reproductibilité : `--created-at` fixe la date écrite dans les manifestes ; deux
constructions indépendantes du même code et des mêmes données donnent la même
archive au bit près. La construction depuis du code non commité est tracée
(`code.uncommitted_paths`) : l'archive à publier doit être reconstruite après le
commit du lot 2.

Validation scientifique : pour chaque territoire, chaque date et chaque paire, les
extraits donnent exactement les résultats des tuiles complètes (aucun écart, y compris
flottant). Référence Windows : `docs/benchmarks/pack_v4_science_windows_py310.json`
(`python scripts/diagnose_pack_science.py <paquet> --estuaire --json …`), vérifiée
sous Python 3.13.3 avec les dépendances figées ; la paire Akanda 15→25/01/2025 y est
identique au bit près à la référence v3. Tests : `tests/test_demo_pack_v4.py`
(`GEONAF_DEMO_PACK_V4_DIR=<paquet décompressé>`).

CI Linux à préparer après publication (lot 3, non fait) : dans `demo-pack-linux`,
installer le v4 par le chargeur puis exécuter `tests/test_demo_pack_v4.py` avec
`GEONAF_DEMO_PACK_V4_DIR`, en plus des contrôles v3 existants.

## Chargement (`src/data/demo_pack.py`, sans Streamlit)

1. Données locales prioritaires : si `GABON_GEOAI_DATA_DIR` (ou `data/`) contient
   des acquisitions, elles sont utilisées sans téléchargement ; un paquet décompressé
   désigné par cette variable est vérifié contre son manifeste.
2. Sinon, si un paquet est configuré, l'archive est téléchargée en HTTPS via la
   session TLS du projet (HTTP refusé, hôtes GitHub autorisés seulement, y compris
   après redirection), délais 10 s / 60 s, 64 Mio au plus.
3. SHA-256 de l'archive comparé à l'empreinte épinglée avant toute extraction.
4. Extraction en flux : chemins absolus, `..`, lecteurs, barres obliques inverses,
   liens, doublons, plus de 320 membres (256 jusqu'au lot 2, voir ci-dessous), plus de
   160 Mio décompressés ou un taux de compression > 50 sont refusés.
5. Chaque fichier est vérifié contre `PACK_MANIFEST.json` (taille, SHA-256, aucun
   fichier non déclaré), puis le dossier est installé par un `os.replace` atomique
   dans `~/.cache/geonaf` (ou `GEONAF_CACHE_DIR`).
6. Streamlit (`src/components/data_source.py`) résout le dossier une fois par
   processus (`st.cache_resource`) ; un échec n'est pas mis en cache. Après un
   redémarrage, l'installation sur disque est revérifiée et retéléchargée si elle
   a disparu ou est altérée.

Erreurs publiques : catégorie et message fixe (configuration, réseau, taille,
intégrité, archive, stockage), sans URL, chemin ni texte d'exception.

## Configuration après publication (non effectuée)

Soit renseigner `RELEASED_PACK` dans `src/data/demo_pack.py` (empreinte épinglée
dans Git, recommandé), soit définir dans les secrets Streamlit, au niveau racine :
`GEONAF_DEMO_PACK_URL`, `GEONAF_DEMO_PACK_SHA256`, `GEONAF_DEMO_PACK_ID`.

Déploiement public : définir aussi `GEONAF_DEMO_READ_ONLY=1` (aucun téléchargement ni écriture
déclenchés par les visiteurs dans le dossier partagé, voir `demo_read_only.md`).

## Mesures du 26/10/2026 (Windows, sans réseau)

| Mesure | Valeur |
|---|---|
| Archive | 17,7 Mo (32,7 Mo décompressés, 30 fichiers) |
| Installation à froid, hors téléchargement | 0,33 s |
| Réutilisation avec revérification complète | 0,07 s |
| Analyse de la paire 15→25 sur le paquet | 3,7 s (4,2 s sur tuiles complètes) |

Le temps de téléchargement depuis l'hébergement n'est pas mesuré (archive non publiée).

## Environnement

`requirements.txt` fige les versions validées (Rasterio 1.4.4, GDAL 3.10.3 embarqué ;
pas de Rasterio 1.5). Wheels Linux manylinux x86_64 disponibles pour Python 3.12
et 3.13. Suite complète réussie sous Windows avec Python 3.10.11 et 3.13.3.
La wheel Linux de Rasterio embarque PROJ 9.7.1 (9.6.0 sous Windows).

Validation Linux (workflow `demo-pack-linux`, Ubuntu 24.04, Python 3.13.16,
Rasterio 1.4.4 / GDAL 3.10.3, 26/10/2026) : téléchargement réel de la release par
le chargeur (0,97 s), puis comparaison composant par composant avec la référence
Windows (`docs/benchmarks/pack_science_windows_py310.json`, script
`scripts/diagnose_pack_science.py`) :

- identiques bit à bit : NDVI des deux dates, ΔNDVI, masques de validité et de
  comparaison, carte de classes, grille, effectifs par classe, statistiques ΔNDVI ;
- différences de dernier bit sur les surfaces pondérées en aire égale et la
  couverture comparable (écart relatif ≤ 6·10⁻¹⁵, soit < 10⁻⁶ m² sur 84 km²) :
  sommes flottantes dont l'arrondi dépend de la bibliothèque mathématique.

Politique de tolérance (`tests/test_demo_pack_science.py`) : égalité exacte pour
tableaux, masques, classes, grille, effectifs et statistiques ; tolérance relative
de 1·10⁻¹² pour surfaces et couverture. L'empreinte globale exacte n'est exigée que
sur la plateforme de référence (Windows).

## Mobile

Cartes MapLibre : hauteur adaptée à l'écran sous 640 px, boutons tactiles de 40 px,
gestes à deux doigts sur écran tactile uniquement (le défilement de la page reste
possible), carte NDVI en hauteur « content ». Vérifié avec Playwright en
390 × 844 : aucun débordement horizontal, carte temporelle en une colonne.
Non vérifié sur un iPhone réel.
