# GEONAF — paquet de démonstration expérimental Akanda, janvier 2025 (v3)

**Démonstration expérimentale. Résultats exploratoires, non validés ; aucune valeur
juridique ni opérationnelle.** Ce paquet alimente l'application GEONAF de
préproduction sur Streamlit Community Cloud.

- Archive : `geonaf-demo-akanda-2025-01-v3.zip`, 228 fichiers + `PACK_MANIFEST.json`.
- SHA-256 de l'archive : voir `geonaf-demo-akanda-2025-01-v3.zip.sha256` (empreinte
  également épinglée dans `src/data/demo_pack.py`).
- Code ayant produit le paquet : commit `b9cd50a` (branche `deploy/cloud-demo-akanda`).
- Provenance détaillée : `PACK_MANIFEST.json` (empreintes de chaque fichier, sources,
  versions logicielles, empreinte des données sources) et un `subset_manifest.json`
  par acquisition (empreintes des tuiles parentes et des extraits).

## Zone

Carré d'acquisition historique de 12 × 12 km autour d'Akanda (Gabon). **Ce n'est ni
la commune d'Akanda ni une autre unité administrative.**

## Sources, licences et attributions

| Données | Dates | Licence | Attribution |
|---|---|---|---|
| Sentinel-2 L2A, tuile T32NNF : extraits B04, B08, SCL, XML de calibration | acquisitions des 15, 20 et 25/01/2025 ; ingestion 16 et 22/10/2026 | Avis juridique des données Copernicus Sentinel : accès libre ; reproduction, distribution, communication au public, adaptation et modification autorisées | « Contains modified Copernicus Sentinel data 2025 » |
| Sentinel-1 Global Mosaics, IW, VV/VH (fenêtres Akanda) | composite mensuel janvier 2025 ; extrait le 22/10/2026 | Avis juridique des données Copernicus Sentinel | « Contains modified Copernicus Sentinel data 2025 — Sentinel-1 Global Mosaics, Copernicus Data Space Ecosystem » |
| ESA WorldCover 10 m 2021 v200 (fenêtre Akanda) | carte annuelle 2021 ; extrait le 22/10/2026 | CC BY 4.0 | « © ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium » ; Zanaga et al. (2022), doi:10.5281/zenodo.7254221 |
| OpenStreetMap (extrait Overpass, carré Akanda) | base du 22/10/2026 16:57 UTC | ODbL 1.0 | « © OpenStreetMap contributors » — https://www.openstreetmap.org/copyright. Cet extrait est une base de données dérivée : il est redistribué sous ODbL 1.0 |
| geoBoundaries gbOpen GAB ADM2 | limites représentant 2018 ; build du 12/12/2023 ; récupéré le 23/10/2026 | CC BY 3.0 | geoBoundaries (Runfola et al.) ; source : Wikimedia |
| geoBoundaries gbHumanitarian GAB ADM2 | limites représentant 2015 ; build du 12/12/2023 ; récupéré le 23/10/2026 | CC BY 3.0 IGO | geoBoundaries ; source : Second Administrative Level Boundaries (SALB), HDX / OCHA COD-AB |

## Limites scientifiques

- Extraits sans rééchantillonnage de tuiles dont l'intégrité a été vérifiée contre
  STAC à l'ingestion ; cette vérification n'est pas recontrôlable depuis l'extrait seul.
- Résultats identiques bit à bit sur tuiles complètes et extraits (rapport du
  26/10/2026), après correction du découpage AOI avec perte (`docs/lossless_aoi_clip.md`).
- 20/01/2025 : couverture valide 2,72 % (minimum du profil : 5 %) ; exclu des
  tendances, paires qualifiées « non comparables ».
- Une variation du NDVI ne démontre pas une déforestation (nuages résiduels,
  saisonnalité, humidité, marées, saturation). Statut : exploratoire, non validé.
- WorldCover 2021 et l'instantané OSM ne décrivent pas l'état du territoire aux dates
  Sentinel-2 ; l'absence d'un objet OSM ne prouve pas son absence sur le terrain.
- Limites geoBoundaries : référentiels communautaire et humanitaire, non officiels,
  non vérifiés, sans valeur juridique. Une divergence entre eux n'est pas un
  changement du territoire. Aucune limite administrative officielle n'est fournie.
- Aucune donnée postérieure au 25/01/2025 : les dates affichées sont celles des
  acquisitions, pas d'une observation récente.
