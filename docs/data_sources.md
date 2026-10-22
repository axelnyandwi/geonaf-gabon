# Sources de données

Sources intégrées ou préparées :

- Copernicus Sentinel-2 L2A (CDSE, STAC + S3) : ingestion opérationnelle, voir
  `acquisition_catalogue.md` et `acquisition_downloads.md`.
- ESA WorldCover 10 m (CC BY 4.0) et OpenStreetMap (ODbL 1.0) : adaptateurs préparés,
  sans récupération automatique, voir `multisource.md`.

Avant chaque ingestion, documenter :

- le fournisseur et l’URL officielle ;
- la licence et les conditions de réutilisation ;
- la couverture spatiale et temporelle ;
- le format et le système de coordonnées ;
- la fréquence de mise à jour et la version de la source ;
- la date d’acquisition et les contrôles de qualité ;
- les limites connues et les éventuelles informations sensibles.

Les secrets d’accès ne doivent jamais figurer dans ce document.
