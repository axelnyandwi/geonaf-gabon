# Registre territorial dans PostgreSQL/PostGIS - D2-a

Copie **en lecture seule** du registre fichier (`docs/territory_registry.md`) dans PostgreSQL 16 et
PostGIS. Le registre fichier (`data/territories/`) reste la référence jusqu'à la validation de D4 ;
le backend fichier reste celui de l'application et de Streamlit Cloud. Aucun indicateur n'est
calculé ni recalculé ici.

État au lot D2-a1 : composants écrits et testés **sans base**. Aucun serveur n'a encore été installé
ni démarré ; les tests d'intégration (`tests/test_postgis_territory_integration.py`) n'ont pas été
exécutés.

## Fichiers

| Fichier | Rôle |
|---|---|
| `requirements-db.txt` | Dépendances optionnelles (psycopg 3, psycopg-pool, SQLAlchemy Core, Alembic). Absentes de `requirements.txt` |
| `db/sql/0001_territorial_registry.up.sql` / `.down.sql` | Migration SQL explicite (schéma `geonaf`) |
| `db/sql/roles.sql` | Rôles et bases, à exécuter une fois par un administrateur |
| `db/alembic.ini`, `db/migrations/` | Alembic sans ORM : chaque révision exécute un fichier SQL |
| `src/data/postgis_territory_registry.py` | Lignes, conversion géométrique, `PostgisTerritoryRegistry`, plan et import |
| `scripts/import_territories_postgis.py` | Import manuel, vérification seule par défaut |
| `tests/test_postgis_territory_unit.py` | Tests sans base (exécutés par la suite normale) |
| `tests/test_postgis_territory_integration.py` | Tests PostGIS (marqueur `postgis`, ignorés sans base) |

## Schéma

```
licences ──< source_releases >── data_sources
                  │
territory_kinds ──< legal_statuses
     │                 │
territories ──< territory_versions >── geometries
                  │
           registry_imports
```

- `geometries` : clé = empreinte géométrique normalisée (`src/geo/aoi_identity.py`). `geom` est
  l'union WGS84 **non arrondie** utilisée par l'application, en `geometry(MultiPolygon, 4326)` avec
  index GiST ; `source_geometry_type` conserve le type de l'union (`Polygon` ou `MultiPolygon`).
  Géométrie invalide refusée (`ST_IsValid`), jamais réparée.
- `territories` : identité stable ; un territoire ne change jamais de type.
- `territory_versions` : une ligne par version, avec toutes les colonnes de l'enregistrement et
  `record_document` (l'enregistrement exact, en `jsonb`). `area_km2` est la surface GEONAF
  (EPSG:6933), recopiée, jamais recalculée par PostGIS.
- `source_releases` : une version de source = tous les champs de `TerritorySource` sauf la date de
  récupération, conservée par version de territoire.
- `licences` : identifiants SPDX ou `LicenseRef-*`, insérés par migration uniquement. Utilisation
  commerciale et redistribution laissées à `NULL` (« non évaluées ») tant qu'elles ne sont pas
  validées juridiquement.
- `registry_imports` : journal des imports (libellé de source sans chemin, commit, compteurs,
  empreinte globale).
- Vues : `territories_latest` (dernière version) et `territory_identities` (les trois identités).

Contraintes reprises de `validate_record` : statut juridique autorisé selon le type ; « officiel
vérifié » avec justificatif d'au moins 8 caractères ; zones d'acquisition et mailles sans
information administrative ni mot administratif dans le libellé ; unité administrative avec pays,
niveau et type local ; grille si et seulement si maille ; clé de fichier `geometries/<sha>.geojson`.
Les listes copiées de Python (types, statuts, mots administratifs) sont comparées par un test.

Préconditions et sécurité (D2-a2, commun au PostgreSQL local et à Supabase) : la migration n'installe
aucune extension ; elle vérifie PostgreSQL ≥ 15, PostGIS ≥ 3.3 et la résolution du type `geometry`
(l'administrateur active PostGIS avant). Les fonctions s'exécutent avec un `search_path` vide, les
vues en `security_invoker`, aucun droit n'est donné à `PUBLIC` ni aux rôles API Supabase, et la RLS
est activée sur toutes les tables (politiques pour `geonaf_reader` et `geonaf_importer`). Variante
Supabase sans connexion PostgreSQL : `docs/supabase_registry.md`.

Immuabilité : déclencheurs refusant `UPDATE`, `DELETE` et `TRUNCATE` sur toutes les tables ; le
journal d'import ne peut être que clôturé une fois. Chaîne de versions vérifiée (version n
seulement si n-1 existe et `supersedes` = son empreinte) ; parties et sommets d'une version égaux à
ceux de sa géométrie.

## Identités d'une zone

| Identité | Question | Champ |
|---|---|---|
| `geometry_sha256` | Quelle surface ? | `geometries`, `territory_versions` |
| `(territory_id, version)` | Quel périmètre, de quelle source, avec quel statut ? | `territory_versions` |
| `file_sha256` (= `aoi_sha256` des caches et indicateurs) | Quels octets ? | `territory_versions` |

Pour les zones d'acquisition historiques, `aoi_id` = `territory_id` (`register_historical_zones`).
Un résultat calculé sur `(aoi_id, aoi_sha256)` n'est rattaché à `(territory_id, version)` que si
`file_sha256 = aoi_sha256`, sinon à la géométrie seulement si l'empreinte géométrique concorde ;
jamais par l'identifiant seul. Ces zones ne sont pas encore enregistrées (prévu en D3, avec
autorisation) : D2-a fournit seulement la vue `territory_identities`.

