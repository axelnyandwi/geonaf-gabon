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
normalisé en `postgresql+psycopg://`). Le décodeur psycopg/libpq lit la destination effective ; hôte TCP,
port unique et base sont explicites. La query n'accepte que `sslmode` et `sslrootcert`, chacun une seule
fois et non vide. Les redéfinitions `host`, `hostaddr`, `port`, `dbname`, les services libpq, les
listes d'hôtes, les fragments URI et les sockets Unix sont refusés.

Pour un hôte distant : `sslmode=verify-full` obligatoire (défaut), avec une AC PEM lisible contenant
un certificat d'autorité. Le fichier est fourni par `GEONAF_DB_SSLROOTCERT` ou `sslrootcert` ; deux
valeurs contradictoires sont refusées. La validité de la chaîne du serveur et de son nom est vérifiée
par libpq lors de la négociation TLS. `require`, `verify-ca` et les modes plus faibles sont refusés.

L'exemption locale concerne seulement `localhost`, `127.0.0.1` et `::1`. Le pilote reçoit une
`hostaddr` de boucle locale explicite : `localhost` est fixé à `127.0.0.1` (utiliser `[::1]` pour IPv6).
TLS y est facultatif et désactivé par défaut. Aucune résolution DNS ne peut déplacer cette exemption.

Toute variable d'environnement `PG*` non vide est refusée, y compris `PGHOSTADDR`, `PGSERVICE`,
`PGSERVICEFILE`, `PGSYSCONFDIR`, `PGSSLMODE`, `PGSSLROOTCERT` et `PGPASSWORD`. La configuration doit
passer par GEONAF. Le fichier de mots de passe implicite est désactivé (`passfile` vers le périphérique
nul) et `gssencmode=disable` empêche GSS de remplacer TLS. Ces contrôles sont rejoués à chaque connexion
physique, y compris via le créateur SQLAlchemy ; l'URI brute n'est pas transmise au pilote.

Les descriptions et représentations publiques n'exposent que l'environnement. Les erreurs utilisent
les catégories `DB_CONFIG_*`, `DB_ENV_*` ou une catégorie de connexion ; elles n'affichent ni URL,
hôte, utilisateur, base, chemin d'AC, ni commentaire serveur arbitraire.

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
uniquement l’environnement déclaré, sans métadonnées de connexion.

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

- La validation des cibles utilise désormais psycopg/libpq (`requirements-db.txt`). Les tests
  `test_database_policy.py` couvrent les configurations ambiguës et les arguments remis au pilote.
  Le mode fichiers et l’import des modules ne chargent toujours aucun pilote. Le seul socket ouvert est un port de bouclage où rien n'écoute
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

## Sécurisation Alembic et bases de test isolées (2026-11-05)

`db/migrations/env.py` utilise désormais `load_target()` et `create_engine()` du connecteur
commun. La destination est revalidée au moment de la connexion physique ; les variables `PG*`
non vides sont refusées. Une connexion distante exige `verify-full` et une AC PEM explicite.
Le mode fichiers par défaut n'est pas modifié. Les exceptions de migration exposent uniquement
une catégorie statique, sans texte du pilote, URL, commentaire serveur ou chemin d'AC.

### Base existante

Avant le DDL et toute écriture dans la table de version, Alembic vérifie :

- la marque `geonaf-environment:dev` ou `geonaf-environment:test` du schéma, égale à
  `GEONAF_DB_ENV` ; la variable seule ne constitue pas une preuve ;
- une révision unique connue dans `public.alembic_version`, cohérente avec l'inventaire des
  tables de 0001/0002 (les colonnes, contraintes et fonctions restent à valider sur serveur réel) ;
- si `geonaf.schema_migrations` existe, la liste exacte des révisions appliquées et les SHA-256
  de leurs corps SQL. Un schéma déjà déployé mais sans version Alembic alignée est refusé.

Aucun alignement automatique, `stamp` de modification ou `purge` n'est exécuté. Une nouvelle
révision est ajoutée au journal existant dans la même transaction que le DDL et la version
Alembic. Son caractère immuable interdit les downgrades lorsqu'il est présent. Sur une base
sans ce journal, un downgrade exige la même autorisation de base jetable que les fixtures.
Une empreinte ancienne différente nécessite un audit distinct ; ne pas réécrire le journal
pour contourner ce refus.

### Base neuve : autorisation séparée

