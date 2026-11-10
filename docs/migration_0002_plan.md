# Migration 0002 - acquisitions, analyses et indicateurs (proposition, non écrite)

Statut : **proposition à valider**. Aucun fichier SQL n'est créé. Prérequis : 0001 appliquée et
vérifiée sur `geonaf-test`.

Objectif : enregistrer les acquisitions Sentinel-2 (puis Sentinel-1), les exécutions d'analyse et
les résultats d'indicateurs, rattachés sans ambiguïté à une version de territoire et à une
géométrie. Le schéma hérité (`geonaf_legacy_draft`) sert d'inventaire des besoins, pas de modèle :
il n'est ni repris ni migré (il ne contient aucune ligne).

## Écarts du schéma hérité corrigés par 0002

| Schéma hérité | Problème | Proposition 0002 |
|---|---|---|
| `sources` (identifiant `bigint`) | Double `data_sources` / `source_releases` | Réutiliser `data_sources` et `source_releases` ; les licences passent par `licences` |
| `territories` et `territory_versions` à identifiant `bigint`, `valid_from`/`valid_to`, `legal_status` libre | Pas d'empreinte. Une version y est confondue avec un changement temporel. Une zone d'acquisition peut être déclarée commune | Supprimés du modèle : référencer `territory_versions (territory_id, version)` de 0001 |
| `analysis_runs.territory_version_id` | Pas de géométrie ni d'octets associés : un cache d'une autre géométrie pourrait être réutilisé | Clés `(territory_id, version)` + `geometry_sha256` + `aoi_sha256`, avec une contrainte de cohérence |
| `acquisitions.acquired_at` seul | La date d'ingestion n'est pas distinguée | `acquired_at` (capteur), `published_at` (catalogue), `ingested_at` (GEONAF) |
| `indicator_definitions.code` modifiable | Une définition qui change réécrit le passé | Clé `(indicator_id, definition_version)`, immuable |
| `indicator_results` sans fiabilité, limites ni nature | Contraire au modèle `TerritorialIndicator` | Colonnes alignées sur `src/indicators/model.py` |
| Aucune immuabilité, aucune politique | — | Mêmes règles que 0001 |

## Tables proposées

- **`acquisitions`** : `acquisition_id` (identity), `source_id` → `data_sources`, `collection`
  (par exemple `sentinel-2-l2a`), `external_id` (identifiant STAC ; unique avec `source_id`),
  `acquired_at`, `published_at`, `ingested_at` (`timestamptz` UTC), `processing_baseline`,
  `cloud_cover_percent` (0-100), `footprint geometry(MultiPolygon, 4326)` avec index GiST, `epsg`,
  `metadata jsonb` (objet), `metadata_sha256`. Immuable : une retraitement est une nouvelle
  acquisition ou une nouvelle ligne de produit.
- **`ingestion_runs`** (nouvelle) : journal de l'ingestion quotidienne (fenêtre interrogée, nombre
  d'acquisitions trouvées, nombre d'acquisitions exploitables, contrôles qualité), clôturé une fois
  comme `registry_imports`. Elle permet d'établir qu'« aucune acquisition exploitable » n'existe à
  une date, sans jamais promettre une image de la veille.
- **`analysis_runs`** : `run_id`, `(territory_id, version)` → `territory_versions`,
  `geometry_sha256` → `geometries`, `aoi_sha256`. Un déclencheur vérifie `geometry_sha256` et
  `aoi_sha256 = file_sha256` de la version. S'y ajoutent `analysis_type`, `code_version`,
  `profile_fingerprint`, `scientific_profile_id`, `parameters jsonb`, `status`, `started_at` et
  `finished_at`. La ligne ne peut être que clôturée.
- **`analysis_acquisitions`** : `(run_id, acquisition_id, role)`, où `role` vaut `input`, `earlier`,
  `later` ou `reference`. S'y ajoutent la politique SCL et le masque appliqué (`mask_policy`).
- **`indicator_definitions`** : `(indicator_id, definition_version)`, `label`, `unit`, `method`,
  `result_kind` (`RESULT_KINDS`), `scientific_status`.
- **`indicator_results`** : `result_id`, `run_id`, `(indicator_id, definition_version)`, `value`
  (fini ou `NULL`), `status` (`disponible`/`indisponible`), `observation_start`, `observation_end`,
  `reference_label`, `reference_at`, `ingested_at`, `coverage_percent`, `comparable_area_m2`,
  `reliability` (`élevée`/`moyenne`/`faible`/`non évaluée`), `reliability_reasons text[]`,
  `limits text[]`, `breakdown jsonb`.

## Attribution à une collectivité (AGENTS.md)

Une valeur n'est « attribuable » à une collectivité que si la version de territoire est de type
`admin_unit` et si le périmètre analysé est exactement celui de cette version
(`aoi_sha256 = file_sha256`). Proposition : une vue `indicator_results_attributable` qui applique
cette règle, plutôt qu'une colonne saisie à la main. Le statut juridique (référentiel communautaire
ou officiel vérifié) est toujours affiché avec la valeur.

## Questions à trancher avant d'écrire 0002

1. Faut-il stocker les rasters en base ? Proposition : non. On ne garde que les clés de stockage et
   les empreintes, comme pour les géométries.
2. Sentinel-1 : mêmes tables avec `collection` distincte, ou table de paramètres radar dédiée
   (orbite, polarisation, mode) ?
3. Quel rôle écrit les analyses : `geonaf_importer` ou un nouveau `geonaf_analyst` ?
4. Les résultats sont-ils une copie des caches fichier (comme le registre jusqu'à D4) ou la
   référence ?
5. Quelle rétention pour les exécutions en échec ?

## Critères de réussite (à la rédaction)

- Corps SQL unique pour local et Supabase, mêmes conventions que 0001 (pas de `%`, `search_path`
  vide, RLS, aucun droit API).
- Tests unitaires : listes copiées de Python (`RESULT_KINDS`, fiabilités, statuts) identiques ;
  refus d'un résultat dont `aoi_sha256` ne correspond pas à la version.
- Simulation sur `geonaf-test` avant toute application.
