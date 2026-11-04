# Connexion PostgreSQL/PostGIS (D2.1)

Couche d'accès en **lecture seule** à une base Supabase (`geonaf-dev` ou `geonaf-test`). Elle ne
migre aucune donnée, ne modifie aucune table et n'est utilisée par aucune page Streamlit :
l'application reste sur le stockage fichier.

## Architecture

| Fichier | Rôle |
| --- | --- |
| `src/data/database.py` | Configuration, validation de l'URL, garde d'environnement, moteur SQLAlchemy, session en lecture seule. |
| `src/data/database_diagnostic.py` | Diagnostic en lecture seule (logique pure + liaison à une transaction réelle). |
| `scripts/check_database_live.py` | Diagnostic manuel avec accès réseau (hors pytest). |
| `tests/test_database_unit.py` | Tests sans base ni réseau. |
| `tests/test_database_integration.py` | Tests facultatifs sur une base réelle, désactivés par défaut. |

Principes :

- **Aucune connexion automatique.** Importer les modules ne charge ni SQLAlchemy ni psycopg et
  n'ouvre aucune connexion ; un test le vérifie, ainsi que l'absence d'import depuis `app.py`,
  `views/` et `src/components/`.
- **Dépendances optionnelles.** SQLAlchemy 2.0.36 et psycopg 3.2.3 sont dans
  `requirements-db.txt`, pas dans `requirements.txt` (Streamlit Cloud inchangé). Versions installées
  et testées sous Python 3.10.11 et 3.13.3.
- **Session = une transaction en lecture seule** (`SET TRANSACTION READ ONLY`), délai d'instruction
  local de 15 s, délai de connexion de 10 s, toujours annulée (`ROLLBACK`) puis connexion fermée,
  y compris en cas d'erreur.
- **`NullPool`** : pas de connexion conservée côté client ; la mutualisation est confiée au pooler
  Supabase.

## Variables d'environnement

| Variable | Valeurs | Défaut | Rôle |
| --- | --- | --- | --- |
| `GEONAF_DB_BACKEND` | `file`, `postgres` | `file` | Stockage choisi par l'application. Aujourd'hui lu par aucune page : `postgres` n'a encore aucun effet. |
| `GEONAF_DATABASE_URL` | `postgresql+psycopg://…` | — | Cible PostgreSQL (secret : contient le mot de passe). |
| `GEONAF_DB_ENV` | `dev`, `test` | — | Environnement **déclaré**. Toute autre valeur (`prod`…) est refusée avant connexion. |

Règles de validation de l'URL (sans connexion) : pilote psycopg 3 uniquement (`postgresql://` est
normalisé en `postgresql+psycopg://`, `psycopg2`/`asyncpg` refusés) ; hôte et base obligatoires ;
pour un hôte non local, TLS obligatoire (`sslmode=require` par défaut, `disable`/`allow`/`prefer`
refusés). Les messages d'erreur ne reprennent jamais l'URL.

## Garde d'environnement

`GEONAF_DB_ENV` n'est qu'une déclaration : un `.env` copié d'un autre projet la rendrait fausse sans
bruit. La preuve vient de la base elle-même : chaque projet porte une marque écrite une seule fois
sur le schéma par `db/supabase/03_mark_environment_<env>.sql` :

```sql
COMMENT ON SCHEMA geonaf IS 'geonaf-environment:dev';
```

`read_only_session()` lit cette marque en premier et **ne remet la connexion** que si elle vaut
`geonaf-environment:<GEONAF_DB_ENV>`. Sinon : `EnvironmentIdentityError`, transaction annulée,
connexion fermée. Une base sans marque est refusée. Prérequis : le script `03` a été exécuté sur
chaque projet (le diagnostic le signale sinon).

Limite : la marque prouve l'identité *déclarée dans la base*, pas l'identité physique du projet. Un
administrateur qui modifierait le commentaire du schéma contournerait la garde ; c'est une protection
contre l'erreur de configuration, pas contre un acte délibéré.

## Diagnostic

Contrôles, dans l'ordre, dans une seule transaction en lecture seule : connexion, présence du schéma
`geonaf`, marque d'environnement, tables attendues, PostGIS (extension déclarée **et** appel de
`postgis_lib_version()`). Après un échec, les contrôles suivants sont « non exécuté » : rien n'est
lu dans le schéma d'une base dont l'identité n'est pas confirmée.

Tables attendues : les **10** tables créées par `db/supabase/02_migration_0001_application.sql`
(`data_sources`, `geometries`, `legal_statuses`, `licences`, `registry_imports`,
`schema_migrations`, `source_releases`, `territories`, `territory_kinds`, `territory_versions`).
Un test unitaire vérifie que `EXPECTED_TABLES` reste identique au SQL. Une table supplémentaire est
signalée sans faire échouer le contrôle ; une table manquante le fait échouer.

```powershell
pip install -r requirements-db.txt
python scripts/check_database_live.py
```

Codes de sortie : `0` conforme, `1` contrôle en échec, `2` configuration invalide. La sortie affiche
`utilisateur@hôte:port/base` sans mot de passe.

## Configuration locale et secrets Supabase

