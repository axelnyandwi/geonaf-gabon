# Radiométrie des mosaïques Sentinel-1 CDSE — décision du 22/10/2026

Produit : `sentinel-1-global-mosaics`, type `S1SAR_L3_IW_MCM` (mosaïque mensuelle IW,
VV/VH, 20 m). Constante de code : `src/data/sentinel1.py::RADIOMETRY`
(`s1-mosaic-radiometry-1`).

## Ce que disent les fichiers (vérifié sur 2025-01, tuile 32NNF)

| Élément | Valeur |
|---|---|
| Type | float32, COG DEFLATE, prédicteur 3, blocs 256², aperçus 2/4/8/16 |
| NoData | −32768 (balise GeoTIFF et STAC) |
| Facteur / décalage | 1 / 0 ; aucune unité ni description de bande |
| `userdata.json` | nom, dates, emprise, centre de traitement ; aucune information radiométrique |
| STAC | `raster:bands` absent ; titres d'actifs « Amplitude of … polarized signal » |

Les fichiers seuls ne permettent donc pas de conclure.

## Sources documentaires

1. Annonce CDSE rédigée par Sinergise, producteur des mosaïques (06/08/2024) :
   mosaïques issues de « terrain-flattened gamma nought backscatter », moyenne pondérée
   mensuelle, échelle linéaire « not optimal for visualizing SAR polarimetric
   intensity ». <https://dataspace.copernicus.eu/node/1621>
2. Forum CDSE, réponse de G. Milčinski (Sinergise, 28/05/2025) : « Values are in linear
   units » ; l'exemple de conversion `ln(x)·0,2171 + 1` vaut `10·log10(x)/20 + 1`, soit
   une conversion de **puissance** en dB. Le calque du navigateur CDSE s'appelle
   `VH-LINEAR-GAMMA0`.
   <https://forum.dataspace.copernicus.eu/t/sentinel-1-monthly-mosaics-linear-or-db/3757>
3. Algorithme CDSE : calibration β⁰, bruit thermique, correction radiométrique de terrain
   par intégration d'aires, Range-Doppler, ombres exclues.
   <https://documentation.dataspace.copernicus.eu/Data/Others/Sentinel1_Mosaic_Algorithm.html>

## Décision

- Grandeur : **γ⁰ aplani du relief (γ⁰_T), puissance linéaire** ; **dB = 10·log10**.
- Masques : NoData −32768, NaN (ombre persistante), valeurs ≤ 0 exclues avant le log.
- VV et VH sont traitées séparément, sans rapport ni combinaison en V1.
- La plausibilité des valeurs (eau VH ≈ −30 dB, forêt VV ≈ −6,6 dB à Akanda) est un
  contrôle de cohérence, **pas** la justification.

## Incertitudes conservées

- Les titres STAC « Amplitude » contredisent le producteur ; ils ne sont pas retenus,
  mais l'incohérence est affichée.
- La source 2 est un message de forum, non une spécification formelle.
- Aucune calibration indépendante (cibles, comparaison GRD traité localement) : les dB
  restent des **valeurs descriptives**, jamais qualifiées de validées. Une convention non
  documentée (ex. « amplitude ») reste calculable pour comparaison mais est étiquetée
  « dB expérimentaux ».
- Composite mensuel : pas d'instant, d'angle d'incidence ni d'orbite unique par pixel.
