# Plan non destructif : schéma `geonaf` hérité, puis migration 0001 (D2.1)

Décision du 2026-11-04 : option A. La migration 0001 est la référence. Le schéma `geonaf` déjà
présent est renommé, jamais supprimé.

**Aucune étape n'a été exécutée.** Chaque opération qui écrit (marquée ✋) exige une validation
explicite avant exécution, projet par projet.

## Constat (export `docs/sql/inspect_geonaf_schema.sql`, 2026-11-03)

- 8 tables : `acquisitions`, `analysis_acquisitions`, `analysis_runs`, `indicator_definitions`,
  `indicator_results`, `sources`, `territories`, `territory_versions`.
- Le comptage donne 0 ligne. La RLS est activée, sans aucune politique. Il n'y a ni déclencheur, ni
  vue, ni fonction, ni marque d'environnement. Seul `postgres` a des droits. PostGIS 3.3.7 est dans
  le schéma `extensions`.
- Aucune trace de ce schéma dans le dépôt, dans aucune branche. Il ne provient pas de 0001.
- Les deux CSV sont identiques à l'octet près. Rien ne prouve qu'ils proviennent de deux projets
  distincts : l'inventaire doit être refait sur chaque projet (étape 1).
- « 0 ligne » n'est pas encore prouvé : avec la RLS activée et sans politique, un rôle qui n'est ni
  propriétaire ni `BYPASSRLS` compte 0 ligne même si la table en contient. L'étape 1 le vérifie.

## Principe

`ALTER SCHEMA geonaf RENAME TO geonaf_legacy_draft` ne modifie que le nom du schéma. Tables,
colonnes, contraintes, index, séquences, droits, politiques, commentaires et lignes sont conservés,
car PostgreSQL les référence par identifiant interne (OID). L'opération est réversible tant que 0001
n'est pas appliquée. Le schéma renommé reçoit le commentaire `geonaf-legacy: ...`, qui ne correspond
à aucune marque `geonaf-environment:*` : la couche D2.1 ne peut pas le prendre pour un environnement.

## Ordre d'exécution

`geonaf-test` d'abord, puis `geonaf-dev` seulement si tout `geonaf-test` est conforme. Chaque fichier
est collé en entier dans une **nouvelle** requête du SQL Editor, puis exécuté. On lit le résultat
avant de passer à l'étape suivante.

| # | Fichier | Écrit ? | Attendu |
|---|---|---|---|
| 1 | `08_inspect_legacy_schema.sql` | Non | Toutes les lignes `conforme = true`. Exporter le résultat en CSV, nommé d'après le projet |
| 2 | `09_rename_legacy_schema_simulation.sql` | Non (annulé) | Erreur « GEONAF SIMULATION DE RENOMMAGE REUSSIE, AUCUNE ECRITURE ». Toute autre erreur : arrêt |
| 3 ✋ | `09_rename_legacy_schema_application.sql` | Oui (renommage) | `geonaf_libere = true`, `tables_conservees = 8` |
| 4 | `00_preflight.sql` | Non | Toutes les lignes conformes ; schéma geonaf « absent (migration à appliquer) » |
| 5 ✋ | `01_roles.sql` | Oui (rôles `NOLOGIN`) | Deux rôles |
| 6 | `02_migration_0001_simulation.sql` | Non (annulé) | « GEONAF SIMULATION DE MIGRATION REUSSIE, AUCUNE ECRITURE » |
| 7 ✋ | `02_migration_0001_application.sql` | Oui | Une ligne `0001_territorial_registry` |
| 8 ✋ | `03_mark_environment_test.sql` (ou `_dev`) | Oui (commentaire) | `geonaf-environment:test` (ou `dev`) |
| 9 | `04_verify_schema.sql` | Non | Toutes les lignes conformes (10 tables) |
| 10 | `90_test_constraints.sql` (**test uniquement**) | Annulé | « GEONAF TESTS REUSSIS, TOUT EST ANNULE : 21 cas conformes » |
| 11 | Diagnostic D2.1 (`scripts/check_database_live.py`) | Non | Code 0. Nécessite un réseau où les ports 5432/6543 sont ouverts |

L'import des 97 territoires (`docs/supabase_registry.md`, section C) est une étape distincte, avec sa
propre validation.

## Critères de réussite

