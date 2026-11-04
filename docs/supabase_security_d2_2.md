# D2.2 - Sécurité de la migration Supabase

Complète `docs/supabase_legacy_schema_plan.md` (ordre des étapes) et `docs/supabase_registry.md`.
**Rien n'a été exécuté sur Supabase.** Les scripts sont vérifiés hors ligne : analyse syntaxique par
le parseur de PostgreSQL 17 (`pglast`) et tests statiques. Les noms de colonnes des catalogues, les
droits réels et le comportement de l'éditeur ne seront confirmés qu'à l'exécution sur `geonaf-test`.

## 1. Procédure manuelle : script 08 sur chaque projet

À faire d'abord sur `geonaf-test`, puis sur `geonaf-dev`, **dans deux sessions distinctes**.

1. Ouvrir le tableau de bord Supabase avec un compte protégé par l'authentification multifacteur.
2. Choisir le projet dans la liste de l'organisation. Vérifier **trois fois** qu'il s'agit du bon :
   - le nom affiché en haut de page ;
   - la référence du projet dans l'URL du navigateur (`/project/<ref>/...`) ;
   - le libellé de l'onglet.

   Ne noter la référence nulle part dans le dépôt.
3. SQL Editor > **New query** (une requête vide, jamais une requête existante).
4. Ouvrir `db/supabase/08_inspect_legacy_schema.sql` dans l'éditeur local. Tout sélectionner, copier,
   coller, puis **Run**. Ne jamais exécuter une partie du texte seulement.
5. Résultat attendu : une seule grille. Ligne `-3` « transaction en lecture seule » = `on`. Si cette
   ligne manque ou vaut `off`, s'arrêter : l'éditeur n'a pas exécuté le texte en une seule
   transaction.
