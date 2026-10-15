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

Cette représentation par cellules entières ne mesure pas la surface. Les
surfaces, couvertures et indicateurs reproduits dans le manifeste restent
ceux du calcul métier : les intersections exactes AOI-cellule sont pondérées
en aire égale. Une cellule partiellement couverte compte comme un pixel
représenté dans le GeoTIFF, mais ne contribue aux surfaces qu'à hauteur de sa
fraction dans l'AOI. `exported_pixel_counts` décrit les pixels valides écrits
dans l'emprise AOI ; `analysis_indicators.class_pixel_counts` reste le compte
calculé par l'analyse sur sa grille complète. Ces comptes peuvent différer.

## Tags et limites

Les tags contiennent seulement des valeurs de synthèse, chacune limitée à
256 caractères ; les valeurs plus longues y sont tronquées. La valeur
complète reste dans le manifeste JSON pour éviter de dépendre des limites de
taille/type des tags GeoTIFF/GDAL. Les
sorties existantes sont refusées pour éviter tout écrasement. L'export est
indépendant de Streamlit et ne contient aucune règle régionale.