- Après l'étape 3 : les 8 tables héritées sont dans `geonaf_legacy_draft`, avec le même inventaire
  qu'à l'étape 1 (relancer `docs/sql/inspect_geonaf_schema.sql` en remplaçant le nom du schéma).
- Après l'étape 9 : 10 tables dans `geonaf` et 0 table manquante ; aucun droit pour `anon`,
  `authenticated`, `service_role` ni `PUBLIC`.
- Aucune ligne supprimée, aucune table supprimée.

## Dépendances susceptibles de bloquer ou de fausser le renommage

| Dépendance | Effet du renommage | Contrôle (08, puis garde de 09) |
|---|---|---|
| Rôle courant ni propriétaire du schéma ni `CREATE` sur la base (par exemple un schéma créé par `supabase_admin`) | Renommage refusé par PostgreSQL | Ligne 23 ; garde « proprietaire » |
| Comptage filtré par la RLS (rôle ni propriétaire ni `BYPASSRLS`) | Le constat « vide » serait faux | Ligne 4 ; bloquant dans 09 |
| Lignes présentes | Aucune perte, mais le schéma serait utilisé : décision à revoir | Ligne 5 ; bloquant |
| Tables différentes des 8 attendues | Ce n'est plus le schéma inventorié | Ligne 3 ; bloquant |
| Marque d'environnement présente | Ce serait un schéma 0001 : ne pas renommer | Ligne 2 ; bloquant |
| Schéma cible `geonaf_legacy_draft` déjà présent | Conflit de nom | Ligne 24 ; bloquant |
| Vues ou règles d'un autre schéma qui lisent `geonaf` | Elles suivent l'OID : elles liraient les tables héritées en silence | Ligne 13 ; bloquant |
| Clés étrangères d'un autre schéma vers `geonaf` | Elles suivent l'OID | Ligne 14 ; bloquant |
| Fonctions qui citent `geonaf` dans leur code | **Résolution par nom** : après 0001, `geonaf.territories` et `geonaf.territory_versions` désigneraient les nouvelles tables, de structure différente | Ligne 15 ; bloquant |
| `search_path` de rôle ou de base contenant `geonaf` | Même résolution par nom | Ligne 16 ; bloquant |
| Schéma exposé par la Data API (`pgrst.db_schemas`) | Cache PostgREST en erreur ; exposition du futur schéma | Ligne 17 ; bloquant |
| Publications Realtime | Elles suivent l'OID ; diffusion de tables héritées | Ligne 18 ; bloquant |
| Tâches `pg_cron` qui citent `geonaf` | Résolution par nom | Ligne 19 ; bloquant |
| Extension installée dans `geonaf` | Déplacement implicite de l'extension | Ligne 20 ; bloquant |
| Droits par défaut (`ALTER DEFAULT PRIVILEGES IN SCHEMA geonaf`) | Ils suivent l'OID et ne s'appliqueraient pas au nouveau schéma | Ligne 21 (information) |
| Sessions ouvertes, plans en cache | Renommage quasi instantané ; une requête en cours peut échouer | Ligne 25 (information) |
| Code externe hors base (Edge Functions, requêtes enregistrées dans Studio, outils BI, scripts personnels) | Résolution par nom | **Non vérifiable par SQL** : à confirmer par la personne qui a créé le schéma |

## Retour arrière

- Avant l'étape 7 : `ALTER SCHEMA geonaf_legacy_draft RENAME TO geonaf;` (✋ validation).
- Après l'étape 7, sur `geonaf-test` : `99_drop_schema_test.sql` (supprime le schéma 0001 seulement,
  ✋), puis le renommage inverse.
- Après l'étape 7, sur `geonaf-dev` : il faudrait supprimer le schéma 0001. C'est une suppression,
  donc une autorisation explicite à part, non prévue ici.

## Risques résiduels

- Les scripts 08 et 09 sont validés hors ligne : analyse syntaxique PostgreSQL 17 (`pglast`) et
  tests statiques. Les noms de colonnes des catalogues et les droits de lecture de
  `pg_db_role_setting` ou de `cron.job` sous Supabase ne sont confirmés que par l'exécution :
  l'étape 2 sur `geonaf-test` sert à cela.
- Nous ne savons pas qui a créé le schéma hérité, ni pourquoi. Le renommer peut casser un usage
  externe non déclaré.
- `geonaf_legacy_draft` reste en place. Sa suppression éventuelle est une décision séparée.
