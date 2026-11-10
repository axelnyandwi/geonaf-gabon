# D2.9c - Sauvegarde du schéma hérité `geonaf` avant renommage

**Rien n'a été exécuté sur Supabase.** Les scripts de `scripts/db_backup/` sont préparés et testés sur un
PostgreSQL local jetable (16.2). La préparation n'est pas une sauvegarde.

## 1. Ce qui est sauvegardé, et ce qui ne l'est pas

| | Sauvegarde du schéma hérité (ce document) | Sauvegarde complète du projet Supabase |
|---|---|---|
| Contenu | Schéma `geonaf` : 8 tables, colonnes, contraintes, index, séquences et leur valeur, RLS, politiques, commentaires, droits, lignes | Toutes les bases et schémas, rôles, extensions, Auth, Storage (fichiers), Edge Functions, réglages API, secrets |
| Outil | `pg_dump --schema=geonaf`, ou export de structure par le SQL Editor | Sauvegardes quotidiennes ou PITR (offres payantes), sinon une combinaison d'outils ; aucune n'est disponible sur l'offre gratuite |
| Usage | Preuve de l'état initial ; reconstruction du schéma dans une base isolée | Retour du projet entier à une date |

Le renommage ne touche que le nom et le commentaire du schéma `geonaf`. **Son retour arrière normal est
`09_rename_legacy_schema_rollback.sql`**, qui conserve l'OID 18655 : une restauration recréerait les
objets avec de nouveaux OID. La sauvegarde sert de dernier recours et de preuve.

## 2. Audit des prérequis (2026-11-04)

| Élément | Constat | Conséquence |
|---|---|---|
| `pg_dump`, `pg_restore`, `psql` | Absents du PATH et de `C:\Program Files\PostgreSQL`. Seuls les outils 16.2 du serveur de test jetable sont présents | `pg_dump` doit être de version majeure ≥ à celle du serveur : **un client 17 est requis** pour Supabase 17.11 |
| Binaires PostgreSQL 17.11 (EDB) | Téléchargés dans le dossier temporaire de la session, exécution **refusée** par la politique de l'agent | Installation d'un client 17 à décider par vous (installateur officiel EDB ou `winget install PostgreSQL.PostgreSQL.17`) |
| Réseau | Ports 5432 et 6543 fermés depuis ce poste, seul HTTPS passe (`docs/supabase_registry.md`, `docs/database_connection.md`) | **Aucun `pg_dump` possible depuis ce poste.** Ne pas contourner le pare-feu : utiliser un réseau ou un poste autorisé |
| Connexion directe `db.<ref>.supabase.co:5432` | IPv6 seulement sur l'offre gratuite | À éviter |
| Pooler mode session `aws-0-<région>.pooler.supabase.com:5432`, utilisateur `postgres.<ref>` | IPv4 ; compatible avec `pg_dump` | **Voie recommandée** |
| Pooler mode transaction (6543) | Pas de session stable : inadapté à `pg_dump` | Refusé par le script |
| Droits | `postgres` possède le schéma et les tables (08 l.6, l.22). `pg_dump` lit avec `row_security = off` : accepté pour le propriétaire sans FORCE RLS (08 l.4). Avec FORCE RLS, `pg_dump` s'arrêterait en erreur, sans rien écrire | Aucun droit supplémentaire. Ne jamais réinitialiser le mot de passe de la base pour l'occasion : d'autres clients en dépendent |
| TLS | `sslmode=require` chiffre sans vérifier le serveur ; `verify-full` avec le certificat racine Supabase (tableau de bord, *Database settings*, *SSL configuration*) | Fournir `-SslRootCert` |
| PowerShell | 7.6.4 | Conforme |

## 3. Stratégie

