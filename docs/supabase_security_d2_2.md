# D2.2 - Sécurité de la migration Supabase

Complète `docs/supabase_legacy_schema_plan.md` (ordre des étapes) et `docs/supabase_registry.md`.
À sa rédaction (D2.2), rien n'avait été exécuté sur Supabase. Les scripts étaient alors vérifiés hors
ligne par analyse syntaxique (`pglast`) et tests statiques. **Une analyse syntaxique ne résout pas les
types** : elle ne pouvait pas détecter l'erreur 42725 de 10 l.32 (section 7). Depuis D2.10a, les scripts
sont exécutés réellement sur un PostgreSQL local jetable (16.2, sans PostGIS ni libxml), avec `EXPLAIN`
de chaque requête pour la résolution des types : **PostgreSQL 17 n'est pas validé localement**. Les
droits réels et le comportement de l'éditeur ne sont confirmés qu'à l'exécution sur `geonaf-test`.

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
- R5 est appliqué aux scripts (D2.7) ; R5 bis ne l'est pas. La ligne 11 reste bloquante dans tous
  les cas : la couche Python (R5 bis) utilise le chemin par défaut de la connexion, qui contient
  `public` (relevé sur les deux projets le 2026-11-04 : `postgres` a `"$user", public, extensions`).
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
| R5 | search_path | Avec `pg_catalog, public, extensions`, un objet `geometry` ou `st_*` placé dans `public` (ou dans `pg_temp`) serait lié **définitivement** aux contraintes et index de 0001 | **Appliqué en D2.7** : tout script qui résout un nom PostGIS (02, lots 05, 06, 07, 90) fixe d'abord `pg_catalog, extensions, pg_temp`. `public` n'est jamais parcouru et `pg_temp` vient en dernier (les lots trouvent leurs tables temporaires). Corps de 0001 inchangé (`719e7e84...`). Tests de non-régression ; 10 l.11 et l.12 restent bloquantes | Valider sur `geonaf-test` par la simulation 02 |
| R5 bis | search_path (Python) | `PostgisTerritoryRegistry` et `read_only_session()` utilisent le chemin par défaut de la connexion, qui contient `public` : `ST_Intersects`, `ST_MakeEnvelope`, `ST_AsBinary` et `ST_GeomFromWKB` pourraient être masqués. `geometry()` est protégé par l'empreinte recalculée, **`intersecting()` ne l'est pas** | Aucune connexion possible depuis le poste (ports fermés) ; inventaire des appels figé par un test | Lot dédié : chemin local à la transaction construit depuis `pg_extension`, passage du registre par `read_only_session()` |
| R6 | Privilèges hérités | Les droits par défaut globaux s'appliquent à tout nouvel objet. Les droits par défaut d'un schéma s'ajoutent aux globaux sans pouvoir les retirer | 0001 révoque après création (tables, séquences, fonctions) | Règle pour 0002 et suivantes : `REVOKE` explicite après chaque création, contrôle 04 étendu |
| R7 | Privilèges hérités | Les membres de `pg_read_all_data` lisent toutes les tables sans droit de table | Liste en 10 l.31 | Décider si ces rôles de la plateforme sont acceptables (avis de l'administrateur) |
| R8 | Fonctions | Droit `EXECUTE` à `PUBLIC` par défaut sur toute nouvelle fonction | 0001 : `REVOKE ALL ON ALL FUNCTIONS ... FROM PUBLIC` et rôles API | Même règle en 0002 |
| R9 | Data API | Un schéma exposé via le tableau de bord n'apparaît pas forcément dans les paramètres de rôle | Contrôle manuel S2 ; aucun droit `USAGE` pour les rôles API (04) | - |
| R10 | Data API | Paramètres `pgrst.*` pouvant contenir un secret (`pgrst.jwt_secret`) | 10 n'affiche que `pgrst.db_schemas` ; test qui interdit toute autre lecture | - |
| R11 | Déclencheurs d'événement | Ils s'exécutent pendant `ALTER SCHEMA` et 0001, avec les droits de leur fonction. Un `NOTIFY` est annulé avec la requête ; **un `nextval` ne l'est pas** (pg_graphql incrémente `graphql.seq_schema_version` à chaque DDL et reconstruit alors son cache) | Listés en 08 l.27 et 10 l.34 ; le message de succès de la simulation 09 liste ceux qui se sont exécutés, avec `(*)` si leur code peut avoir un effet non transactionnel. Comportement démontré par un test d'intégration (D2.9) | Relecture humaine (section 3) |
| R12 | Renommage | Les références **par nom** (fonctions, `search_path`, `pg_cron`, code externe) viseraient, après 0001, les nouvelles tables `territories`/`territory_versions` | Bloquants en 08 et 09 (code **et** `search_path` des fonctions, `pg_proc.proconfig`, depuis l'audit du 2026-11-05) ; verrou des 8 tables pendant 09 | S6 (code externe, invisible depuis SQL) |
| R13 | Identification | Coller un script dans le mauvais projet. 08 et 09 ne peuvent pas s'appuyer sur une marque : les deux schémas hérités n'en ont pas | Critères I1 à I4 (section 1), triple vérification manuelle, ordre test puis dev. L'identifiant système de PostgreSQL est partagé entre projets et n'est plus utilisé | Fiche d'exécution hors dépôt (référence, nom, heures `-3` et `-2`) ; envisager une marque d'identité posée avant 09 (décision à prendre : c'est une écriture) |
| R14 | Éditeur | Si l'éditeur n'exécute pas tout en une transaction, un échec pourrait laisser un état partiel | Sonde de 00 ; lecture seule imposée (08, 10) ; 09 : une seule instruction `DO`, atomique par elle-même quel que soit l'éditeur (D2.9) | - |
| R17 | Éditeur | **Sélection partielle** : le SQL Editor exécute le texte sélectionné. Avant D2.9, la ligne de renommage de 09 seule renommait sans garde, même dans le fichier de simulation, et la ligne `SELECT ... INTO found` seule créait une table `public.found` (`SELECT INTO` hors PL/pgSQL = `CREATE TABLE AS`) : 122 plages de lignes dangereuses mesurées sur PostgreSQL 16 | D2.9 : renommage et commentaire construits par `format()` à l'intérieur du `DO`, plus de `SELECT INTO`, aucune instruction exécutable dans les commentaires. Test d'intégration : aucune plage de lignes des trois scripts 09 ne modifie la base (état initial et état renommé) ; test statique sur le texte brut | Une sélection qui commence ou finit au milieu d'une ligne n'est couverte que par le test statique |
| R18 | Retour arrière | Un objet créé **après** le renommage qui cite `geonaf_legacy_draft` par son nom (fonction, paramètre, tâche `pg_cron`) serait cassé par le retour arrière ; le script ne le détecte pas | Retour arrière réservé aux incidents, avant 0001, juste après l'application ; 10 l.20 et l.21 relues avant | Relancer 08 et l'empreinte après le retour arrière |
| R15 | Poste | Exports CSV et futur `.env` de connexion | Worktree hors OneDrive ; `.env` ignoré par Git | Exports dans `%USERPROFILE%\.geonaf\exports\` |
| R16 | Dépôt | Le `.git` du dépôt principal est dans OneDrive : objets synchronisés (aucun secret suivi, vérifié), risque de corruption par la synchronisation | - | Décision séparée : déplacer le dépôt hors OneDrive |

## 5. Opérations nécessitant une validation humaine

1. ✋ Simulation de 09 sur `geonaf-test` (section 6). Elle n'écrit rien dans `geonaf`, mais exécute un
   DDL annulé, pose des verrous sur les 8 tables pendant la requête et déclenche les déclencheurs
   d'événement (R11).
2. ✋ Application de 09 (renommage), projet par projet, après 08 conforme et simulation réussie.
3. ✋ `01_roles.sql`, puis ✋ application de 02 (migration 0001), puis ✋ 03 (marque).
4. Push de la branche `db/d2a-territory-registry` (sécurisation D2.9 `f6ab959` et audit D2.9 du
   2026-11-05 : commits locaux, non poussés).
5. Décision sur les rôles de la plateforme listés en 10 l.30 et l.31.
6. Activation future de `LOGIN` pour les rôles GEONAF et choix d'un rôle de migration distinct.
7. Suppression éventuelle de `geonaf_legacy_draft` : hors périmètre, autorisation séparée.
8. ✋ Retour arrière (`09_rename_legacy_schema_rollback.sql`) : seulement en cas d'incident, avant 0001.

## 6. Simulation 09 sur geonaf-test (D2.9, préparée, non exécutée)

### Conception

| Exigence | Réponse | Preuve |
|---|---|---|
| Une sélection partielle ne doit rien écrire | La simulation est **une seule instruction `DO`**. Une sélection partielle est une erreur de syntaxe. Le renommage et le commentaire n'existent que sous forme de modèles `format('ALTER SCHEMA %I RENAME TO %I', ...)`, construits à l'exécution ; aucun `SELECT ... INTO` ; aucune instruction exécutable dans les commentaires | Test d'intégration : chaque plage de lignes contiguës (et chaque fin de ligne de commentaire) des trois scripts est exécutée seule ; aucune ne modifie la base. Contre-épreuve : 122 plages dangereuses dans les scripts d'avant D2.9 |
| Ne pas compter sur un `ROLLBACK` final | Il n'y a ni `BEGIN`, ni `ROLLBACK`, ni `COMMIT` de niveau requête (interdits par test). Le renommage se fait dans un sous-bloc PL/pgSQL `BEGIN ... EXCEPTION` : une **sous-transaction**, annulée par le signal interne `GN009`. Le bloc vérifie ensuite que le nom, l'OID et le commentaire sont revenus à l'état initial, puis l'erreur finale annule toute la requête (deuxième annulation, indépendante) | Documentation PostgreSQL (PL/pgSQL, « Trapping Errors » : les modifications de l'état persistant faites dans le bloc sont annulées) ; test d'intégration : empreinte identique avant et après |
| Seul le signal interne est intercepté | `EXCEPTION WHEN SQLSTATE 'GN009'`, jamais `WHEN OTHERS` : un refus de droits, un déclencheur d'événement en erreur ou un verrou indisponible remontent tels quels. L'indicateur `rename_tested`, positionné juste avant le signal, prouve que le `GN009` intercepté est bien le sien : un `GN009` levé ailleurs (par un déclencheur d'événement, avant le renommage) donne « signal GN009 inattendu » au lieu d'un faux succès (faille trouvée en revue, D2.9) | Test statique ; test d'intégration avec un déclencheur piège sur `ddl_command_start` et `ddl_command_end` |
| Simulation sans renommage réel ? | Étudiée et **non retenue seule** : la garde lit déjà toutes les préconditions vérifiables par catalogue (droits du propriétaire, `CREATE` sur la base, nom cible libre, dépendances). Elle ne peut pas vérifier ce que seul le DDL déclenche : déclencheurs d'événement de la plateforme, crochets d'extension (`supautils`), verrous réels sur le catalogue. Le renommage en sous-transaction est la seule façon de les éprouver sans rien conserver | - |
| `BEGIN`, contrôles, renommage, `ROLLBACK` explicites ? | **Rejeté** : une sélection qui omet le `ROLLBACK` laisse une transaction ouverte sur une connexion de l'éditeur (validée ou annulée selon son pool, inconnu) ; une sélection de la seule ligne de renommage la valide aussitôt | - |
| Confusion simulation / application | L'application et le retour arrière **refusent de s'exécuter tels qu'ils sont versionnés** : il faut remplacer `AAAA-MM-JJ HH:MM:SS.UUUUUU` par la ligne `-3` de 08 (démarrage de l'instance, UTC, microsecondes), lue juste avant sur le même projet. La simulation n'a rien à modifier. Coller le mauvais fichier arrête tout avant la garde | Tests d'intégration et statiques |
| État identique avant et après | `09_legacy_schema_fingerprint.sql` (lecture seule) : OID et commentaire exact (JSON) des deux schémas, nombre de tables et SHA-256 d'un texte canonique (schéma, relations, colonnes, contraintes, index, politiques, déclencheurs, droits du schéma, des relations et des colonnes, privilèges par défaut). Les lignes sont comptées par 08 l.5 | Test d'intégration |

**Garanties de PostgreSQL utilisées.** Plusieurs instructions envoyées en une requête forment une
transaction implicite : une erreur annule tout (démontré par test). Une instruction `DO` est atomique
par elle-même. Une sous-transaction annulée défait le DDL qu'elle contient ; un `NOTIFY` émis dedans
n'est jamais délivré (démontré par test).

**Ce que PostgreSQL n'annule pas** (démontré par test avec un déclencheur d'événement qui imite celui de
pg_graphql) : les incréments de séquence (`nextval`), ainsi que les statistiques, les journaux et la
consommation d'identifiants de transaction. Aucun ne touche `geonaf`.

**Non vérifié** (pas d'accès, aucune documentation officielle trouvée) : la façon dont le SQL Editor
envoie le texte (une requête ou plusieurs), l'exécution de la seule sélection, les déclencheurs
d'événement et les crochets réels de Supabase, le comportement sous PostgreSQL 15 ou 17 (tests
exécutés sur PostgreSQL 16.2). La conception ne dépend pas des points propres à l'éditeur.

### Audit du 2026-11-05 (D2.9, avant autorisation)

Revue des quatre scripts 09 sur `42a4c6e`, avec exécution sur un PostgreSQL 16.2 local jetable (jamais
Supabase). La conception (une seule instruction `DO`, sous-transaction annulée par `GN009`, double
annulation, confirmation par l'heure de démarrage, retour arrière qui lit le commentaire initial dans la
marque) est conservée. Deux lacunes démontrées par reproduction, puis corrigées :

| Lacune | Reproduction avant correction | Correction | Test |
|---|---|---|---|
| Une fonction hors `geonaf` déclarée `SET search_path = geonaf` n'était pas vue : le chemin est dans `pg_proc.proconfig`, pas dans `prosrc`. Après 09 puis 0001, elle se lierait en silence aux **nouvelles** tables (R12) | Simulation « REUSSIE » avec `public.probe()` présente | Garde de 09 (simulation et application, identiques), 08 l.15 et 10 l.20 lisent aussi `proconfig` | Intégration : simulation et application refusées, empreinte inchangée ; 10 l.20 non conforme ; statique sur les quatre fichiers |
| L'empreinte ignorait les droits de colonnes (`attacl`) et les privilèges par défaut du schéma (`pg_default_acl`) | Empreinte identique après `GRANT SELECT (label)` et `ALTER DEFAULT PRIVILEGES` | Ajout des deux éléments au texte canonique (`defaclobjtype::text`, cf. D2.10a) | Intégration : trois empreintes distinctes ; statique |

L'empreinte de 09 n'a jamais été exportée sur un projet : la modifier ne casse aucune référence. Elle
est distincte de l'empreinte de l'export D utilisée par 10 (inchangée).

### Prérequis, tous requis

- 08 et 10 du 2026-11-04 conformes (constat D2.7 : 08 sans ligne fausse ; 10 anticipé, seules les
  lignes 2, 3, 20 et 21 fausses, comme attendu).
- I1, I3 et I4 satisfaits (`geonaf-test` : démarrage 2026-11-02 16:49:52 UTC ; `geonaf-dev` : 16:08:10).
- S2 : Data API désactivée sur les deux projets (constat déclaré le 2026-11-04).
- 08 l.27 relue : chaque déclencheur d'événement est connu (section 3).
- Autorisation explicite de la simulation, pour `geonaf-test` seulement.
- 08 dans sa version **actuelle** (l.15 étendue au `search_path` des fonctions, audit du 2026-11-05) :
  le constat du 2026-11-04 ne couvre pas ce contrôle. L'étape 2 le relance ; l.15 doit valoir `0`.

### Procédure (`geonaf-test` seulement)

1. Tableau de bord avec authentification multifacteur. Vérifier le nom et la référence du projet
   `geonaf-test` et les noter dans la fiche, avec l'heure (I2).
2. New query : coller **tout** `08_inspect_legacy_schema.sql`, ne rien sélectionner, Run. Toutes les
   lignes conformes ; la ligne `-3` commence par `2026-11-02 16:49:52` (I4). Exporter. Sinon, s'arrêter.
3. New query : coller **tout** `09_legacy_schema_fingerprint.sql`, ne rien sélectionner, Run.
   `lecture_seule = on`, `oid_geonaf_legacy_draft = absent`, `commentaire_geonaf_json = null`,
   `tables = 8`. Exporter (empreinte **avant**).
4. New query : coller **tout** `09_rename_legacy_schema_simulation.sql`. Avant Run, vérifier :
   première ligne `... 09 rename of the legacy geonaf schema: SIMULATION. Writes nothing.` ; aucune
   ligne `confirmation constant` dans le texte ; **aucune sélection** (cliquer une fois dans le texte).
5. Run, puis lire le résultat :

   | Message | Signification | Suite |
   |---|---|---|
   | `GEONAF SIMULATION DE RENOMMAGE REUSSIE, AUCUNE ECRITURE`, `DETAIL` qui commence par `declencheurs d'evenement executes :` | Garde franchie, renommage éprouvé puis annulé deux fois | Recopier le `DETAIL` dans la fiche, puis étape 6 |
   | `GEONAF: renommage refuse` + `DETAIL` | Un bloquant est apparu depuis 08 | S'arrêter ; transmettre le `DETAIL` |
   | `lock not available` (55P03) | Une autre session tient un verrou (`NOWAIT`) | Une seule nouvelle tentative quelques minutes plus tard ; sinon s'arrêter |
   | `schema geonaf marque`, `tables inattendues`, `geonaf_legacy_draft existe deja`, `droits du proprietaire` | L'état ne correspond plus à l'inventaire | S'arrêter |
   | `ETAT NON RESTAURE APRES LA SIMULATION` | Ne devrait jamais arriver (la requête est quand même annulée) | S'arrêter, ne rien relancer, transmettre |
   | `signal GN009 inattendu, simulation non concluante` | Un déclencheur d'événement (ou autre code) a levé le code réservé GN009 : le renommage n'a pas été éprouvé ; rien n'est écrit | S'arrêter ; relire 08 l.27 ; ne pas appliquer 09 |
   | `APPLICATION REFUSEE` ou `RETOUR ARRIERE REFUSE` | **Mauvais fichier collé** | S'arrêter ; rien n'a été fait ; recommencer à l'étape 4 avec le bon fichier |
   | Résultat sans erreur (« Success ») | Ne peut pas venir du fichier de simulation | S'arrêter ; étape 6 immédiatement, puis transmettre |
   | Toute autre erreur (`must be owner`, `permission denied`, erreur de syntaxe) | Non prévu ; une erreur de syntaxe signale une sélection partielle ou un collage incomplet | S'arrêter, ne rien relancer |

6. New query : relancer **tout** `09_legacy_schema_fingerprint.sql`. Toutes les colonnes doivent être
   identiques à l'étape 3, sauf `execution_utc`. Puis relancer **tout** 08 : identique à l'étape 2,
   sauf `-2` et éventuellement 25 (sessions).
7. Exporter les résultats et le message de 09 sous `geonaf-test_09sim_<AAAAMMJJ>.*`, hors dépôt et
   hors OneDrive. Fermer les onglets sans les enregistrer.

**Conditions d'arrêt** : toute ligne non conforme en 08, toute empreinte différente, tout message autre
que le succès, un déclencheur d'événement inconnu dans le `DETAIL`, un doute sur le projet.

**Ne pas simuler sur `geonaf-dev` ni appliquer 09 sans une nouvelle autorisation, après examen du
résultat.**

### Retour arrière (incident ou décision, avant 0001 seulement)

`09_rename_legacy_schema_rollback.sql`, ✋ après autorisation, avec la même confirmation que
l'application. Il refuse si un schéma `geonaf` existe (0001 appliquée), si `geonaf_legacy_draft`
n'a pas exactement la marque écrite par 09, ou si ses tables diffèrent des 8 attendues. Il redonne au
schéma son nom (même OID : tables, droits, politiques et lignes inchangés), puis **le commentaire
initial exact lu dans la marque**, qui se termine par `; commentaire initial (JSON) : <null ou chaîne>`.
Il ne suppose donc pas que ce commentaire était NULL. La garde de 09 n'accepte aujourd'hui qu'un
schéma sans commentaire, mais la marque conserve la valeur quelle qu'elle soit. Une marque dont la
valeur ne peut pas être un vrai commentaire (chaîne vide, nombre, objet, JSON invalide) est refusée.
Après le retour arrière, l'empreinte doit être identique à celle exportée avant l'application.

L'ancienne commande d'une ligne (renommage inverse seul) laissait la marque `geonaf-legacy:` sur
`geonaf` : 08 et 09 auraient ensuite refusé le schéma. Elle n'est plus documentée (test).

## 7. Écarts entre scripts versionnés et exécutions (D2.10a, 2026-11-04)

Erreur PostgreSQL 42725 (opérateur non unique) : `text || "char"` est ambigu. Toute colonne de catalogue de
type `"char"` (`relkind`, `contype`, `defaclobjtype`...) doit être convertie (`::text`) avant `||`.
`concat_ws`, `format` et les comparaisons ne sont pas concernés. Garde-fou :
`tests/test_sql_char_concatenation.py`. Ce test liste les 42 colonnes `"char"` du catalogue, contrôle tous
les SQL suivis par Git et fait un `EXPLAIN` de chaque requête des scripts en lecture seule.

| Script | Écart | Preuve | Statut |
|---|---|---|---|
| `10_security_precheck.sql` l.32 | `d.defaclobjtype \|\| ':'` (42725) | Reproduit en local ; présent depuis `5693659` (D2.2) | **Corrigé** (`::text`). Sur `geonaf-test`, une correction manuelle a donné le CSV favorable : son texte exact n'est pas connu |
| `10_security_precheck.sql`, exécution anticipée du 2026-11-04 (09:16:32 UTC, `geonaf-test` et `geonaf-dev`) | Résultats rapportés (lignes 2, 3, 20 et 21 fausses) alors que la version versionnée échoue à l'analyse | Commit `5693659` (08:36 UTC) antérieur à l'exécution ; 42725 avant toute exécution | **Prouvé, non reconstituable** : une variante modifiée dans l'éditeur a été exécutée. Relancer la version corrigée avant 02 (S1) |
| `docs/sql/inspect_geonaf_schema.sql` l.27 et l.32 | `a.attidentity`, `k.contype` concaténés (42725) | Reproduit en local ; fichier versionné le 2026-11-04 (`8b1eafb`), export cité du 2026-11-03 | **Corrigé**. L'export du 2026-11-03 provient d'une version non versionnée, **non reconstituable** |
| `09_rename_legacy_schema_application.sql` | Seule la ligne de confirmation différait dans la copie de travail | `git diff` : 1 ligne, horodatage au format attendu | Restauré depuis `HEAD` (D2.9d) ; aucune autre correction |
| 08, 09 (simulation, empreinte), `legacy_structure.sql` (export D), 00 | Aucune concaténation `"char"` | `EXPLAIN` local ; exécutions rapportées sans erreur | Cohérent |
| Autres corrections éventuelles dans l'éditeur | Inconnues | Aucun historique du SQL Editor | **Non reconstituables**. Règle : n'exécuter que les fichiers versionnés, sans modification, et signaler toute erreur avant de corriger |
