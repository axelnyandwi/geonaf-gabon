# Convergence des projets Supabase vers les migrations 0001 et 0002 (D2)

Ce document ne présume pas de l'état des bases. On exécute d'abord `db/supabase/11_state_diagnostic.sql`
(lecture seule) sur chaque projet, puis on suit la ligne de l'état reconnu. Rien n'est exécuté sur
Supabase sans autorisation explicite.

## 1. État connu au 2026-11-05

| Projet | Observé (source) | Hypothèse à confirmer par 11 |
|---|---|---|
| `geonaf-test` | Schéma `geonaf` renommé en `geonaf_legacy_draft`, 8 tables conservées, OID 18655, commentaire `geonaf-legacy` présent (constat déclaré le 2026-11-05). PostGIS dans `extensions`, Data API désactivée, démarrage d'instance 2026-11-02 16:49:52 UTC (exports du 2026-11-04) | État **S1**. Rôles `geonaf_*` : inconnus. Migration 0001 : non confirmée. Version de 09 utilisée (avant ou après l'audit `6f18dc2`) : inconnue, donc 10_verify l.20 doit être relancé |
| `geonaf-dev` | Schéma hérité `geonaf` présent, même empreinte que `geonaf-test` le 2026-11-04 (`32f20b4465de`). PostGIS dans `extensions`. Démarrage 16:08:10 UTC | État **S0** : aucun renommage n'a été autorisé pour dev à notre connaissance |

L'exécution anticipée de `10_security_precheck.sql` du 2026-11-04 a porté sur une variante modifiée
dans l'éditeur (section 7 de `supabase_security_d2_2.md`). Ses résultats ne valent pas contrôle S1.

## 2. États reconnus par 11 et passage à l'état suivant

| État | Signification | Prochaine étape (✋ = autorisation explicite) |
|---|---|---|
| S0 | Schéma hérité `geonaf` en place | 08, simulation 09, ✋ 09 application, puis 10_verify (verdict GO) |
| S1 | Hérité renommé et marqué, 0001 absente | 10_verify (GO) ; 00_preflight ; ✋ 01_roles si les rôles manquent ; 10_security_precheck dans sa version versionnée et contrôles S2 à S8 ; simulation 02 ; ✋ 02 application |
| S3 | 0001 appliquée, schéma non marqué | ✋ 03_mark_environment_<test\|dev>, puis 04_verify_schema |
| S4 | 0001 appliquée et marquée | 04_verify_schema ; ✋ import des territoires (05/06) ; simulation 12 ; ✋ 12 application |
| S5 | 0001 et 0002 appliquées | ✋ imports D3 (générateur SQL Editor à écrire, voir section 4) |
| V | Aucun schéma GEONAF | Aucune : vérifier le projet |
| INCOHERENT | Combinaison non prévue | Aucune : décision manuelle sur la base de l'export |

Le passage par les rôles (ancien « S2 ») n'est pas un état du schéma. La ligne `roles` de 11 l'indique,
et la prochaine étape le mentionne si les rôles manquent.

Deux points de reconnaissance :
- 0001 est reconnue à `geonaf.source_releases` et 0002 à `geonaf.acquisition_manifests`. Le schéma
  hérité possède aussi des tables `territory_versions` et `indicator_results` : ces noms ne prouvent
  rien (défaut trouvé par le test d'intégration de 11).
- 12 refuse de s'exécuter si 0001 n'est pas inscrite avec son corps revu (`719e7e84…`), si 0002 est
  déjà inscrite, ou si le schéma n'est pas marqué.

## 3. Comparer test et dev

1. Exécuter 11 en entier sur chaque projet, dans deux sessions distinctes. Exporter les CSV hors dépôt
   et hors OneDrive.
2. `python scripts/compare_state_diagnostics.py <test.csv> <dev.csv>` compare uniquement les lignes
   `comparable = true`. Les dates, OID, estimations de lignes et marques d'environnement sont ignorés.
   Le code de retour est 0 si les lignes comparables sont identiques.
3. Tant que les deux projets ne sont pas au même état, les écarts attendus sont ceux de l'état
   (schémas, tables, migrations). Toute autre différence (extensions, déclencheurs d'événement, droits
   API, dépendances, empreinte de structure à état égal) est à expliquer avant de continuer.

## 4. Ce qui reste à produire pour Supabase

- **Import des observations (D3) par le SQL Editor.** Les imports Python
  (`scripts/import_observations_postgis.py`) exigent une connexion directe, fermée depuis le poste. Il
  faut un générateur de lots SQL Editor sur le modèle de `supabase_sql.import_batches` :
  1. lignes vérifiées dans des tables temporaires ;
  2. refus des divergences ;
  3. simulation terminée par une erreur volontaire.

  Ce générateur n'est pas écrit : le volume réel est faible (3 manifestes, aucun résultat
  d'indicateur enregistré à ce jour), et il ne servira qu'à l'état S5.
- **Alignement Alembic.** Sur Supabase, l'historique est `geonaf.schema_migrations`. `alembic stamp`
  ne sera possible qu'avec une connexion directe.

## 5. Opérations Supabase restant à autoriser (aucune n'a été exécutée)

1. `11_state_diagnostic.sql` sur `geonaf-test` et `geonaf-dev` (lecture seule ; à autoriser comme
   tout accès).
2. `geonaf-test` (S1) : 10_verify, 00_preflight, ✋ 01_roles, 10_security_precheck + S2 à S8,
   simulation 02, ✋ 02, ✋ 03_mark_environment_test, 04, 90 (test seulement), ✋ import des
   territoires, simulation 12, ✋ 12.
3. `geonaf-dev` (S0 supposé) : toute la séquence 08 → 09 → … après succès complet sur `geonaf-test`,
   avec une autorisation propre à chaque étape ✋.
4. Mode PostGIS de l'application en préproduction : secrets Streamlit (`GEONAF_DB_BACKEND`,
   `GEONAF_DATABASE_URL` avec le rôle `geonaf_reader` à activer en `LOGIN`, `GEONAF_DB_ENV`). Cela
   suppose une décision sur l'activation de `LOGIN` (section 5 de `supabase_security_d2_2.md`).
