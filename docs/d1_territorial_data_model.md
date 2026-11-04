# D1 - Modèle de données du registre territorial

Référence : migration `db/sql/0001_territorial_registry.up.sql` (décision du 2026-11-04, option A).
Ce document décrit le modèle, ses relations, ses contraintes et les décisions qui le justifient. Le
détail d'exploitation est dans `docs/postgis_registry.md` (PostgreSQL local) et
`docs/supabase_registry.md` (Supabase).

État : modèle écrit et testé **sans base** (tests unitaires, parité hors ligne des 97 unités
geoBoundaries). Jamais appliqué sur un serveur PostgreSQL ni sur Supabase.

## Périmètre

- Le registre fichier (`data/territories/`, `src/data/territory_registry.py`) reste la référence
  jusqu'à D4. La base en est une **copie en lecture seule**, contrôlée par empreintes.
- Hors périmètre : acquisitions, analyses et indicateurs (proposition de migration 0002,
  `docs/migration_0002_plan.md`).

## Relations

```
licences ──< source_releases >── data_sources
                  │
territory_kinds ──< legal_statuses
     │                 │
territories ──< territory_versions >── geometries
                  │
           registry_imports
```

`A ──< B` : une ligne de A pour plusieurs lignes de B. `territory_versions` référence aussi
`territories` par `parent_territory_id` (unité parente, contrainte différée).

| Table | Clé | Rôle |
|---|---|---|
| `licences` | `licence_id` (SPDX ou `LicenseRef-*`) | Licences connues, insérées par migration uniquement. Usage commercial et redistribution à `NULL` (non évalués) |
| `data_sources` | `source_id` | Fournisseur et nature d'une source (geoBoundaries, GEONAF...) |
| `source_releases` | `release_id`, unicité sur tous les champs | Une version de source = tous les champs de `TerritorySource` sauf la date de récupération |
| `geometries` | `geometry_sha256` | Union WGS84 non arrondie, `geometry(MultiPolygon, 4326)`, index GiST, type d'origine conservé |
| `territory_kinds` | `kind` | `acquisition_zone`, `admin_unit`, `custom_import`, `grid_cell` |
| `legal_statuses` | `(kind, legal_status)` | Statuts autorisés par type (copie de `LEGAL_STATUSES`) |
| `territories` | `territory_id` | Identité stable ; un territoire ne change jamais de type |
| `territory_versions` | `(territory_id, version)` | Une ligne par version, avec l'enregistrement exact (`record_document`, `jsonb`) |
| `registry_imports` | `import_id` | Journal des imports (libellé sans chemin, commit, compteurs, empreinte globale) |
| `schema_migrations` | `version` | Supabase uniquement : historique tant qu'Alembic ne peut pas se connecter |

Vues (`security_invoker`) : `territories_latest` (dernière version de chaque territoire) et
`territory_identities` (les trois identités de chaque version).

## Les trois identités d'une zone

| Identité | Question | Champ |
|---|---|---|
| `geometry_sha256` | Quelle surface ? | `geometries`, `territory_versions` |
| `(territory_id, version)` | Quel périmètre, de quelle source, avec quel statut ? | `territory_versions` |
| `file_sha256` (= `aoi_sha256` des caches et indicateurs) | Quels octets ? | `territory_versions` |

Un résultat calculé sur `(aoi_id, aoi_sha256)` n'est rattaché à une version que si
`file_sha256 = aoi_sha256` ; à défaut, à la géométrie seulement si l'empreinte géométrique concorde ;
jamais par l'identifiant seul.

## Exigences territoriales (AGENTS.md) et mécanismes

| Exigence | Mécanisme dans 0001 | Contrôle Python équivalent |
|---|---|---|
| Observer toute zone, avec ou sans frontière officielle | Types `custom_import` et `grid_cell` ; `country_iso3` facultatif hors unités administratives | `KINDS` |
| Zones d'acquisition historiques jamais présentées comme communes | `CHECK` : ni pays, niveau, type local, identifiant source ou parent ; libellé sans mot administratif | `validate_record`, `ADMINISTRATIVE_WORDS` |
| Officiel seulement avec justificatif | `CHECK` : « officiel vérifié » exige `legal_evidence` d'au moins 8 caractères ; statut autorisé selon le type (FK `legal_statuses`) | `LEGAL_STATUSES`, `is_official` |
| Provenance, version, licence, statut, identité géométrique par périmètre | `release_id` (source, produit, version, licence, attribution), `legal_status`, `geometry_sha256`, `file_sha256`, `record_fingerprint` | `TerritoryRecord` |
| Ne jamais réutiliser un résultat d'une autre géométrie | Trois identités séparées ; vue `territory_identities` | `load_aoi_identity` |
| Nouvelle version ou autre référentiel ≠ changement réel du territoire | Pas de colonne de validité temporelle ; une version est une nouvelle ligne chaînée par `supersedes` | Versions du registre fichier |
| Afficher les limites des périmètres | `limits text[]` recopié | `limits` |
| Extension africaine, grilles stables | ISO 3166-1 alpha-3, `grid_system`, `grid_cell_id`, `grid_cell_size_deg` | `AdminInfo`, `GridInfo` |
| Reproductibilité | `record_document` exact, empreinte recalculable ; surface EPSG:6933 recopiée, jamais recalculée | `record_fingerprint` |

