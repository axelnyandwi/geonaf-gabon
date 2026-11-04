# Registre territorial sur Supabase - D2-a2

Adaptation de D2-a (`docs/postgis_registry.md`) à Supabase, projet `geonaf-dev` (offre gratuite,
PostGIS 3.3 activé). Depuis le poste Windows, seul HTTPS passe : les ports PostgreSQL 5432 et 6543
sont fermés. Ni Alembic, ni psycopg, ni les tests d'intégration ne peuvent donc se connecter. Tout
passe par des scripts SQL relus puis **collés à la main dans le SQL Editor**.

Le registre fichier (`data/territories/`) reste la référence ; l'application et Streamlit Cloud ne
sont pas modifiées ; aucun indicateur n'est calculé.

État : scripts préparés et testés hors ligne. **Aucun n'a été exécuté sur Supabase.**

## Incompatibilités de D2-a1 relevées et traitement

| D2-a1 | Problème sur Supabase | Traitement |
|---|---|---|
| `CREATE EXTENSION IF NOT EXISTS postgis` | PostGIS est déjà dans le schéma `extensions` ; si l'extension manquait, elle serait créée dans `public` | Supprimé ; précondition qui vérifie PostgreSQL ≥ 15, PostGIS ≥ 3.3 et la résolution du type `geometry` |
| Type et fonctions PostGIS non qualifiés | Résolus seulement si `extensions` est dans le `search_path` | Chaque script commence par `set_config('search_path', 'pg_catalog, public, extensions', true)` (local à la requête) |
| Fonctions sans `search_path` fixé | Avertissement du linter Supabase ; risque de détournement | `SET search_path = ''` sur les trois fonctions (tout est qualifié) |
| Vues par défaut | Exécutées avec les droits du propriétaire (alerte « security definer view ») | `WITH (security_invoker = true)` |
| Rôles API `anon`, `authenticated`, `service_role` | Doivent n'avoir aucun accès | `REVOKE` explicite sur le schéma, les tables, séquences et fonctions ; schéma non exposé par la Data API (vérifié) |
| Pas de RLS | Aucune seconde barrière si le schéma était exposé un jour | RLS activée sur toutes les tables ; politiques pour `geonaf_reader` et `geonaf_importer` ; le propriétaire (migrations) n'y est pas soumis |
| `roles.sql` : `CREATE DATABASE`, rôles `LOGIN` avec `\password` | Une seule base par projet ; `\password` indisponible ; un mot de passe saisi resterait dans l'historique de l'éditeur | Rôles `NOLOGIN` sans mot de passe (`01_roles.sql`) ; migrations exécutées par `postgres`, propriétaire du schéma |
| Alembic | Exige une connexion TCP | Historique tenu dans `geonaf.schema_migrations` (immuable) ; plus tard `alembic stamp 0001_territorial_registry` |
| Import psycopg | Exige une connexion TCP | Lots SQL générés hors ligne depuis le registre vérifié |
| Tests d'intégration (`geonaf_test`) | Pas de seconde base ni de connexion | Second projet `geonaf-test` ; tests de contraintes en SQL, toujours annulés |
| `BEGIN` / `COMMIT` | Une erreur au milieu laisserait la session de l'éditeur dans une transaction en échec | Aucun contrôle de transaction : une requête à plusieurs instructions est une seule transaction implicite (tout ou rien) ; vérifié par la sonde de `00_preflight.sql` |

Le corps de la migration reste **unique** (`db/sql/0001_territorial_registry.up.sql`), pour PostgreSQL
local et pour Supabase ; les fichiers `02_migration_0001_*.sql` l'incluent sans modification
(contrôlé par un test).

## Fichiers

| Fichier | Nature | Projet |
|---|---|---|
| `db/supabase/00_preflight.sql` | Lecture seule ; sonde de transaction | dev et test |
| `db/supabase/01_roles.sql` | Crée `geonaf_reader` et `geonaf_importer` (`NOLOGIN`), idempotent | dev et test |
| `db/supabase/02_migration_0001_simulation.sql` | Migration complète puis erreur volontaire : n'écrit rien | dev et test |
| `db/supabase/02_migration_0001_application.sql` | Migration 0001, une seule fois | dev et test |
| `db/supabase/03_mark_environment_dev.sql` / `_test.sql` | Marque l'environnement (commentaire du schéma), jamais modifiable | un seul chacun |
| `db/supabase/04_verify_schema.sql` | Lecture seule | dev et test |
| `05_import_lot_NN_simulation.sql` / `_application.sql` | Générés hors du dépôt ; la simulation n'écrit rien | dev (et test) |
| `06_verify_import.sql` | Généré ; lecture seule, valeurs attendues incluses | dev (et test) |
| `db/supabase/07_export_parity.sql` | Lecture seule ; export CSV | dev |
| `db/supabase/90_test_constraints.sql` | Destructif mais toujours annulé ; refuse de tourner hors `test` | **test uniquement** |
| `db/supabase/99_drop_schema_test.sql` | Supprime le schéma ; refuse de tourner hors `test` | **test uniquement** |
| `scripts/build_supabase_sql.py` | Génère la migration (`migration`) et les lots (`import`) | poste |
| `scripts/verify_supabase_export.py` | Compare l'export CSV au registre fichier | poste |

## Ordre d'exécution