L'initialisation est réservée à une nouvelle base locale jetable dans un cluster dédié aux tests.
Son administrateur doit d'abord la créer et enregistrer, dans le **commentaire de la base**, une
marque `geonaf-isolated-test:<UUID canonique propre à cette instance>`. Cette étape est externe
au lanceur : aucun test ne crée la base et aucun test ne lui attribue sa propre identité.
Ne pas marquer une base existante de données métier pour rendre les tests exécutables.

Le processus de test doit recevoir simultanément :

| Variable | Exigence |
|---|---|
| `GEONAF_DB_ENV` | `test` pour Alembic |
| `GEONAF_DATABASE_URL` | URL explicite locale pour Alembic |
| `GEONAF_TEST_DATABASE_URL` | URL explicite locale pour les fixtures |
| `GEONAF_TEST_DATABASE_NAME` | Nom exact de la base, également terminé par `_test` |
| `GEONAF_TEST_INSTANCE_ID` | UUID canonique identique à celui du commentaire de la base |
| `GEONAF_ALLOW_DESTRUCTIVE_TESTS` | `1`, autorisation explicite des destructions dans cette base jetable |
| `GEONAF_MIGRATION_BOOTSTRAP` | `1` uniquement pour autoriser Alembic à partir d'un schéma absent |

Le connecteur refuse les destinations distantes, multiples et les paramètres de redirection.
Il fixe l'adresse de boucle locale, puis lit le nom réel et le commentaire de la base avant
les écritures. L'identifiant est une protection contre une erreur de cible ; il ne remplace
pas la confiance dans l'administrateur ni ne prouve l'absence d'un tunnel local mal configuré.
Les rôles étant communs au cluster, ne pas utiliser un cluster partagé avec des bases métier.

La migration 0001 crée elle-même `geonaf`. Le lanceur ne précrée jamais ce schéma ; il pose la
marque `test` après 0001, dans la même transaction. Une base vide sans autorisation de bootstrap
ou sans identité côté serveur est refusée. Désactiver l'autorisation de bootstrap après usage.

### Transactions, R5 bis et compatibilité

Une invocation utilise une transaction SQLAlchemy explicite unique : contrôles, verrou
consultatif de migration, chemin local `pg_catalog, <schéma PostGIS>, pg_temp`, DDL et historiques.
Tout échec annule l'ensemble des révisions de l'invocation, au lieu de conserver un commit par
révision. Le verrou sérialise les lanceurs qui utilisent ce chemin ; il ne bloque pas un
administrateur qui modifie le catalogue directement. `public.alembic_version` reste explicitement
qualifiée, même si PostGIS réside dans `extensions`.

Les scripts sont limités aux révisions 0001/0002 connues. Ajouter une révision demandera de
mettre à jour les contrôles et leurs tests. Les anciennes commandes ne définissant que l'URL
ou utilisant `PGPASSWORD`, `.pgpass` implicite ou une base non marquée sont incompatibles.
Les fixtures locales des six modules d'intégration destructeurs utilisent toutes
`tests/postgis_support.py` ; elles vérifient l'identité dans une transaction en lecture seule
avant de rendre une connexion autorisée à écrire. Le test R5 bis injecte son chemin hostile
par SQL après cette vérification, jamais par une option de connexion brute.

Aucune migration réelle n'a validé ce changement. Alembic n'est pas installé dans l'environnement
Python utilisé : les tests du lanceur emploient un faux contexte Alembic et de faux catalogues ;
les tests de paramètres utilisent le vrai SQLAlchemy et interceptent psycopg avant connexion.
Le contrat de transaction et les callbacks ont été relus dans les sources officielles Alembic
1.14.0, version déclarée dans `requirements-db.txt`. La compatibilité réelle du moteur, des
callbacks et du pooler Supabase reste à vérifier après autorisation séparée.

### Avertissement Rasterio conservé

Le test de budget réseau peut émettre `PytestUnraisableExceptionWarning` depuis
`rasterio._err.log_error` : `ByteBudgetExceeded` traverse le callback natif de lecture GDAL.
La reproduction en mémoire avec Rasterio 1.4.4 / GDAL 3.10.3 respecte le plafond d'octets et
ferme les vues ainsi que les datasets observés. Aucune fuite n'a été démontrée ; cela ne
constitue pas une mesure des ressources natives à long terme. Aucun code raster, réglage
d'avertissement ni dépendance n'est modifié par la correction Alembic.