La concordance des listes copiées de Python (types, statuts, mots administratifs) est vérifiée par
`tests/test_postgis_territory_unit.py::test_sql_copies_of_python_rules_are_identical`.

## Décisions architecturales

1. **Copie, pas source de vérité (jusqu'à D4).** La restauration de référence est la réimportation
   du registre fichier.
2. **Immuabilité.** Déclencheurs qui refusent `UPDATE`, `DELETE` et `TRUNCATE` sur toutes les tables.
   Seule exception : le journal d'import, qui ne peut être clôturé qu'une fois. Une correction est
   une nouvelle version.
3. **Chaîne de versions.** La version n existe seulement si la version n-1 existe et si `supersedes`
   est égal à son empreinte. Les parties et les sommets doivent être égaux à ceux de la géométrie
   stockée.
4. **Géométrie adressée par son empreinte.** Elle est stockée en `MultiPolygon` avec son type
   d'origine (`Polygon` ou `MultiPolygon`), ce qui permet de recalculer exactement l'empreinte (le
   WKB diffère d'un type à l'autre). Une géométrie invalide est refusée, jamais réparée.
5. **Dates en texte d'origine** (`created_at_utc`, `retrieved_at_utc`) : l'empreinte de
   l'enregistrement porte sur le texte exact.
6. **Statuts juridiques par type, sous forme de table**, pas d'énumération : un nouveau statut
   s'ajoute par migration, de façon traçable.
7. **Sécurité.** La RLS est activée partout, avec des politiques pour `geonaf_reader` (lecture) et
   `geonaf_importer` (insertion et clôture du journal). Aucun droit n'est donné à `PUBLIC` ni aux
   rôles API Supabase. Les fonctions ont un `search_path` vide et les vues sont en
   `security_invoker`. Le schéma n'est pas exposé par la Data API.
8. **Un seul corps SQL** pour PostgreSQL local (Alembic) et pour Supabase (SQL Editor). La migration
   n'installe aucune extension.
9. **Marque d'environnement** (`COMMENT ON SCHEMA geonaf`), écrite une fois. Sans elle, la couche
   D2.1 refuse toute session.
10. **Schéma hérité renommé, jamais supprimé.** Le schéma `geonaf` trouvé le 2026-11-03 n'est pas issu
    de 0001. Il devient `geonaf_legacy_draft` (`docs/supabase_legacy_schema_plan.md`).

## Écarts mineurs entre Python et SQL

Aucun ne laisse entrer un enregistrement invalide par le chemin normal : l'import passe par
`validate_record`, puis l'empreinte est vérifiée. La base reste en revanche moins stricte que Python
pour une insertion SQL directe.

| Point | Python | SQL |
|---|---|---|
| Informations administratives partielles sur un `custom_import` (pays sans niveau, par exemple) | Refusées | Acceptées |
| Informations de grille partielles sur un type autre que maille | Refusées | Acceptées (seule la conjonction `grid_system` + `grid_cell_id` est contrôlée) |
| Clé de stockage | Préfixe `geometries/` | Plus strict : `geometries/<sha>.geojson` |
| Vérification de l'autorité émettrice d'une limite officielle | Non (longueur du justificatif seulement) | Non |

Correctif possible dans une migration ultérieure (non préparée) : des `CHECK` « tout ou rien » sur
les blocs administratif et grille. Le contrôle de l'autorité émettrice reste une procédure humaine
et documentaire.

## Limites

- Aucun test d'intégration exécuté : les contraintes, les déclencheurs, les droits, la RLS et
  l'aller-retour WKB réel ne sont vérifiés que hors ligne.
- Les droits commerciaux des licences ne sont pas évalués.
- Les zones d'acquisition historiques ne sont pas encore enregistrées (prévu en D3).