## Conversion Polygon → MultiPolygon

L'empreinte géométrique est le SHA-256 du WKB de l'union normalisée **dans son propre type** : le
WKB d'un `Polygon` diffère de celui d'un `MultiPolygon` à une partie. La base stocke donc
`MultiPolygon` + type d'origine, et la lecture reconstruit le type d'origine avant de recalculer
l'empreinte. Les coordonnées circulent en WKB (doubles binaires), sans arrondi.

Vérifié sans base : formes synthétiques (carré, polygone à trou, trois parties, zone en UTM) et les
97 unités geoBoundaries du paquet v3 (92 `Polygon`, 5 `MultiPolygon`) : empreinte inchangée après
conversion et aller-retour WKB.

## Lecture : `PostgisTerritoryRegistry`

Mêmes méthodes de lecture que `TerritoryRegistry` (`versions`, `latest`, `get`, `territories`,
`find_geometry`, `geometry_path`), plus `geometry(record)` et `intersecting(bounds, kind)`. Chaque
ligne lue est reconstruite depuis `record_document` ; l'empreinte recalculée, les colonnes clés et
la version de source doivent concorder, sinon la ligne est refusée. `geometry_path` lit et vérifie
toujours le fichier sur disque. `register` est refusé (lecture seule). L'application n'est pas
branchée sur ce dépôt en D2-a.

## Import

`plan_import` lit et vérifie tout le registre fichier (empreintes d'enregistrement, empreintes des
fichiers, empreintes géométriques, chaîne de versions, conversion MultiPolygon) sans rien écrire.
`apply_import` travaille en une transaction sous verrou consultatif :

- empreinte d'enregistrement déjà présente : ignorée (« déjà présente et identique ») ;
- même territoire et même version avec un autre contenu : **tout le lot est refusé** ;
- géométrie déjà présente : comparée exactement ;
- licence absente : refus (pas de création implicite) ;
- sans `--apply`, la transaction est annulée et le rapport indique ce qui serait inséré.

```powershell
# Vérification seule (aucune connexion)
python scripts/import_territories_postgis.py --data-dir "<données>" --source-label "registre local" --offline
# Simulation dans la base (transaction annulée), puis import réel ; un second import n'insère rien
python scripts/import_territories_postgis.py --data-dir "<données>" --source-label "registre local"
python scripts/import_territories_postgis.py --data-dir "<données>" --source-label "registre local" --apply
```

Le script n'affiche jamais l'URL de la base ni de chemin local.

## Rôles

