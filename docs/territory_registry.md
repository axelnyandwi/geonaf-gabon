# Registre territorial — lot A

`src/data/territory_registry.py` : registre versionné et immuable des territoires observés par
GEONAF. Stockage seulement : ni interface, ni réseau. Le moteur d'analyse n'est pas encore branché
dessus (lots suivants) ; les analyses existantes continuent de lire `data/aoi/<id>.geojson`.

## Pourquoi

GEONAF doit observer des territoires dont les limites sont absentes, contradictoires ou
imprécises. Le registre sépare donc trois questions :

- **quelle géométrie** : empreinte normalisée (`src/geo/aoi_identity.py`) et empreinte du fichier ;
- **d'où elle vient** : source, produit, version, licence, attribution, millésime ;
- **ce qu'elle est juridiquement** : `legal_status`, contrôlé selon le type de territoire.

## Types et statuts

| Type (`kind`) | Statuts autorisés | Contraintes |
|---|---|---|
| `acquisition_zone` | zone d'acquisition GEONAF | aucune information administrative ; libellé sans « commune », « département », « arrondissement », « province »… |
| `admin_unit` | référentiel communautaire ; référentiel humanitaire (COD) ; officiel vérifié | niveau (`ADM2`…) et type local **déclarés par la source** (jamais déduits) |
| `custom_import` | fourni par l'utilisateur ; officiel vérifié | informations administratives facultatives |
| `grid_cell` | grille GEONAF | système de grille et identifiant de maille obligatoires ; pas d'information administrative |

« officiel vérifié » exige un justificatif (`legal_evidence` : acte, référence, document). Deux
référentiels décrivant la même unité (gbOpen, gbHumanitarian) sont **deux territoires distincts**
(identifiants `gab-gbopen-adm2-…`, `gab-gbhumanitarian-adm2-…`), jamais fusionnés.

## Immuabilité et versions

Arborescence `data/territories/` (écriture unique, jamais écrasée) :

- `geometries/<geometry_sha256>.geojson` : premier fichier enregistré pour cette géométrie ;
- `records/<territory_id>/v<NNNN>.json` : enregistrement + empreinte de l'enregistrement.

`register(...)` : contenu identique ⇒ version existante renvoyée ; géométrie ou métadonnées
différentes ⇒ version n+1 qui référence (`supersedes`) l'empreinte de la version n. Les versions
antérieures restent lisibles (`get(id, version)`). À la lecture, un enregistrement ou une géométrie
modifiés après écriture sont refusés. Géométrie invalide : refus, rien n'est stocké.

Une nouvelle version de géométrie n'est **pas** un changement du territoire réel : c'est une autre
description de ses limites.

## Zones historiques

`register_historical_zones(registry, aoi_dir)` enregistre Akanda, Libreville et Owendo comme
`acquisition_zone` (« Zone d'acquisition Akanda (12 × 12 km) »), avec **leurs identifiants
actuels**, une copie octet pour octet du fichier, le profil scientifique de référence et les limites
« pas une limite administrative ; zones voisines qui se recouvrent ». Empreintes géométriques et de
fichier identiques aux originaux ⇒ caches, couches stockées et résultats d'Akanda inchangés.

| Zone | Surface | Empreinte géométrique | Profil |
|---|---|---|---|
| akanda | 144,109 km² | `d8b8e93ee562…` | gabon-equatorial-akanda |
| libreville | 144,107 km² | `813da80554dc…` | gabon-equatorial-libreville |
| owendo | 144,104 km² | `1c3d5607c77a…` | gabon-equatorial-owendo |

Le lot n'écrit rien dans le répertoire de données réel ; l'enregistrement effectif se fera avec
l'intégration (lot suivant) ou sur autorisation.

## Correspondance PostGIS (prévue, aucune migration engagée)

```sql
CREATE TABLE territories (
  territory_id text, version integer, PRIMARY KEY (territory_id, version),
  label text, kind text, legal_status text, legal_evidence text,
  source jsonb, admin jsonb, grid jsonb, scientific_profile_id text, limits text[],
  geometry_sha256 char(64), file_sha256 char(64), storage_key text,
  area_km2 double precision, vertices integer, parts integer,
  geom geometry(MultiPolygon, 4326), created_at timestamptz, supersedes char(64),
  record_fingerprint char(64) UNIQUE
);
CREATE INDEX ON territories USING gist (geom);
CREATE INDEX ON territories (geometry_sha256);
```

## Performances (copie temporaire des données, 23/10/2026)

- Enregistrement des 3 zones historiques : 0,18 s ; ré-enregistrement idempotent : 0,05 s.
- Lecture de 203 territoires (dernières versions, empreintes vérifiées) : 0,75 s — linéaire,
  ≈ 3,7 ms par territoire. À l'échelle africaine (milliers d'unités), un index ou PostGIS sera
  nécessaire.

## Limites

- Pas encore d'interface, d'adaptateur geoBoundaries ni d'import GeoJSON (lots suivants).
- Le registre décrit les limites ; il ne garantit pas leur exactitude terrain.
- Une même surface décrite autrement (sommets colinéaires) peut avoir une autre empreinte
  (comportement conservateur, voir `aoi_identity.md`).