| Approche | Contenu | Limites | Faisable aujourd'hui |
|---|---|---|---|
| A. `pg_dump -Fc --schema=geonaf` | Tout le schéma, données comprises ; archive vérifiable (`pg_restore --list`) et restaurable sélectivement | Ni rôles (les droits les citent), ni extensions, ni déclencheurs d'événement, ni publications ; OID et propriétaires non conservés à la restauration | **Non** (client 17 et port 5432) |
| B. Export SQL du schéma | Texte lisible et comparable (`pg_restore --schema-only` à partir de l'archive A, sans nouvelle connexion) | Pas de données ; mêmes limites que A | Avec A |
| C. Sauvegarde logique plus large (`pg_dump` de la base, `supabase db dump`) | Autres schémas applicatifs | Schémas gérés par Supabase (`auth`, `storage`, `realtime`, `extensions`, `graphql`, `vault`...) détenus par `supabase_admin` : erreurs de droits ou restauration impossible ; rôles sans mots de passe ; ni fichiers Storage, ni configuration du projet. Disproportionné pour un renommage | Non (mêmes contraintes que A) |
| D. Métadonnées et empreintes par le SQL Editor (HTTPS) | 08, `09_legacy_schema_fingerprint.sql` (OID, commentaire, SHA-256), `scripts/db_backup/legacy_structure.sql` (structure détaillée, sans OID), requête B1 | Pas une sauvegarde restaurable : un enregistrement vérifiable. Avec 0 ligne dans les 8 tables (08 l.5 et garde de 09), il décrit tout le contenu | **Oui** |

**Recommandation pour l'offre gratuite :**

1. **Toujours D**, juste avant le renommage : il est faisable, en lecture seule, et complète le
   retour arrière (empreinte attendue après rollback).
2. **A (avec B dérivé), si le renommage ne doit pas avoir lieu sans archive restaurable** : depuis un
   poste et un réseau autorisés, avec un client 17, par les scripts ci-dessous.

Les tables sont vides et le renommage est réversible en conservant les OID. C'est à vous de décider si D
suffit (voir le verdict en fin de document).

## 4. Scripts (`scripts/db_backup/`)

| Fichier | Rôle | Écrit ? |
|---|---|---|
| `Test-GeonafBackupPrerequisites.ps1` | Outils et versions, dossier de sortie, variables `PG*`. Réseau seulement avec `-TestNetwork` (ouverture TCP sans authentification, **contacte Supabase : autorisation requise**) | Non |
| `Invoke-GeonafSchemaBackup.ps1` | Plan à sec par défaut. Avec `-Execute` : version et instance, `pg_dump`, structure, empreinte 09, comptages, sommaire, SQL du schéma, SHA-256, manifeste, vérification | Seulement dans un **nouveau** dossier privé |
| `Test-GeonafBackupArchive.ps1` | Vérification hors ligne : SHA-256, lisibilité, 8 tables et leurs données, aucun autre schéma, sommaire = manifeste | Non |
| `Restore-GeonafBackupIsolated.ps1` | Plan à sec par défaut. Avec `-Execute` : base locale **nouvelle** `geonaf_restore_*`, `pg_restore` sans propriétaires ni droits, comparaison de structure et des lignes | Une nouvelle base locale et un rapport |
| `legacy_structure.sql` | Structure sans OID ni propriétaires, empreinte `structure_sha256` (tri `COLLATE "C"`, comparable entre Supabase et un serveur local) | Non (lecture seule imposée) |

**Protections, toutes couvertes par `tests/test_db_backup_scripts.py` :**

- Aucun secret : le mot de passe est saisi masqué (`Read-Host -AsSecureString`), placé dans
  `PGPASSWORD` pour la durée de l'exécution, puis supprimé dans un bloc `finally`. Hôte, port et
  utilisateur passent par les variables `PG*` du processus, jamais en ligne de commande ni dans un
  journal. Le manifeste masque l'hôte (`<region>.pooler.supabase.com`) et la référence (`postgres.<ref>`).
- Le script refuse de démarrer si une variable `PG*` est déjà définie dans la session.
- Hôtes : sauvegarde depuis le pooler ou l'hôte direct Supabase, port 5432 ; restauration
  **uniquement en local**, dans une base `geonaf_restore_<suffixe>` qui ne doit pas exister.
- Rien n'est jamais supprimé : ni `--clean`, ni `DROP`, ni effacement de fichiers sauvegardés.
- Dossier de sortie hors du dépôt, hors OneDrive (variables et nom `OneDrive - …`), jamais réseau,
  toujours nouveau, droits limités à l'utilisateur courant (`icacls /inheritance:r`).
- Lecture seule : `pg_dump` travaille dans une transaction `READ ONLY` ; chaque requête `psql`
  s'exécute en une transaction (`-1`) ouverte par `transaction_read_only = on`.
- Chaque commande native est suivie du contrôle de son code de retour ; un échec arrête tout.

## 5. Procédure de sauvegarde A (après autorisation)

1. Sur un poste et un réseau autorisés, avec un client PostgreSQL 17 : lancer
   `Test-GeonafBackupPrerequisites.ps1 -PgBin <bin>`. Tout doit être conforme.
2. ✋ Après autorisation seulement : `-TestNetwork -PgHost aws-0-<région>.pooler.supabase.com`.
3. Plan à sec : `Invoke-GeonafSchemaBackup.ps1 -ProjectLabel geonaf-test -PgHost <pooler> -PgUser postgres.<ref> -PgBin <bin> -SslRootCert <ca>`.
   Vérifier l'hôte masqué, le dossier et `sslmode verify-full`.
4. ✋ Même commande avec `-Execute`. Saisir le mot de passe quand il est demandé.
5. Attendu : `VERIFICATION CONFORME`, puis `SAUVEGARDE TERMINEE ET VERIFIEE`. Dans le manifeste :
   `empreinte_09.oid_geonaf = 18655` ;
   `empreinte_09.sha256 = 0084862c64986b5917ccfb3670253b5bd8c5562f3ec761c6ccea36cae615d379` ;
   `demarrage_instance_utc = 2026-11-02 16:49:52.952389` ; 8 tables ; toutes les lignes à 0.
   Toute différence : ne pas renommer.
6. Consigner dans la fiche (hors dépôt) le chemin et le SHA-256 de l'archive.

## 6. Vérification

- Immédiatement : `Test-GeonafBackupArchive.ps1 -BackupDir <dossier> -PgBin <bin>` (aussi lancé par la
  sauvegarde).
- Avant tout usage ultérieur : relancer la même commande ; une modification d'un seul octet est
  détectée (test).

## 7. Restauration isolée

1. Serveur PostgreSQL **local**, de version majeure ≥ 17 (même majeure de préférence), jamais
   `geonaf-test` ni `geonaf-dev`.
2. Plan à sec : `Restore-GeonafBackupIsolated.ps1 -BackupDir <dossier> -TargetDatabase geonaf_restore_<AAAAMMJJ> -TargetPort <port> -PgBin <bin>`.
3. ✋ `-Execute`. Attendu : `structure : identique`, `lignes : identiques`,
   `RESTAURATION ISOLEE CONFORME`.
4. Après examen, supprimer la base locale à la main. Le script ne supprime jamais rien.

**Écarts attendus entre la copie locale et Supabase :**

- OID différents : l'empreinte 09 ne peut pas correspondre ; c'est `structure_sha256` qui doit
  correspondre.
- Propriétaires et droits non restaurés (`--no-owner --no-acl`) : les rôles Supabase (`anon`,
  `authenticated`, `service_role`, `supabase_admin`) n'existent pas en local. Ils restent dans le
  sommaire de l'archive et dans la section `information` de la structure.
- Politiques RLS visant un rôle Supabase : la restauration échoue tant que ce rôle n'existe pas en local.
- Types d'une extension (PostGIS dans le schéma `extensions`) : le script refuse ; installer
  l'extension dans un schéma `extensions` local puis adapter la procédure.
- Collation et locale de la base (Supabase `en_US.UTF-8`, local souvent `C`) : sans effet sur
  l'empreinte de structure (tri `COLLATE "C"`) ; peut changer l'ordre des index textuels.
- Non couverts par aucune restauration de schéma : déclencheurs d'événement, publications, `pg_cron`,
  paramètres de rôle et de base, réglages de la Data API.

## 8. Checklist avant renommage

1. ☐ Fiche I2 : nom et référence du projet, heure.
2. ☐ Plan affiché par Database > Backups, consigné (constat : offre gratuite, aucune sauvegarde automatique).
3. ☐ 08 conforme. Ligne `-3` = `2026-11-02 16:49:52.952389`. Exporter.
4. ☐ B1 : 0 ligne.
5. ☐ Empreinte 09 : OID 18655, SHA-256 `0084862c…`. Exporter.
6. ☐ **Sauvegarde D** : `scripts/db_backup/legacy_structure.sql` dans le SQL Editor (tout coller, ne
   rien sélectionner, Run). Exporter en CSV ; noter `structure_sha256` et le nombre d'éléments.
7. ☐ Sauvegarde A, si exigée : sections 5 et 6 conformes, OID et empreinte 09 identiques aux étapes 3 et 5.
8. ☐ Exports rangés hors dépôt, hors OneDrive, avec des droits limités à l'utilisateur.
9. ☐ Autorisation explicite du renommage. La sauvegarde n'en tient pas lieu.

## 9. Récupération en cas d'incident

| Situation | Action |
|---|---|
| Application 09 refusée ou en erreur | Rien n'a été écrit (requête annulée en bloc). Relancer 08 et l'empreinte, comparer, s'arrêter |
| Renommage fait, retour souhaité, 0001 non appliquée | Contrôle R1 (D2.9c), puis ✋ `09_rename_legacy_schema_rollback.sql`. Attendu : OID 18655, commentaire `null`, empreinte `0084862c…`, `structure_sha256` identique à l'étape 6 |
| Rollback refusé (marque modifiée, tables différentes, `geonaf` déjà recréé) | Ne rien forcer. Aucune donnée n'est perdue : les tables sont sous `geonaf_legacy_draft`. Comparer avec la sauvegarde D (et A) et décider |
| Schéma hérité endommagé (cas non prévu par les scripts) | Restauration isolée de A en local d'abord, comparaison, puis décision séparée. **Jamais de restauration directe sur `geonaf-test` ou `geonaf-dev` sans une autorisation dédiée** : elle recréerait des objets avec de nouveaux OID et pourrait entrer en conflit avec l'existant |
| Perte au-delà du schéma `geonaf` | Hors de portée de cette sauvegarde : l'offre gratuite n'a pas de sauvegarde du projet |

## 10. Tests et limites

- `tests/test_db_backup_scripts.py` :
  - contrôles statiques sans PowerShell ;
  - refus réels avec `pwsh`, sans connexion ;
  - un test de bout en bout sur un serveur **local** (`GEONAF_TEST_DATABASE_URL`, base `*_test`,
    `GEONAF_BACKUP_PGBIN`) : sauvegarde, vérification, restauration dans une nouvelle base, refus d'une
    base existante, détection d'une archive modifiée.
- Validé sur PostgreSQL 16.2 seulement. `legacy_structure.sql` n'utilise que des catalogues stables
  (`pg_sequence` depuis 10, `pg_get_triggerdef`, `pg_policy`), mais n'a encore été exécuté ni sur
  Supabase ni en 17.11.
- Non vérifiés : le comportement réel du pooler (Supavisor) avec `pg_dump`, l'affichage du résultat de
  `legacy_structure.sql` dans le SQL Editor.