| Rôle | Droits |
|---|---|
| `geonaf_migrator` | Propriétaire des bases et du schéma ; applique les migrations |
| `geonaf_importer` | Lecture ; `INSERT` sur sources, versions de source, géométries, territoires, versions, journal ; clôture du journal. Pas de licence |
| `geonaf_reader` | Lecture seule |

Les rôles sont créés une fois (`db/sql/roles.sql`) ; la migration 0001 accorde les droits aux rôles
existants. Les mots de passe sont définis de façon interactive (`\password`), jamais écrits.

## Installation et initialisation (D2-a2, après autorisation)

Option retenue : Docker Desktop (WSL2), installé par le service informatique (droits
administrateur ; licence Docker à confirmer pour un poste d'entreprise).

```powershell
# Secrets hors dépôt et hors OneDrive : %USERPROFILE%\.geonaf\postgres.env contient
#   POSTGRES_USER=geonaf_admin / POSTGRES_PASSWORD=<mot de passe> / POSTGRES_DB=postgres
docker pull postgis/postgis:16-3.5
docker image inspect postgis/postgis:16-3.5 --format '{{index .RepoDigests 0}}'   # digest à consigner ici
docker volume create geonaf-pgdata
docker run -d --name geonaf-pg --restart unless-stopped `
  --env-file "$env:USERPROFILE\.geonaf\postgres.env" `
  -v geonaf-pgdata:/var/lib/postgresql/data -p 127.0.0.1:5433:5432 postgis/postgis:16-3.5
docker exec geonaf-pg pg_isready -U geonaf_admin
docker cp db/sql/roles.sql geonaf-pg:/tmp/roles.sql
docker exec -it geonaf-pg psql -U geonaf_admin -d postgres -f /tmp/roles.sql
docker exec -it geonaf-pg psql -U geonaf_admin -d postgres     # \password pour les trois rôles
docker exec geonaf-pg psql -U geonaf_admin -d geonaf -c "CREATE EXTENSION IF NOT EXISTS postgis; GRANT CONNECT ON DATABASE geonaf TO geonaf_importer, geonaf_reader;"
docker exec geonaf-pg psql -U geonaf_admin -d geonaf_test -c "CREATE EXTENSION IF NOT EXISTS postgis; GRANT CONNECT ON DATABASE geonaf_test TO geonaf_importer, geonaf_reader;"

# Environnement Python dédié (ignoré par Git : .venv-*/)
python -m venv .venv-db
.\.venv-db\Scripts\python -m pip install -r requirements-dev.txt -r requirements-db.txt

# Migration (URL lue depuis un fichier hors dépôt, jamais affichée)
$env:GEONAF_DATABASE_URL = (Get-Content "$env:USERPROFILE\.geonaf\migrator.url" -Raw).Trim()
.\.venv-db\Scripts\alembic -c db/alembic.ini upgrade head

# Tests PostGIS sur geonaf_test, puis suite complète
$env:GEONAF_TEST_DATABASE_URL = (Get-Content "$env:USERPROFILE\.geonaf\test.url" -Raw).Trim()
.\.venv-db\Scripts\python -m pytest -m postgis -rs
.\.venv-db\Scripts\python -m pytest -q
```

Les URL ont la forme `postgresql+psycopg://<rôle>:<mot de passe>@127.0.0.1:5433/<base>` ; le
serveur n'écoute que sur 127.0.0.1. Les données PostgreSQL et les sauvegardes ne sont jamais
placées dans OneDrive (risque de corruption par la synchronisation).

## Sauvegarde et restauration (base locale)

Jusqu'à D4 la base est une copie du registre fichier : la restauration de référence est la
réimportation. La sauvegarde sert à valider la procédure qui sera nécessaire ensuite.

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force "$env:USERPROFILE\.geonaf\backups" | Out-Null
# Écrite dans le conteneur puis copiée : aucune redirection binaire par PowerShell
docker exec geonaf-pg pg_dump -U geonaf_admin -d geonaf -Fc -Z 6 -f "/tmp/geonaf-$stamp.dump"
docker cp "geonaf-pg:/tmp/geonaf-$stamp.dump" "$env:USERPROFILE\.geonaf\backups\"
docker exec geonaf-pg rm "/tmp/geonaf-$stamp.dump"
Get-FileHash "$env:USERPROFILE\.geonaf\backups\geonaf-$stamp.dump" -Algorithm SHA256

# Restauration de contrôle dans une base distincte, jamais sur la base active
docker exec geonaf-pg createdb -U geonaf_admin -O geonaf_migrator geonaf_restore_check
docker cp "$env:USERPROFILE\.geonaf\backups\geonaf-$stamp.dump" geonaf-pg:/tmp/restore.dump
docker exec geonaf-pg pg_restore -U geonaf_admin -d geonaf_restore_check --exit-on-error /tmp/restore.dump
docker exec geonaf-pg psql -U geonaf_admin -d geonaf_restore_check -c "SELECT count(*), md5(string_agg(record_fingerprint, ',' ORDER BY record_fingerprint)) FROM geonaf.territory_versions;"
docker exec geonaf-pg psql -U geonaf_admin -d geonaf -c "SELECT count(*), md5(string_agg(record_fingerprint, ',' ORDER BY record_fingerprint)) FROM geonaf.territory_versions;"
docker exec geonaf-pg dropdb -U geonaf_admin geonaf_restore_check
docker exec geonaf-pg rm /tmp/restore.dump
```

Critère : même nombre de lignes et même empreinte agrégée. Rétention proposée : 7 sauvegardes,
plus une avant chaque migration. Le volume Docker n'est jamais copié à chaud.

## Limites

- Aucun test d'intégration exécuté en D2-a1 : contraintes SQL, déclencheurs, droits, Alembic et
  aller-retour WKB réel par PostGIS restent à vérifier sur une base (D2-a2).
- Les versions de `requirements-db.txt` et la balise d'image sont à confirmer à l'installation.
- Les droits commerciaux des licences ne sont pas évalués.
- Les zones d'acquisition historiques ne sont pas encore dans le registre.

## Complément de sécurité Alembic (2026-11-05)

La procédure historique d'installation ci-dessus n'est pas une autorisation d'exécution.
Avant toute migration ou test destructeur, appliquer les conditions de
[connexion et initialisation isolée](database_connection.md#sécurisation-alembic-et-bases-de-test-isolées-2026-11-05).
Une URL locale et un nom terminé par `_test` ne suffisent plus : une autorisation explicite,
le nom exact et un UUID correspondant au commentaire de la base sont obligatoires.

Pour une base déjà migrée, la marque du schéma et l'historique doivent être cohérents avant
l'écriture. Pour une nouvelle base, seul le bootstrap local de test explicitement autorisé
est possible. Aucun `stamp` automatique n'aligne une base Supabase issue du SQL Editor.
La tête actuelle est `0002_observations`.

Après autorisation de provisionner un **cluster neuf et jetable**, prévoir deux bases séparées :

1. Sans PostGIS : refus de l'extension absente, simulations de diagnostic et tests utilisant
   un domaine `geometry` substitut. Ces tests ne prouvent aucune propriété spatiale.
2. Avec PostgreSQL >= 15 et PostGIS >= 3.3 : migrations réelles 0001/0002, rollback vérifié depuis
   une seconde connexion, droits/RLS avec plusieurs rôles, imports atomiques et idempotents,
   parité fichiers/PostGIS, R5 bis et GiST. Répéter avec PostGIS dans `public` et `extensions`.

Ne pas lancer tous les tests marqués `postgis` indistinctement contre une seule base : certains
nécessitent l'absence de PostGIS. Les tests de diagnostic distant restent désactivés
(`GEONAF_DB_INTEGRATION` non défini). Garder données scientifiques en lecture seule et sorties
pytest dans un répertoire temporaire unique, sans cache dans le dépôt.

Avant de comparer les performances, vérifier la parité ; utiliser ensuite
`scripts/benchmark_storage_queries.py` sur les données de test, avec `GEONAF_DEMO_READ_ONLY=1`.
Consigner versions, volumes, médianes et plans `EXPLAIN (ANALYZE, BUFFERS)`. Le test GiST qui
désactive le parcours séquentiel prouve l'utilisabilité de l'index, pas un gain de performance.
Aucune installation, migration réelle ou opération Supabase n'a été exécutée dans ce lot.
