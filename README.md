# GEONAF

## Prototype Streamlit local V0.1 — Akanda

Depuis la racine du dépôt, avec l’environnement installé :

```powershell
.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
```

Ouvrir `http://127.0.0.1:8501`. Arrêter avec `Ctrl+C`.
Le prototype lit uniquement les acquisitions locales vérifiées des 15 et
25 janvier 2025 et `data/aoi/akanda.geojson` (ou le répertoire de données
configuré). Chaque produit doit posséder son `acquisition_manifest.json`,
ses bandes B04/B08, sa SCL et son XML BOA. Les empreintes SHA-256 sont
vérifiées avant calcul. Aucun accès réseau aux données ni contournement TLS.

Les sélecteurs affichent les dates d’acquisition UTC présentes dans les
manifestes ; aucun moteur de sélection supplémentaire. Le bouton affiche
la paire sélectionnée sans recalcul si ses entrées sont inchangées. Le
résultat scientifique reste en session ; acquisitions, AOI, SCL et seuil
participent à la clé d’invalidation. Seuls les PNG sont partagés via
`st.cache_data` (quatre entrées en mémoire). Le détail des clés, des limites
et des mesures est dans [le diagnostic de performance](docs/streamlit_performance.md).
Les deux NDVI utilisent la même échelle [−1, 1], le delta
une échelle fixe symétrique [−2, 2] ; le gris représente les observations
inexploitables ou non comparables. Politique quantitative : SCL 4 et 5.

L’AOI est un carré d’acquisition de 12 × 12 km, pas la limite administrative
d’Akanda. Les statistiques décrivent les pixels comparables ; les surfaces
sont pondérées par leur intersection avec l’AOI. Une variation de NDVI ne
démontre pas une déforestation. Les limites atmosphériques, saisonnières et
de représentativité restent applicables. Aucun export de provenance contenant
des chemins locaux n’est proposé dans cette interface.

**Observer · Comprendre · Prédire · Simuler**

GEONAF est une plateforme Python d’intelligence géospatiale destinée à observer, comprendre, prédire et simuler les phénomènes territoriaux. Le MVP porte sur Libreville, Akanda et Owendo, avec l’ambition de pouvoir s’étendre à l’échelle africaine.

Le socle actuel traite des acquisitions Copernicus Sentinel-2 : découverte et ingestion, préparation et découpage raster, contrôle qualité SCL, calibration radiométrique et calcul d’indices comme le NDVI. Sentinel-1 est prévu pour une intégration future. L’application Streamlit est une interface de travail ; les traitements métier sont organisés dans `src/` afin de pouvoir être réutilisés par une interface ou un service ultérieur.

## Orientations du projet

Les évolutions visées couvrent notamment :

- l’exploration et la visualisation territoriales ;
- l’analyse d’images satellite et le suivi des changements ;
- la détection des changements ;
- la recherche de similarités géospatiales ;
- la prévision de transformations territoriales ;
- l’expérimentation avec des modèles d’apprentissage automatique et des modèles de fondation GeoAI.

## Installation et démarrage

Python **3.10.11** est la version de référence. Exécuter les commandes depuis la racine du dépôt.

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Sous Windows, créer l’environnement avec `py -3.10 -m venv .venv`, puis l’activer dans PowerShell avec `.venv\Scripts\Activate.ps1`.

L’application est accessible à l’adresse locale affichée par Streamlit, généralement `http://localhost:8501`.

## Configuration

Aucun secret ni service externe n’est nécessaire au démarrage. Les constantes générales et les chemins des données sont définis dans `src/config.py`.

La variable d’environnement optionnelle `GABON_GEOAI_DATA_DIR` permet de choisir un autre dossier de données. Un chemin relatif est résolu depuis la racine du projet. Par défaut, le dossier `data/` est utilisé.

`.env.example` documente cette variable. L’application ne charge pas automatiquement les fichiers `.env`. Définir la variable dans le terminal avant de démarrer Streamlit, par exemple :

```bash
export GABON_GEOAI_DATA_DIR=data
python -m streamlit run app.py
```

Sous PowerShell, utiliser `$env:GABON_GEOAI_DATA_DIR = "data"` depuis la racine du projet.

Ne jamais versionner de secrets. Les fichiers `.env` et les secrets Streamlit sont ignorés par Git.

## Architecture du dépôt

```text
.
|-- README.md
|-- .gitignore
|-- .env.example
|-- requirements.txt
|-- requirements-dev.txt
|-- app.py
|-- src/
|   |-- __init__.py
|   |-- config.py
|   |-- diagnostics.py
|   |-- data/
|   |   |-- __init__.py
|   |   |-- ingestion.py
|   |   |-- cloud_mask.py
|   |   |-- preparation.py
|   |   |-- quality.py
|   |   |-- radiometric_calibration.py
|   |   `-- raster_processing.py
|   |-- geo/
|   |   |-- aoi.py
|   |   `-- vegetation_indices.py
|   |-- features/__init__.py
|   |-- models/
|   |   |-- __init__.py
|   |   |-- training.py
|   |   |-- prediction.py
|   |   `-- evaluation.py
|   |-- services/__init__.py
|   `-- components/__init__.py
|-- data/
|   |-- raw/.gitkeep
|   |-- interim/.gitkeep
|   `-- processed/.gitkeep
|-- notebooks/
|   |-- .gitkeep
|   `-- README.md
|-- tests/
|   |-- .gitkeep
|   |-- test_config.py
|   `-- fixtures/.gitkeep
|-- scripts/                # outils manuels et diagnostics
`-- docs/data_sources.md
```

