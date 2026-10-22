# Sources de données

Sources intégrées ou préparées :

- Copernicus Sentinel-2 L2A (CDSE, STAC + S3) : ingestion opérationnelle, voir
  `acquisition_catalogue.md` et `acquisition_downloads.md`.
- ESA WorldCover 10 m v200 (CC BY 4.0) : fenêtre COG sur l'AOI par HTTP Range,
  plafonnée et mise en cache ; voir `multisource_demo.md`.
- OpenStreetMap via Overpass (ODbL 1.0, partage à l'identique) : requête bornée,
  instantanés mis en cache 7 jours ; voir `multisource_demo.md`.
- Copernicus Sentinel-1 Global Mosaics IW (CDSE, avis légal Copernicus) : mosaïques
  mensuelles VV/VH 20 m, fenêtre AOI par S3 Range avec estimation préalable et plafond ;
  voir `multisource_demo.md`.

Attributions obligatoires à afficher avec toute carte ou export : « Contains modified
Copernicus Sentinel data [année] », « © ESA WorldCover project / Contains modified
Copernicus Sentinel data processed by ESA WorldCover consortium », « © OpenStreetMap
contributors ». Toute base dérivée d'OSM diffusée doit rester sous ODbL.

Avant chaque ingestion, documenter :

- le fournisseur et l’URL officielle ;
- la licence et les conditions de réutilisation ;
- la couverture spatiale et temporelle ;
- le format et le système de coordonnées ;
- la fréquence de mise à jour et la version de la source ;
- la date d’acquisition et les contrôles de qualité ;
- les limites connues et les éventuelles informations sensibles.

Les secrets d’accès ne doivent jamais figurer dans ce document.
