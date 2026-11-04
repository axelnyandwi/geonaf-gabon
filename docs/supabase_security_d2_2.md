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
5. Résultat attendu : une seule grille. Ligne `-5` « transaction en lecture seule » = `on`. Si cette
   ligne manque ou vaut `off`, s'arrêter : l'éditeur n'a pas exécuté le texte en une seule
   transaction.
6. Recopier dans la fiche d'exécution (hors dépôt, hors OneDrive) les lignes `-3` (démarrage de
   l'instance, UTC) et `-2` (exécution, UTC), à côté du nom et de la référence relevés à l'étape 2.
7. Exporter le résultat (**Export > CSV**) sous `geonaf-test_08_<AAAAMMJJ>.csv` (ou `geonaf-dev_...`).
   Le ranger dans `%USERPROFILE%\.geonaf\exports\`, hors OneDrive et hors dépôt.
8. Fermer l'onglet de requête. Ne pas enregistrer la requête dans Studio.
9. Recommencer de 2 à 8 sur l'autre projet, **dans les 30 minutes**, pour que le critère I1
   s'applique.

### Identité des environnements (révisée le 2026-11-04, D2.4)

**Aucune valeur SQL lisible depuis l'éditeur n'identifie un projet Supabase.** L'identifiant système
de PostgreSQL (`pg_control_system().system_identifier`), utilisé jusqu'en D2.3, est le même pour
`geonaf-test` et `geonaf-dev`. Les exports du 2026-11-04 ont la même empreinte (`32f20b4465de`)
alors que leurs instances ont démarré à 16:08:10 et 16:49:52 UTC le 2026-11-02 et ont été
interrogées dans la même minute. Les projets sont visiblement créés à partir d'une même image déjà
initialisée. Cet identifiant a été retiré de 08 et de 10, et un test empêche de le réintroduire. La
référence du projet, la date de création et le nom ne sont pas exposés en SQL. Les paramètres qui
pourraient les contenir sont des secrets (`app.settings.*`, `pgrst.jwt_secret`) et ne sont jamais
lus. Les deux exports identiques du 2026-11-03 ne prouvent donc pas qu'un seul projet a été
inventorié : deux schémas hérités identiques donnent le même inventaire.

| # | Critère | Ce qu'il établit | Statut |
|---|---|---|---|
| I1 | Heures de démarrage (`-3`) différentes **et** les deux démarrages antérieurs aux deux exécutions (`-2`) | Deux instances PostgreSQL fonctionnaient en même temps, donc deux projets distincts (un projet = une instance ; offre gratuite sans réplique de lecture). Une même instance redémarrée entre les deux exécutions aurait une heure de démarrage postérieure à la première exécution | **Nécessaire** ; ne dit pas lequel est `test` |
| I2 | Nom et référence du projet relevés dans le tableau de bord au moment de chaque exécution, références différentes, fiche consignée | Quel export correspond à quel projet | **Décisif** pour l'attribution `test` / `dev` |
| I3 | Fichiers non identiques à l'octet près | Contrôle de cohérence | Nécessaire, pas suffisant |
| I4 | Même heure de démarrage dans 08 puis dans 10 (ou 09) sur un projet | Même instance entre les deux scripts. Si l'heure a changé (redémarrage), refaire I2 et relancer 08 | Continuité, pas identité |
| I5 | Après 03 : marque `geonaf-environment:<env>` écrite une fois, lue par 10, 04 et la couche D2.1 | Identité persistante dans la base | Seule preuve en base ; inexistante avant 0001 |

Remplacent C1 à C4 (D2.3). C1 (empreintes différentes) **n'était pas techniquement justifié** : il
reposait sur une valeur partagée par les projets. Aucun de ces critères ne dit que le renommage
est sans risque : ce sont les lignes de contrôle de 08 et 09 qui le disent.

**Critère de passage vers 09** : toutes les lignes non informatives sont `conforme = true` sur le
projet concerné, et les lignes d'information ont été relues (section 3).

## 2. Contrôles de sécurité obligatoires avant l'application de 0001

Ordre sur chaque projet : 08, simulation 09, ✋ application 09, 00, ✋ 01, **10**, simulation 02,
puis ✋ application 02. Le script 10 doit être entièrement conforme **et** les contrôles manuels
ci-dessous doivent être faits. Sinon, ne pas appliquer 0001.

| # | Contrôle | Où | Attendu |
|---|---|---|---|
| S1 | `10_security_precheck.sql` | SQL Editor | Toutes les lignes non informatives conformes ; ligne 0 « après renommage, avant 0001 » ; critères I2 et I4 vérifiés pour ce projet |
| S2 | Schémas exposés par la Data API | Tableau de bord : Settings > API (Data API), *Exposed schemas* et *Extra search path* | Ni `geonaf` ni `geonaf_legacy_draft`. Le contrôle SQL (ligne 41) ne voit que les paramètres de rôle, pas la configuration de la plateforme |
| S3 | Membres de l'organisation et du projet | Organization > Team | Liste nominative connue ; authentification multifacteur activée pour chacun ; aucun compte partagé |
| S4 | Mot de passe de la base `postgres` | Database > Settings | Jamais copié dans le dépôt, un `.env` suivi, un ticket ou un message ; changé s'il a circulé |
| S5 | Clés `anon` et `service_role` | Settings > API | Non utilisées par GEONAF ; `service_role` absente de tout poste, script ou page Streamlit |
| S6 | Usage externe du schéma hérité | Auteur du schéma | Confirmation qu'aucune Edge Function, requête enregistrée, tâche ou outil externe n'utilise `geonaf` par son nom |
| S7 | Lignes d'information de 08 et 10 | Section 3 | Relues et consignées dans le rapport d'exécution |
| S8 | Résultat de `00_preflight.sql` | SQL Editor | Première ligne (une seule transaction) conforme |

### Exécution anticipée de 10 (lecture seule, avant 09 et 01)

Elle permet de repérer un problème de sécurité avant toute décision de renommage, sans rien écrire.
Même procédure que 08 (section 1, étapes 1 à 9), fichier `geonaf-<env>_10_<AAAAMMJJ>.csv`.

- La ligne 0 doit indiquer « avant renommage (09 non appliqué) » et « rôles GEONAF absents (01 non
  appliqué) ». Les lignes 2, 3, 20 et 21 sont alors attendues fausses.
- **Toute autre ligne non informative fausse bloque la suite.** C'est particulièrement vrai pour
  l.11 et l.12 (masquage de PostGIS), l.14 (droit `CREATE` sur `extensions`) et l.41 (Data API).
- Cohérence avec 08 : mêmes heures `-3` (critère I4), PostGIS dans `extensions`, mêmes déclencheurs
  d'événement (l.34 et 08 l.27).
- R5 et R5 bis ne sont pas appliqués. La ligne 11 reste bloquante dans tous les cas : même avec le
  chemin `pg_catalog, extensions, pg_temp` dans les scripts, la couche Python (R5 bis) utilise le
  chemin par défaut de la connexion, qui contient `public`.
- Après 09 et 01, relancer 10 : toutes les lignes non informatives doivent être conformes (S1).

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
| R13 | Identification | Coller un script dans le mauvais projet. 08 et 09 ne peuvent pas s'appuyer sur une marque : les deux schémas hérités n'en ont pas | Critères I1 à I4 (section 1), triple vérification manuelle, ordre test puis dev. L'identifiant système de PostgreSQL est partagé entre projets et n'est plus utilisé | Fiche d'exécution hors dépôt (référence, nom, heures `-3` et `-2`) ; envisager une marque d'identité posée avant 09 (décision à prendre : c'est une écriture) |
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
