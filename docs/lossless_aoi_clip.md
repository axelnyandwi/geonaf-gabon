# Correctif du 26/10/2026 : découpage AOI sans perte

## Défaut

`clip_raster_to_aoi` (`src/data/preparation.py`) recopiait le profil du raster
source pour écrire le découpage. Sur une tuile Sentinel-2 au format JPEG 2000,
le découpage intermédiaire était donc réécrit par le pilote GDAL `JP2OpenJPEG`,
dont l'encodage par défaut est **avec perte** (`COMPRESSION_REVERSIBILITY=LOSSY`),
malgré l'extension `.tif` du fichier.

Mesure sur B04 du 15/01/2025, AOI Akanda : 1 275 716 pixels sur 1 442 401
(88 %) modifiés, écart maximal 21 comptes numériques (0,0021 de réflectance),
écart absolu moyen 2,95. Le SCL, dont les valeurs sont des **classes**, était
lui aussi altéré : des pixels changeaient de classe, donc de masque.

Le défaut a été mis en évidence en comparant les tuiles complètes aux extraits
du paquet de démonstration : les extraits (GeoTIFF) suivaient un chemin sans
perte, et les pixels des extraits étaient identiques aux fenêtres des tuiles.

## Portée

- Concernés : toutes les analyses calculées sur tuiles JP2 complètes —
  NDVI d'une date, paire temporelle, série NDVI, cartes interactives, exports
  GeoTIFF, jointures WorldCover — et le contrôle qualité SCL de `src/data/quality.py`.
- Non concernés : indicateurs territoriaux (WorldCover, OSM), Sentinel-1,
  rasters déjà en GeoTIFF.
- Fichiers déjà écrits par l'ancien code et non modifiés : les 25 fichiers
  `data/interim/sentinel2_quality/**/SCL_20m_AOI.tif` sont du JPEG 2000 avec
  perte ; l'application ne les lit pas. À régénérer si ces statistiques servent.
- Même défaut, hors du chemin de l'application et non corrigé ici :
  `clip_raster_to_bbox` (utilisée par `clip_rgb_bands`).

## Correction

Le découpage est désormais toujours écrit en GeoTIFF DEFLATE (sans perte), quel
que soit le format source. Type de données, CRS, transformation affine, nodata
et étiquettes du jeu de données sont repris explicitement de la source. Aucun
autre traitement n'est modifié : `rasterio.mask` (fenêtre, `all_touched`,
remplissage) est inchangé.

## Impact sur les résultats affichés (Akanda, profil de référence)

| Résultat | Avant (avec perte) | Après (sans perte) |
|---|---|---|
| Couverture valide 15/01 | 66,18 % | 66,73 % |
| Couverture valide 20/01 | 2,69 % | 2,72 % |
| Couverture valide 25/01 | 63,78 % | 64,51 % |
| Paire 15→25 : pixels comparables | 827 865 | 845 383 |
| Paire 15→25 : couverture comparable | 57,40 % | 58,61 % |
| Paire 15→25 : ΔNDVI moyen | −0,03283 | −0,03287 |
| Paire 15→25 : baisse / sous le seuil / hausse (ha) | 1 138,8 / 6 783,9 / 348,8 | 1 175,8 / 6 901,6 / 369,1 |
| Empreinte scientifique de la paire | `b0356911…cb0e41` | `995a1dbf…4e073b` |

Le statut scientifique est inchangé (exploratoire, non validé). Le 20/01 reste
sous le minimum de couverture du profil (5 %) et exclu des séries.

Les valeurs Sentinel-2 citées dans les documents antérieurs au 26/10/2026
proviennent de l'ancien découpage.

## Validation

- Égalité bit à bit, sans tolérance, entre tuiles complètes et extraits du paquet
  de démonstration : NDVI des 3 dates, 3 paires et leurs cartes, série de 3 dates,
  indicateurs territoriaux (`tests/test_demo_pack_regression.py`,
  rapport `docs/benchmarks/demo_pack_validation_after_fix_2026-10-26.json`).
- SCL réel : découpage identique à la lecture directe du JP2, mêmes classes et mêmes
  effectifs par classe, pour les 3 dates.
- Tests synthétiques : source JP2 réversible (réflectance uint16, classes SCL uint8)
  et source GeoTIFF float32 avec nodata ; pilote, compression, type, CRS,
  transformation et nodata vérifiés.
- Références de `tests/test_akanda_multisource_regression.py` mises à jour : chaque
  nouvelle valeur est identique sur tuiles complètes et sur extraits, qui n'ont jamais
  subi de réencodage.
- Limite : validation sous Windows, Python 3.10.11, Rasterio 1.4.4, GDAL 3.10.3 ;
  à rejouer sous Linux.