6. Exporter le résultat (**Export > CSV**) sous `geonaf-test_08_<AAAAMMJJ>.csv` (ou `geonaf-dev_...`).
   Le ranger dans `%USERPROFILE%\.geonaf\exports\`, hors OneDrive et hors dépôt.
7. Fermer l'onglet de requête. Ne pas enregistrer la requête dans Studio.
8. Recommencer de 2 à 7 sur l'autre projet.

**Distinguer les deux projets** : la ligne `-2` (empreinte de l'identifiant système, md5 tronqué)
doit **différer** entre les deux exports. Si elle vaut « non disponible », comparer la ligne `-1`
(démarrage du serveur et version) et l'identification manuelle de l'étape 2. Deux exports
identiques, comme ceux du 2026-11-03, signifient que le même projet a été inventorié deux fois.
Après la migration 0001, la marque `geonaf-environment:<env>` (script 03) prend le relais et la
couche D2.1 la vérifie à chaque session.

**Critère de passage vers 09** : toutes les lignes non informatives sont `conforme = true` sur le
projet concerné, et les lignes d'information ont été relues (section 3).

## 2. Contrôles de sécurité obligatoires avant l'application de 0001

Ordre sur chaque projet : 08, simulation 09, ✋ application 09, 00, ✋ 01, **10**, simulation 02,
puis ✋ application 02. Le script 10 doit être entièrement conforme **et** les contrôles manuels
ci-dessous doivent être faits. Sinon, ne pas appliquer 0001.

| # | Contrôle | Où | Attendu |
|---|---|---|---|
| S1 | `10_security_precheck.sql` | SQL Editor | Toutes les lignes non informatives conformes ; empreinte (ligne `-2`) identique à celle de 08 sur ce projet |
| S2 | Schémas exposés par la Data API | Tableau de bord : Settings > API (Data API), *Exposed schemas* et *Extra search path* | Ni `geonaf` ni `geonaf_legacy_draft`. Le contrôle SQL (ligne 41) ne voit que les paramètres de rôle, pas la configuration de la plateforme |
| S3 | Membres de l'organisation et du projet | Organization > Team | Liste nominative connue ; authentification multifacteur activée pour chacun ; aucun compte partagé |
| S4 | Mot de passe de la base `postgres` | Database > Settings | Jamais copié dans le dépôt, un `.env` suivi, un ticket ou un message ; changé s'il a circulé |
| S5 | Clés `anon` et `service_role` | Settings > API | Non utilisées par GEONAF ; `service_role` absente de tout poste, script ou page Streamlit |
| S6 | Usage externe du schéma hérité | Auteur du schéma | Confirmation qu'aucune Edge Function, requête enregistrée, tâche ou outil externe n'utilise `geonaf` par son nom |
| S7 | Lignes d'information de 08 et 10 | Section 3 | Relues et consignées dans le rapport d'exécution |
| S8 | Résultat de `00_preflight.sql` | SQL Editor | Première ligne (une seule transaction) conforme |

Après 0001 : `04_verify_schema.sql` (aucun droit pour les rôles API ni pour `PUBLIC`, RLS partout,
fonctions à `search_path` fixe, vues en `security_invoker`), puis le diagnostic D2.1 depuis un réseau
autorisé.

## 3. Lecture des lignes d'information

| Script, ligne | Valeur normale | À signaler |
|---|---|---|
| 08 l.6 et l.22 propriétaires | `postgres` | `supabase_admin` : le renommage échouera (08 l.23 = false). Ne pas contourner : demander l'avis de l'administrateur |
| 08 l.12 politiques | 0 | Des politiques : quelqu'un a configuré l'accès, c'est un indice d'usage |
| 08 l.19.5 tâches pg_cron | `pg_cron absent` ou seulement `postgres` | Le filtrage RLS de `cron.job` cache les tâches des autres rôles. Vérifier aussi la page Cron du tableau de bord, si l'intégration est activée |
| 08 l.21 droits par défaut sur geonaf | 0 | Ils suivraient le schéma renommé |
| 08 l.25 autres sessions | Quelques sessions de la plateforme | Une session applicative inconnue |
| 08 l.27 / 10 l.34 déclencheurs d'événement | Fonctions des schémas `extensions`, `pgbouncer` ou de la plateforme (par exemple `pgrst_ddl_watch`, `pgrst_drop_watch`, `issue_pg_graphql_access`, `issue_graphql_placeholder`, `issue_pg_cron_access`, `issue_pg_net_access`) | Tout déclencheur dont la fonction est dans `public` ou appartient à un rôle inconnu : il s'exécute pendant `ALTER SCHEMA` et pendant 0001 |
| 10 l.13 CREATE sur `public` | Valeur par défaut du projet, à consigner | Si `anon` ou `authenticated` y peuvent créer, le masquage de PostGIS est possible : la ligne 11 doit rester à 0 juste avant 02 |
| 10 l.15 `search_path` | Valeurs de la plateforme (`authenticator`, `postgres`...) | `geonaf` dans un `search_path` |
| 10 l.25 membres des rôles GEONAF | `postgres` (option admin, accordée au créateur depuis PostgreSQL 16) | Tout autre rôle |
| 10 l.30 SUPERUSER ou BYPASSRLS | Rôles de la plateforme (`supabase_admin`, `service_role`, `postgres` selon la version) | Un rôle inconnu, surtout avec `(login)` |
| 10 l.31 `pg_read_all_data`, `pg_write_all_data` | Rôles de la plateforme (par exemple `supabase_read_only_user`) | Un rôle inconnu : il lit `geonaf` sans aucun droit de table |
| 10 l.32 droits par défaut globaux | À consigner | Toute entrée pour `anon`, `authenticated`, `service_role` ou `PUBLIC` : 0001 les révoque après création, mais toute future migration devra le faire aussi |

## 4. Risques de cybersécurité

| # | Domaine | Risque | Mesure en place | Reste à faire |
|---|---|---|---|---|
| R1 | Rôles | `postgres` (éditeur SQL) est administrateur : qui accède au tableau de bord contrôle la base | Rôles `geonaf_reader`/`geonaf_importer` en `NOLOGIN` ; aucune connexion directe | S3, S4 ; plus tard un rôle de migration distinct et des rôles `LOGIN` à mot de passe généré hors de l'éditeur |
| R2 | Rôles | `01_roles.sql` est idempotent : un rôle `geonaf_*` préexistant (LOGIN, BYPASSRLS, membre d'un rôle privilégié) serait réutilisé en silence | 10 l.20 à l.23 bloquantes | - |
| R3 | RLS | RLS activée sans politique : le propriétaire la contourne, sauf FORCE RLS. Un comptage peut afficher 0 alors que la table contient des lignes | 08 l.4 et la garde de 09 tiennent compte de `relforcerowsecurity` | - |
| R4 | RLS | `service_role` et les rôles `BYPASSRLS` ignorent la RLS : seuls les droits de table protègent `geonaf` | 0001 révoque tout aux rôles API ; 04 le vérifie | Vérifier 04 après toute future migration |
| R5 | search_path | Le wrapper Supabase de 0001 utilise `pg_catalog, public, extensions`. Un objet `geometry` ou `st_*` placé dans `public` (ou dans `pg_temp`) serait lié **définitivement** aux contraintes et index de 0001 | 10 l.11 et l.12 bloquantes ; fonctions de 0001 à `search_path = ''` | **Correction proposée, à valider** : `pg_catalog, extensions` dans `src/data/supabase_sql.py` (le corps de 0001 n'utilise rien de `public`), puis régénération des fichiers 02. Le hash du corps resterait inchangé |
| R6 | Privilèges hérités | Les droits par défaut globaux s'appliquent à tout nouvel objet. Les droits par défaut d'un schéma s'ajoutent aux globaux sans pouvoir les retirer | 0001 révoque après création (tables, séquences, fonctions) | Règle pour 0002 et suivantes : `REVOKE` explicite après chaque création, contrôle 04 étendu |
| R7 | Privilèges hérités | Les membres de `pg_read_all_data` lisent toutes les tables sans droit de table | Liste en 10 l.31 | Décider si ces rôles de la plateforme sont acceptables (avis de l'administrateur) |
| R8 | Fonctions | Droit `EXECUTE` à `PUBLIC` par défaut sur toute nouvelle fonction | 0001 : `REVOKE ALL ON ALL FUNCTIONS ... FROM PUBLIC` et rôles API | Même règle en 0002 |
| R9 | Data API | Un schéma exposé via le tableau de bord n'apparaît pas forcément dans les paramètres de rôle | Contrôle manuel S2 ; aucun droit `USAGE` pour les rôles API (04) | - |
| R10 | Data API | Paramètres `pgrst.*` pouvant contenir un secret (`pgrst.jwt_secret`) | 10 n'affiche que `pgrst.db_schemas` ; test qui interdit toute autre lecture | - |
| R11 | Déclencheurs d'événement | Ils s'exécutent pendant `ALTER SCHEMA` et 0001, avec les droits de leur fonction | Listés en 08 l.27 et 10 l.34 | Relecture humaine (section 3) |
| R12 | Renommage | Les références **par nom** (fonctions, `search_path`, `pg_cron`, code externe) viseraient, après 0001, les nouvelles tables `territories`/`territory_versions` | Bloquants en 08 et 09 ; verrou des 8 tables pendant 09 | S6 (code externe, invisible depuis SQL) |
| R13 | Identification | Coller un script dans le mauvais projet. 08 et 09 ne peuvent pas s'appuyer sur une marque : les deux schémas hérités n'en ont pas | Empreinte du serveur, triple vérification manuelle, ordre test puis dev | Consigner l'empreinte de chaque projet dans le rapport d'exécution, pas dans le dépôt |
| R14 | Éditeur | Si l'éditeur n'exécute pas tout en une transaction, un échec pourrait laisser un état partiel | Sonde de 00 ; lecture seule imposée (08, 10) | - |
| R15 | Poste | Exports CSV et futur `.env` de connexion | Worktree hors OneDrive ; `.env` ignoré par Git | Exports dans `%USERPROFILE%\.geonaf\exports\` |
| R16 | Dépôt | Le `.git` du dépôt principal est dans OneDrive : objets synchronisés (aucun secret suivi, vérifié), risque de corruption par la synchronisation | - | Décision séparée : déplacer le dépôt hors OneDrive |

## 5. Opérations nécessitant une validation humaine

1. ✋ Application de 09 (renommage), projet par projet, après 08 conforme.
2. ✋ `01_roles.sql`, puis ✋ application de 02 (migration 0001), puis ✋ 03 (marque).
3. Correction R5 du `search_path` du wrapper : modifie les fichiers 02 relus.
4. Décision sur les rôles de la plateforme listés en 10 l.30 et l.31.
5. Activation future de `LOGIN` pour les rôles GEONAF et choix d'un rôle de migration distinct.
6. Suppression éventuelle de `geonaf_legacy_draft` : hors périmètre, autorisation séparée.
