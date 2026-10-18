# Export GeoTIFF des changements temporels

`src.services.geotiff_export.export_temporal_change_geotiff` sérialise un
`TemporalVegetationChangeResult` existant. Le module ne recalcule ni NDVI,
classes, couverture, surfaces, ni comparabilité saisonnière.

L'export produit un raster de classes (`uint8`, codes définis par
`ChangeClass`), un raster ΔNDVI (`float32`, date 2 moins date 1) et un
manifeste JSON compagnon. Le GeoTIFF porte des tags courts et stables
(identifiants produits, dates UTC, relation saisonnière, statut, compte
d'avertissements et nom du manifeste). Les chemins, contextes saisonniers
complets avec source/version, évaluation et avertissements, politique SCL,
calibration, grille, indicateurs, paramètres et provenance sont dans le JSON.
Les rasters et le manifeste ne remplacent pas les sources.

## Saisons

Le manifeste distingue `unknown`, `different` et `same`. Les contextes
d'acquisition sont conservés séparément, y compris quand l'un manque. La
relation `same` ne signifie jamais que les observations sont comparables.
Une évaluation `not_comparable` ou un avertissement reste descriptif ;
l'export ne rejette ni ne reclasse le résultat. Aucun calendrier, seuil,
classificateur climatique ou comportement propre au Gabon n'est codé.

## AOI, masque raster et surfaces

Le masque GeoTIFF est l'intersection de la comparabilité déjà calculée par le
service et de l'AOI rasterisée avec `all_touched=True` : une cellule est
représentée dès qu'elle touche l'AOI. Les cellules invalides/non comparables
ont un masque GDAL nul ; leur valeur de classe est `0` et leur ΔNDVI est
`NaN` (NoData). Les tags indiquent ces conventions.

Cette représentation par cellules entières ne mesure pas la surface. Avec une
AOI, les statistiques descriptives et `class_pixel_counts` portent sur les
pixels comparables sélectionnés par la même règle `all_touched=True` que
l'export ; chaque cellule touchant l'AOI compte une fois, quelle que soit sa
fraction couverte. Les pixels extérieurs à cette emprise ne contribuent plus
à ces indicateurs. Sans AOI, ils conservent leur périmètre historique : toute
la grille comparable.

Les surfaces et couvertures utilisent une mesure différente : chaque cellule
contribue selon l'intersection exacte AOI-cellule en aire égale. Ainsi, une
cellule partielle compte comme un pixel entier dans les comptes et statistiques
par pixel, mais sa surface ne contribue qu'à hauteur de sa fraction dans l'AOI.
`exported_pixel_counts` et `analysis_indicators.class_pixel_counts` concordent
pour les pixels comparables écrits ; le compte des statistiques (`count`) suit
la même sélection. Les surfaces ne sont pas déduites de ces comptes.

## Tags et limites

Les tags contiennent seulement des valeurs de synthèse, chacune limitée à
256 caractères ; les valeurs plus longues y sont tronquées. La valeur
complète reste dans le manifeste JSON pour éviter de dépendre des limites de
taille/type des tags GeoTIFF/GDAL. Les
sorties existantes sont refusées pour éviter tout écrasement. L'export est
indépendant de Streamlit et ne contient aucune règle régionale.
