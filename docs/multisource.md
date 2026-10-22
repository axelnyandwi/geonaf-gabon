# Préparation multisource — lot 3

## Contrat (`src/data/external_sources.py`)

`ExternalLayerDescriptor`, immuable, décrit une couche externe :

| Champ | Contenu |
|---|---|
| `source_id`, `product`, `version` | identité de la source et de sa version |
| `kind` | `categorical_raster`, `continuous_raster` ou `vector` |
| `temporal` | `TemporalValidity` : `instant`, `interval` (carte annuelle) ou `snapshot` (extrait de base) en UTC — date décrite, pas date de téléchargement |
| `resolution_m` | résolution nominale (raster) ; `None` pour un vecteur |
| `crs`, `extent` | CRS natif ; emprise native et WGS84 |
| `license` | identifiant SPDX, attribution, URL, partage à l'identique |
| `provenance` | point d'accès, date de récupération, URL source ; **aucun chemin local accepté** |
| `quality` | `declared`, `verified` ou `unknown`, notes, SHA-256 du fichier local |
| `classes` | nomenclature des rasters catégoriels |

`fingerprint` identifie le descripteur complet. `RetrievalPlan` liste explicitement
les ressources qu'une étape autorisée pourra récupérer ; rien n'est téléchargé à la
planification. `ExternalSourceAdapter` et `LayerRepository` sont des `Protocol`.
`InMemoryLayerRepository` sert aux tests et au prototype.

## Adaptateurs

**ESA WorldCover** (`src/data/worldcover.py`) : tuiles COG 3° × 3° en EPSG:4326,
≈10 m, nommées par leur coin sud-ouest (Akanda → `N00E009`), versions v100 (2020)
et v200 (2021), licence CC BY 4.0. La planification est bornée à 16 tuiles. Le
schéma d'URL a été vérifié par une requête `HEAD` (tuile v200 : 10 132 372 octets,
`Accept-Ranges: bytes`). La qualité est `declared`, non vérifiée sur l'AOI.

**OpenStreetMap** (`src/data/openstreetmap.py`) : requête Overpass construite pour
une emprise ≤ 0,5° et des filtres de tags validés (pas d'injection de requête),
jamais envoyée par l'adaptateur. L'analyse d'une réponse `out geom` produit un
GeoDataFrame EPSG:4326 et un descripteur `snapshot` daté par `timestamp_osm_base`.
Licence ODbL 1.0 (partage à l'identique). Les relations multipolygones ne sont pas
encore assemblées ; une absence d'objet n'est pas une absence sur le terrain.

## Jointures avec Sentinel-2

`src/geo/zonal.py` et `src/services/multisource.py` (sans Streamlit) :

- **spatiale** : la couche externe est alignée sur la grille scientifique
  Sentinel-2, jamais l'inverse. Les classes raster passent par un plus proche voisin
  (aucune interpolation de classe), avec lecture de la seule fenêtre utile. Les
  vecteurs sont rasterisés (`all_touched` configurable). Les statistiques zonales
  portent sur les pixels valides touchés par l'AOI ; les surfaces utilisent les poids
  exacts cellule/AOI existants.
- **temporelle** : `assess_temporal_join` situe la date d'observation par rapport à la
  validité de la couche (`within`, `before_layer`, `after_layer`), calcule l'écart en
  jours et le compare à une tolérance. Une couche statique n'est jamais supposée
  valable à une autre date sans le signaler.

Chaque `LayerJoinResult` porte le descripteur, l'attribution de licence, l'évaluation
temporelle, les pixels sans valeur de couche et une portée explicite (une classe
externe n'explique pas à elle seule le NDVI).

## Validation réelle (diagnostic manuel, réseau)

`scripts/check_worldcover_akanda_live.py` lit uniquement la fenêtre COG couvrant
l'AOI d'Akanda par requêtes HTTP Range via `secure_session` (politique TLS GEONAF,
HTTP refusé, hôte restreint). GDAL/curl n'est pas utilisé pour le réseau : sa pile
schannel rejetait la chaîne (`CERT_TRUST_IS_PARTIAL_CHAIN`). Aucune vérification TLS
n'a été désactivée, aucune autorité ajoutée.

Résultat du 22/10/2026 : 1 048 576 octets reçus sur 10 132 372 (tuile non
téléchargée), fenêtre de 126 Ko écrite une seule fois dans
`data/external/worldcover/` (jamais écrasée). Jointure avec les 827 865 pixels communs
15/25 janvier 2025 : 827 275 pixels classés, 590 sans classe. Exemples de NDVI moyen
au 25/01 : couvert arboré 0,5604 (401 717 px), mangroves 0,5353 (94 580 px), bâti
0,2809 (202 410 px). Écart temporel signalé : 1 120 jours après la carte 2021.

## Anticipation PostGIS

Correspondance prévue, sans migration engagée :

```sql
CREATE TABLE external_layers (
  source_id text, version text, fingerprint char(64) PRIMARY KEY,
  product text, kind text, crs text, resolution_m double precision,
  valid_from timestamptz, valid_to timestamptz, temporal_kind text,
  extent geometry(Polygon, 4326), license_spdx text, attribution text,
  provenance jsonb, quality jsonb, classes jsonb
);
CREATE INDEX ON external_layers USING gist (extent);
```

`LayerRepository.find(source_id, emprise, date)` deviendrait une requête
`ST_Intersects` triée par écart temporel, comme l'implémentation en mémoire.

## Limites et suite

- Pas de module de récupération autorisé en production : l'accès réseau aux hôtes
  WorldCover et Overpass reste à valider (politique réseau, quotas, mise en cache).
- Exactitude WorldCover non vérifiée localement ; nomenclature non adaptée au Gabon.
- Pas encore d'interface Streamlit pour les jointures.