1. Créer un fichier `.env` à la racine (ignoré par Git, comme `.env.*` et `.streamlit/secrets.toml`)
   à partir de `.env.example`, ou définir les variables dans le terminal.
2. Récupérer la chaîne de connexion dans le tableau de bord Supabase (*Connect* > *Session pooler*)
   et y placer le mot de passe de la base. Encoder les caractères spéciaux du mot de passe
   (`@` → `%40`, `:` → `%3A`, `/` → `%2F`, `#` → `%23`).
3. Un fichier ou un terminal par environnement ; ne jamais réutiliser l'URL de `dev` avec
   `GEONAF_DB_ENV=test` (la garde le refuserait de toute façon).

Règles :

- Le mot de passe n'apparaît ni dans le code, ni dans Git, ni dans les journaux : `repr()` de la cible
  l'exclut, les erreurs de connexion sont expurgées, SQLAlchemy masque les paramètres liés
  (`hide_parameters`).
- **Clés API Supabase** : la connexion PostgreSQL n'utilise ni `anon` ni `service_role`. La clé
  `service_role` contourne la RLS : elle ne doit jamais être placée dans le navigateur, dans une
  page Streamlit ni dans ce dépôt.
- **Moindre privilège** : l'utilisateur `postgres` est administrateur. Les rôles prévus
  (`geonaf_reader`, `geonaf_importer`) sont aujourd'hui `NOLOGIN` (`db/supabase/01_roles.sql`) ;
  leur activation (`ALTER ROLE … LOGIN PASSWORD`) est une décision distincte, hors D2.1. En
  attendant, la transaction en lecture seule limite le risque d'écriture accidentelle, pas celui
  d'un usage malveillant du mot de passe.
- Pour un futur déploiement Streamlit Cloud, les variables iront dans les *secrets* de la plateforme,
  jamais dans `requirements.txt` ni dans le code.

## Pooler Supabase (Supavisor)

| Mode | Hôte et port | Utilisateur | Usage conseillé |
| --- | --- | --- | --- |
| Connexion directe | `db.<ref>.supabase.co:5432` | `postgres` | IPv6 uniquement sur l'offre gratuite. |
| Pooler, mode session | `aws-0-<région>.pooler.supabase.com:5432` | `postgres.<ref>` | Diagnostic, scripts, Alembic (IPv4). |
| Pooler, mode transaction | `aws-0-<région>.pooler.supabase.com:6543` | `postgres.<ref>` | Futures requêtes courtes de l'application. |

Le mode transaction ne permet ni instructions préparées côté serveur ni `SET` de session. La couche
en tient compte : `prepare_threshold=None` (psycopg 3 ne prépare jamais) et paramètres de
transaction posés avec `SET TRANSACTION` / `set_config(…, true)`, locaux à la transaction. Elle
fonctionne donc dans les deux modes.

Sur le poste Windows actuel, les ports 5432 et 6543 sont fermés (voir `docs/supabase_registry.md`) :
le diagnostic réel doit être lancé depuis un réseau qui les autorise.

## Tests

```powershell
python -m pytest tests/test_database_unit.py               # sans base ni réseau
$env:GEONAF_DB_INTEGRATION = "1"                            # facultatif : base réelle, lecture seule
python -m pytest tests/test_database_integration.py -m postgis
```

- Les tests unitaires tournent sans `requirements-db.txt` ; ceux qui ont besoin de SQLAlchemy et
  psycopg sont alors ignorés. Le seul socket ouvert est un port de bouclage où rien n'écoute
  (`127.0.0.1:1`), pour vérifier l'expurgation d'une erreur de connexion réelle.
- Les tests d'intégration exigent `GEONAF_DB_INTEGRATION=1` en plus de l'URL : la seule présence
  d'une URL dans l'environnement ne suffit pas à solliciter une base réelle. Ils vérifient le
  diagnostic, le refus de l'autre environnement et le refus d'une écriture (table temporaire).
  Ils ne suppriment ni ne créent rien, contrairement à `tests/test_postgis_territory_integration.py`
  qui utilise une autre variable (`GEONAF_TEST_DATABASE_URL`) et une base locale.

## Limites

- Aucun test n'a encore été exécuté contre Supabase (ports fermés depuis le poste).
- Divergence expliquée (2026-11-04) : les 8 tables déployées forment un schéma hérité sans rapport
  avec la migration 0001 (`docs/supabase_legacy_schema_plan.md`). Option A validée : renommer ce
  schéma en `geonaf_legacy_draft`, puis appliquer 0001. `EXPECTED_TABLES` reste aligné sur 0001
  (10 tables) ; d'ici là, le contrôle « tables » du diagnostic doit
  échouer ; `EXPECTED_TABLES` ne sera aligné qu'après comparaison détaillée des deux schémas.
- `GEONAF_DB_BACKEND=postgres` est accepté mais n'est branché sur aucun usage : l'application reste
  sur les fichiers. Le registre PostGIS de D2-a (`src/data/postgis_territory_registry.py`) ouvre
  encore ses connexions sans cette garde ; l'y raccorder est un lot ultérieur.
- Les sessions sont en lecture seule ; l'écriture (import) passera par un lot dédié.
