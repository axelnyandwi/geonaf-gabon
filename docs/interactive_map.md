# Carte NDVI interactive V0.2

## Lancement local

Streamlit 1.65 ou supérieur est requis pour Components v2. Depuis la racine du dépôt, avec les acquisitions locales vérifiées déjà disponibles :

```powershell
.venv/Scripts/python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false --theme.base dark
```

La première itération affiche le NDVI de la deuxième acquisition (25 janvier 2025 pour la paire Akanda du 15/25 janvier). Les dates, statistiques, surfaces et métadonnées existantes restent affichées. Décocher « Carte NDVI interactive (date 2) » retrouve les trois cartes Matplotlib. Une erreur MapLibre/WebGL déclenche également ce repli. Le navigateur doit prendre en charge WebGL ; aucun fond de carte externe n'est requis. Le carré de 12 × 12 km n'est pas une limite administrative. Aucune comparaison par curseur n'est encore intégrée.

## Modules et contrat public

- `src/services/map_layer.py` transforme le résultat scientifique existant en charge utile publique `geonaf.ndvi-map/v1`, sans dépendre de Streamlit ni écrire de raster.
- `src/components/maplibre/map.js` exporte `mountNdviMap(container, payload, maplibre, onError)`. Il reçoit le moteur MapLibre et retourne `{map, dispose}` ; un futur composant React peut utiliser cette même fonction et appeler `dispose` au démontage.
- `src/components/interactive_map.py` adapte ce module à Components v2. Il charge les ressources locales et met en cache la préparation des couches.
- `app.py` orchestre l'affichage et conserve le repli Matplotlib.

La charge utile contient un PNG encodé, ses quatre coins longitude/latitude (nord-ouest, nord-est, sud-est, sud-ouest), les géométries publiques AOI/emprise sans attributs sources, la date, les CRS et la légende. Aucun chemin, manifeste d'acquisition ou secret n'est transmis. Les coordonnées affichées au survol sont des coordonnées géographiques ; aucune valeur NDVI ponctuelle n'est interpolée dans le navigateur.

MapLibre GL JS 5.24.0 est livré dans `vendor/` avec sa licence BSD et sa provenance (intégrité du paquet npm et empreintes des fichiers). Le bundle autonome inclut son worker. Aucun CDN, serveur de tuiles, service de styles ou police distante n'est utilisé. Les ressources Streamlit sont également servies localement. Redémarrer Streamlit après une modification des sources JavaScript/CSS : leur chargement est mis en cache par processus.

## Géoréférencement, masques et cache

Seule une copie d'affichage est reprojetée en EPSG:3857, limitée à 1536 pixels sur son plus grand côté. Valeurs et catégories utilisent le plus proche voisin pour éviter le mélange de valeurs à travers les masques. La source image MapLibre reçoit les coins de cette grille convertis en EPSG:4326 et emploie aussi le plus proche voisin.

Les catégories distinguent les pixels valides de la date 2 (palette RdYlGn, échelle fixe −1 à +1), les pixels masqués dans l'AOI (gris) et l'extérieur (transparent). Le masque AOI reprend la convention de rasterisation scientifique existante. Le contour AOI est blanc ; l'emprise raster est bleue discontinue. La couche représente l'observation de la date 2, pas exclusivement l'intersection des observations des deux dates ; la couverture comparable reste celle des statistiques temporelles existantes.

Le service exige la politique `usable_land` (SCL 4/5). Il réutilise les NDVI et masques déjà calculés : aucune nouvelle calibration, classification ou statistique. Les résultats natifs et exports ne sont pas arrondis ou rééchantillonnés. La clé du cache d'affichage inclut la version d'affichage, la taille et la clé d'analyse existante (acquisitions, empreintes des fichiers, AOI, politique SCL et paramètres). Zoom, déplacement, coordonnées et visibilité sont gérés entièrement dans JavaScript, sans appel au calcul scientifique. Seules les erreurs du moteur remontent à Streamlit pour activer le repli.

## Vérifications réalisées

```powershell
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests/test_map_layer.py tests/test_local_preview.py tests/test_cloud_mask.py tests/test_ndvi_canonical.py tests/test_temporal.py tests/test_temporal_acquisition_integration.py tests/test_change_analysis.py tests/test_geotiff_export.py tests/test_quality.py
node --test tests/frontend/test_ndvi_map.mjs
.venv/Scripts/python.exe -B scripts/benchmark_map_layer.py
git diff --check
```

Résultats : 123 tests Python et 2 tests JavaScript réussis. Les 947 avertissements Python concernent les multiplications Affine dont Rasterio annonce la dépréciation. Les tests couvrent orientation, coins géographiques, passage UTM/Mercator, catégories de masque, absence de chemins/attributs privés, conservation des tableaux sources, cache, interactions et repli.

L'application a été lancée en thème sombre avec les acquisitions locales réelles d'Akanda. Le navigateur a affiché la couche, le contour, la légende et les statistiques. L'audit des ressources n'a trouvé aucune requête HTTP(S) hors du serveur local. Après passage du navigateur hors ligne, zoom, déplacement, coordonnées, visibilité et recentrage fonctionnaient, sans nouvelle requête de ressource. Cette coupure intervient après le chargement initial : ce n'est pas un test de démarrage hors ligne à cache navigateur vide. Une erreur du moteur injectée a fait apparaître le repli Matplotlib sans exception applicative.

Mesures ponctuelles locales, sans garantie de latence sur une autre machine :

| Étape | Durée |
|---|---:|
| Analyse scientifique locale existante | 23,506 s |
| Préparation de la copie d'affichage | 0,586 s |
| Premier appel au cache d'affichage, préparation incluse | 0,592 s |
| Deuxième appel, même clé | 0,0027 s |
| Montage navigateur → premier état MapLibre stable | 0,531 s |
| Navigation initiale → premier état stable, calcul et transfert inclus | 34,521 s |

L'image réelle fait 1197 × 1205 pixels ; la charge utile JSON mesure 2 249 249 octets. Les ressources cartographiques locales représentent 1 133 403 octets. La préparation mesurée dans le navigateur était de 0,552 s lors d'un lancement distinct du benchmark Python. Les interactions hors ligne groupées ont pris 0,649 s jusqu'à stabilisation ; ce chiffre ne mesure pas une fréquence d'images.

L'empreinte SHA-256 des résultats scientifiques avant/après préparation est identique à la référence V0.1 : `b0356911623b4af85939f9a5bb62f0f01cdff9ffd1dae875ca84b77523cb0e41`. Cela vérifie la non-régression numérique sur cette paire, sans constituer une validation scientifique indépendante des observations ou de leurs interprétations. Les variations NDVI ne démontrent pas une déforestation.