- `app.py` : point d’entrée de l’interface Streamlit.
- `src/config.py` : configuration générale et chemins, sans création automatique de dossiers.
- `src/data/` : découverte et ingestion, préparation raster, calibration et contrôle qualité.
- `src/geo/` : validation des AOI et calculs géospatiaux, dont les indices spectraux.
- `src/features/` et `src/models/` : espaces séparés pour les futures variables et analyses prédictives.
- `src/services/` : orchestration applicative à développer, indépendante de l’interface.
- `src/components/` : composants de l’interface Streamlit.
- `data/raw/` : données sources conservées sans modification.
- `data/interim/` : résultats intermédiaires des traitements.
- `data/processed/` : données préparées pour les analyses.
- `notebooks/` : expérimentations et exploration.
- `tests/` : tests automatisés, construits principalement avec des données géospatiales synthétiques.
- `docs/data_sources.md` : provenance, licences et limites des futures sources.

Les scripts de `scripts/` servent principalement à l’expérimentation et au diagnostic manuel ; ils ne sont pas tous des étapes requises du pipeline applicatif. L’ingestion Sentinel-2 s’appuie sur les interfaces Copernicus configurées dans le module de données. Les dossiers de données restent séparés du code afin de conserver les sources et les produits dérivés hors des commits ordinaires.

## Indicateurs de qualité SCL

Dans `calculate_scl_quality`, `total_pixels` désigne les pixels observés : pixels non masqués dont la classe SCL n’est pas 0. `invalid_percent` est calculé parmi ces observations, en comptant les classes nuage/ombre, défectueuses et neige/glace selon les ensembles de classes définis dans le code. Les pixels masqués et la classe 0 sont exclus du numérateur et du dénominateur ; l’eau reste une observation valide. Les métriques terrestres utilisent un dénominateur sans eau.

Ces définitions diffèrent des anciens calculs qui incluaient les pixels NoData dans `total_pixels` et dans le dénominateur d’`invalid_percent`. Les valeurs historiques ne sont donc pas directement comparables : les séries doivent être recalculées avec la même version de la méthode avant d’interpréter leurs écarts. Cette clarification documente les indicateurs existants et ne change pas la règle de classement des produits.

## Dépendances

`requirements.txt` déclare les bibliothèques utilisées par l’application et le pipeline géospatial : NumPy, Rasterio, GeoPandas, Shapely, pyproj, Matplotlib et les clients requis par l’ingestion. Des plages de versions bornent les versions majeures sans verrouiller les dépendances transitives.

Pour les tests et les scripts expérimentaux de géocodage/cartographie, installer aussi les dépendances de développement :

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q -p no:cacheprovider
```

La configuration pytest à la racine limite la collecte automatique à `tests/`. Les scripts `scripts/test_*.py` restent des outils manuels et ne sont pas importés pendant la collecte.

## Données et expérimentations

Tout le contenu de `data/` est ignoré, à l’exception des fichiers `.gitkeep`. Les principaux formats de données géospatiales, d’archives et de modèles sont également ignorés dans le reste du dépôt. Ne pas forcer leur ajout avec Git sans évaluer leur taille et leur pertinence.

Les notebooks peuvent être versionnés. Retirer leurs sorties volumineuses et toute information sensible avant de les ajouter ; leurs dossiers de sauvegarde automatique sont ignorés.

## Vérifications de la fondation

Après installation, vérifier la cohérence des dépendances et la syntaxe Python :

```bash
python -m pip check
python -m compileall -q app.py src
```

Le démarrage avec `python -m streamlit run app.py` doit afficher le titre, la devise et le message de recherche et de conception. Pour vérifier la configuration, installer les dépendances de développement et exécuter les tests :

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Vérifier également les exclusions et le contenu préparé pour commit :

```bash
git check-ignore .env .venv/exemple data/raw/exemple.csv artifacts/exemple.bin
git status --short
git diff --cached --check
```

La première commande doit afficher les quatre chemins ignorés. `.env.example` et les fichiers `.gitkeep` doivent rester versionnables. `.gitignore` ne bloque pas les fichiers déjà suivis ni les ajouts forcés et ne filtre pas selon la taille : relire la liste des fichiers avant tout commit. Conserver les artefacts dans les dossiers ignorés et ne jamais ajouter de secrets.

Aucun commit n’est nécessaire pour effectuer ces contrôles.