Règles générales : un fichier = un collage complet dans une **nouvelle** requête du SQL Editor, puis
« Run ». Ne jamais sélectionner une partie du texte. Lire le résultat avant de passer au fichier
suivant ; en cas d'erreur inattendue, s'arrêter (rien n'a été écrit par la requête en erreur).

### A. Projet de test `geonaf-test` (environnement isolé, à créer d'abord)

Second projet gratuit dans la même organisation, PostGIS activé (Database > Extensions > `postgis`,
schéma `extensions`), comme `geonaf-dev`.

1. `00_preflight.sql` : toutes les lignes `conforme = true` ; **la première (« texte exécuté en une
   seule transaction ») est bloquante** : si elle est fausse, ne rien exécuter d'autre.
2. `01_roles.sql`.
3. `02_migration_0001_simulation.sql` : attendu, l'erreur « GEONAF SIMULATION DE MIGRATION REUSSIE,
   AUCUNE ECRITURE ». Toute autre erreur : s'arrêter.
4. `02_migration_0001_application.sql` : une ligne `0001_territorial_registry`.
5. `03_mark_environment_test.sql` : `geonaf-environment:test`.
6. `04_verify_schema.sql` : toutes les lignes `conforme = true`.
7. `90_test_constraints.sql` : attendu, l'erreur « GEONAF TESTS REUSSIS, TOUT EST ANNULE : 21 cas
   conformes ». Sinon la liste des cas en échec.
8. Lots d'import (section C), puis `06_verify_import.sql` et `07_export_parity.sql` : répétition
   générale avant `geonaf-dev`.
9. Facultatif : `99_drop_schema_test.sql` pour repartir de zéro (le projet de test seulement).

### B. Projet `geonaf-dev`

Après validation de A, mêmes étapes 1 à 6 avec `03_mark_environment_dev.sql`. **Ne jamais exécuter
90 ni 99 sur `geonaf-dev`** (ils refusent d'ailleurs de tourner).

### C. Import des 97 territoires (sans connexion PostgreSQL)

Sur le poste, hors du dépôt (les fichiers contiennent des données, pas de secret) :

```powershell
python scripts/build_supabase_sql.py import --data-dir "<données>" --source-label "registre local" `
  --output-dir "$env:USERPROFILE\.geonaf\supabase-import"
```

Le script vérifie tout le registre fichier (empreintes des enregistrements, des fichiers et des
géométries, chaîne des versions, conversion MultiPolygon) avant d'écrire : 4 lots de 25 versions au
plus (environ 230 Ko chacun), leurs simulations, `06_verify_import.sql` et `manifest.json` (SHA-256
de chaque fichier, valeurs attendues). Sur le paquet v3 : 97 versions, 97 géométries, 2 versions de
source, empreinte globale `283c58d87f3ea989ff2b03856e5c8eb4654c0340c8a275dbf981abe4d383bbdb`.

Dans le SQL Editor, pour chaque lot dans l'ordre :

1. `05_import_lot_NN_simulation.sql` : attendu, l'erreur « GEONAF SIMULATION REUSSIE, AUCUNE
   ECRITURE : 25 versions, 25 a inserer, 0 deja presentes, empreinte du lot conforme ».
2. `05_import_lot_NN_application.sql` : une ligne avec `empreinte_conforme = true`.

Puis `06_verify_import.sql` : toutes les lignes `conforme = true` (97 versions, 97 géométries, 2
versions de source, empreinte globale, 92 `Polygon` / 5 `MultiPolygon` d'origine, aucune géométrie
invalide, colonnes cohérentes avec les documents, journaux clôturés, 3 unités qui intersectent le
carré Akanda par `ST_Intersects`, identiques au calcul shapely).

Enfin la parité complète : exécuter `07_export_parity.sql`, télécharger le résultat en CSV, puis

```powershell
python scripts/verify_supabase_export.py --csv "<export.csv>" --data-dir "<données>"
```

Attendu : « Parité confirmée ». Ce contrôle reconstruit chaque enregistrement depuis
`record_document`, recalcule son empreinte, le compare au registre fichier et compare le SHA-256 du
WKB renvoyé par PostGIS à celui de la conversion locale.

Rejouer un lot déjà appliqué est sans effet (« 0 à insérer, 25 déjà présentes ») ; un contenu
différent pour un territoire et une version déjà présents est refusé et rien n'est écrit.

## Sécurité

- Aucun identifiant Supabase dans le dépôt, les scripts ou les journaux ; les scripts s'exécutent
  dans l'éditeur authentifié, sans clé ni URL.
- Les rôles GEONAF sont `NOLOGIN` : aucun mot de passe à gérer tant qu'aucune connexion directe
  n'existe. Leur activation (`ALTER ROLE ... LOGIN PASSWORD`) se fera hors de l'éditeur.
- Le schéma `geonaf` ne doit **pas** être ajouté aux schémas exposés (Settings > API > Exposed
  schemas) ; `00` et `04` le vérifient.
- Les clés `anon` et `service_role` ne sont pas utilisées.

## Limites et risques

- Rien n'a été exécuté sur Supabase : syntaxe, contraintes, déclencheurs, RLS et aller-retour WKB ne
  sont vérifiés que hors ligne. Le projet de test sert précisément à les valider.
- Le comportement « une requête = une transaction » du SQL Editor est supposé ; la sonde de `00` le
  vérifie avant toute écriture.
- Limite de taille ou de durée de l'éditeur : lots de 230 Ko ; réduire `--batch-size` si nécessaire.
- L'offre gratuite met en pause un projet inactif ; la base n'est qu'une copie du registre fichier
  (restauration de référence : réimport). Les sauvegardes Supabase gratuites ne sont pas un moyen de
  restauration garanti.
- Les tests Python d'intégration (`-m postgis`) et Alembic restent non exécutés tant que le port
  5432 (connexion directe, IPv6 sur l'offre gratuite) ou 5432/6543 (pooler, IPv4) est fermé.
