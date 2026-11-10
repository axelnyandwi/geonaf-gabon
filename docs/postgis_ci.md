# Validation PostGIS isolée — D2 à D4

Le workflow `postgis-validation.yml` est indépendant de la démo scientifique Linux.
Il ne télécharge aucun paquet scientifique Akanda et ne contacte aucune base externe.
Sa préparation locale ne constitue pas une validation PostgreSQL/PostGIS : le premier
résultat réel ne sera connu qu'après publication et exécution explicitement autorisées.

## Exécution et isolation

Trois jobs Ubuntu 24.04 x64, Python 3.13.18, au plus deux simultanés, 25 minutes
maximum chacun. Un nouveau push sur la même branche annule l'exécution précédente.
Déclencheurs : push sur `db/d2a-territory-registry` et `workflow_dispatch`.
Attention : publier cette branche déclenchera donc la CI. Le déclenchement manuel
exige normalement l'enregistrement du workflow sur la branche par défaut ; aucune
modification de cette branche n'est proposée ici.

Les conteneurs sont démarrés par `scripts/ci/postgis_validation.py`, après génération
et masquage de mots de passe aléatoires. Ce choix évite un mot de passe statique dans
un bloc `services`, lancé avant les étapes du job. Docker est utilisé uniquement sur
le runner GitHub hébergé, via son socket Unix local. Le script refuse Windows, les
runners auto-hébergés, les configurations Docker externes et les paramètres `PG*`
implicites non vides. Aucun argument ne permet de fournir une URL externe.

Chaque service écoute uniquement sur `127.0.0.1`, sur un port dynamique. Le contrôle
de santé utilise `pg_isready`, avec attente bornée. L'image exacte, le nom et le label
du conteneur sont vérifiés avant connexion et avant suppression. Aucun montage du
poste ni volume persistant partagé. Le nettoyage vise uniquement ce conteneur et
ses volumes anonymes ; une interruption brutale laisse la destruction au runner
éphémère GitHub. Un échec de nettoyage fait échouer le job.

L'administrateur synthétique crée une base depuis `template0`, les trois rôles et
une marque UUID indépendante dans le commentaire de la base. Il installe l'extension
uniquement dans le schéma demandé, après vérification de son absence. Une lecture
de `pg_extension` vérifie ensuite schéma et version ; PostgreSQL doit être en version 16.
Le schéma `geonaf` reste à créer par migration 0001.

Les tests utilisent `geonaf_migrator`, propriétaire mais sans superuser ni BYPASSRLS,
et deux connexions séparées lecteur/importateur également sans superuser ni BYPASSRLS.
Le compte migrateur peut prendre ces rôles ; l'inverse n'est pas autorisé.
Le droit CREATE public est retiré à PUBLIC ; `extensions` appartient à l'administrateur.
Les fixtures conservent la destination locale, le nom exact de base, l'UUID et
`GEONAF_ALLOW_DESTRUCTIVE_TESTS=1`. Une base suffixée `_test` seule ne suffit pas.
Les connexions de tests et Alembic passent par les connecteurs sécurisés existants.
L'exception administrative du script ne peut viser que le conteneur qu'il vient de créer.
TLS distant n'est pas simulé par ce test loopback : ses contre-tests restent unitaires.

## Matrice obligatoire

La liste exacte des identifiants pytest est versionnée dans `scripts/ci/postgis-tests.json`.

| Variante | Tests requis | Portée |
| --- | ---: | --- |
| `plain` | 9 | Refus sans marque, absence de PostGIS explicite, confidentialité des erreurs ; contraintes, triggers, RLS et downgrade SQL 0002 avec types de substitution explicitement limités ; compilation PL/pgSQL de l'import SQL Editor généré (D3) et arrêt sur le premier garde-fou |
| `public` | 23 | PostGIS réel dans public ; Alembic réel 0001/0002, upgrade/downgrade/base/réapplication, rollback DDL, imports, droits/RLS, R5 bis, GiST et parité synthétique |
| `extensions` | 39 | Mêmes validations, avec PostGIS réellement installé dans extensions, et scénarios de marquage SQL Editor |

## Workflow hors ligne `offline-tests.yml` (D3, D5, Node)

Job Ubuntu 24.04 distinct, mêmes déclencheurs, `contents: read`, aucune base, aucun secret, aucun
appel Supabase. Python 3.13.18 et `requirements-ci-postgis.txt` (épingles existantes) ; Node 22.14.0
via `actions/setup-node` v6 épinglé (`249970729cb0ef3589644e2896645e5dc5ba9c38`, vérifié le
10 novembre 2026 via l'API GitHub). `scripts/ci/offline_validation.py` exécute la liste explicite de
`scripts/ci/offline-tests.json` :

- pytest `-m "not postgis"` sur les tests D5 (client HTTPS, SCRAM, cohérence SQL/fonction, `.gitignore`),
  l'intégration Streamlit D5, le générateur SQL D3 et les contrats CI ;
- `node --test` sur tous les `tests/frontend/test_*.mjs`, dont la sécurité de la fonction Edge.

Échec fermé : code non nul, échec, erreur, skip, todo ou fichier listé sans cas invalide l'exécution.
Le type TypeScript de `index.ts` (runtime Deno) n'est ni compilé ni exécuté. Les 8 cas `postgis` de
`test_edge_acquisitions_d5.py` (scripts 13, connexion SCRAM réelle) restent hors CI : ils exigent
`CREATEROLE`, que le rôle `geonaf_migrator` de la CI n'a volontairement pas.

Le job sans extension ne prétend pas valider la géométrie ou les migrations spatiales.
Les deux autres jobs exécutent notamment :

- Les migrations réelles, y compris l'échec injecté en fin de 0002 : aucune table
  Alembic ni schéma geonaf ne doit subsister, puis une réapplication saine doit réussir.
- Les imports idempotents, versions immuables et refus de contenus divergents.
- L'atomicité multi-périmètres : un indicateur sans géométrie fait annuler aussi les
  acquisitions et le journal ; après import du registre, les deux imports réussissent,
  et une répétition ne crée aucune nouvelle observation.
- La parité fichiers/PostGIS sur registres, acquisitions et indicateurs synthétiques.
- Les ACL effectives et la RLS avec logins distincts : après retrait temporaire de
  la politique lecteur, SELECT reste accordé mais ne retourne aucune ligne.
- L'ombrage malveillant d'une fonction PostGIS (R5 bis), les index GiST valides dans
  le catalogue et leur utilisation dans EXPLAIN sur des données synthétiques.
- Le refus d'une identité UUID incorrecte, d'une suppression sans autorisation et
  d'une migration dont la marque d'environnement ne correspond pas.

Les cas obligatoires sont passés individuellement à pytest, avec marqueurs stricts,
sans filtre `-m` et sans `PYTEST_ADDOPTS`. Le rapport doit contenir exactement ces cas,
une seule fois : aucun skip, xfail, cas absent, doublon ou cas inattendu n'est accepté.
Un code pytest non nul invalide aussi le résultat. Les contrôles locaux du manifeste
vérifient que chaque fonction requise existe. Ajouter un nouveau comportement critique
exige de mettre à jour ce manifeste après revue ; une découverte automatique ne le remplace pas.

## Références et dépendances

Références vérifiées le 5 novembre 2026 via API GitHub et registre Docker (manifestes
SHA-256 et configuration linux/amd64, sans télécharger les couches ni lancer Docker) :

| Élément | Référence immuable |
| --- | --- |
| checkout v5 | `fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09` |
| setup-python v6 | `ece7cb06caefa5fff74198d8649806c4678c61a1` |
| upload-artifact v4 | `ea165f8d65b6e75b540449e92b4886f43607fa02` |
| postgres:16-bookworm, PostgreSQL 16.15 | `sha256:5983db9a9c6ad225a48a46cfe2e03e6680b1826662befd0465eb05b34cb1c64e` |
| postgis/postgis:16-3.6, PostgreSQL 16.15 / PostGIS 3.6.4 | `sha256:5f97c32e0f92a0d65b253cea1b14e356718220c23ee78b25425eabe5835540d4` |

Sources : API `repos/actions/{checkout,setup-python,upload-artifact}/git/ref/tags/{v5,v6,v4}` ;
registre `registry-1.docker.io/v2/{library/postgres,postgis/postgis}/manifests/`.
Python 3.13.18 est présent pour Linux x64 Ubuntu 24.04 dans le
[manifeste officiel setup-python](https://github.com/actions/python-versions/blob/main/versions-manifest.json).
Une vérification de digest prouve le contenu sélectionné, pas l'absence de vulnérabilités.

`requirements.txt` et `requirements-db.txt` sont inchangés. Le fichier CI ajoute
pytest 9.2.0 et trustme 1.2.1 ; psycopg 3.2.3, SQLAlchemy 2.0.36 et Alembic 1.14.0
restent les versions DB existantes. Les dépendances transitives ne sont pas toutes
verrouillées par hash : le premier job Linux doit confirmer leur résolution et leur
compatibilité. Les principales versions effectivement chargées sont rapportées.
L'image Ubuntu du runner reste maintenue par GitHub, même avec l'étiquette 24.04.

Le workflow emploie la notation JSON, sous-ensemble YAML 1.2, pour permettre une
validation syntaxique locale sans installer de parseur YAML. Les règles GitHub sont
contrôlées par tests statiques ; le parseur de GitHub n'a pas encore exécuté ce fichier.

## Écart avec l'environnement Supabase documenté

La CI valide uniquement **PostgreSQL 16.15 et PostGIS 3.6.4** (images épinglées ci-dessus).
L'environnement Supabase documenté dans ce dépôt utilise d'autres versions :

| Élément | CI | Supabase documenté | Source dans le dépôt |
| --- | --- | --- | --- |
| PostgreSQL | 16.15 | 17.11 | `tests/test_supabase_scripts_execution.py`, `docs/supabase_backup_d2_9c.md` (client 17 requis) |
| PostGIS | 3.6.4 | 3.3.7, schéma `extensions` | `docs/supabase_legacy_schema_plan.md` |

La variante `extensions` reproduit le schéma d'installation, pas les versions. Une fonction
PostGIS postérieure à 3.3, ou un comportement propre à PostgreSQL 17, peut donc réussir en CI et
échouer sur Supabase. Un succès de cette CI ne vaut pas validation sur Supabase ; aucune version
de dépendance ni d'image n'est modifiée pour réduire cet écart dans ce lot.

## Tests PostGIS hors de la première CI

`pytest -m postgis` collecte 139 cas. La CI en exige 25 distincts (48 exécutions sur trois
variantes). Les **114 autres restent non validés** : ils ne sont ni exécutés ni présentés comme réussis.

| Fichier | Cas hors CI | Motif principal |
| --- | ---: | --- |
| `test_supabase_corrections_integration.py` | 37 | Scripts Supabase correctifs, hors matrice |
| `test_supabase_legacy_rename_integration.py` | 25 | Renommage du schéma historique, hors matrice |
| `test_supabase_legacy_verify_integration.py` | 22 | Vérification post-renommage, hors matrice |
| `test_supabase_scripts_execution.py` | 9 | Scripts SQL Editor ; l'un exige PostgreSQL 17 |
| `test_sql_char_concatenation.py` | 9 | Résolution de types des scripts en lecture seule |
| `test_supabase_state_diagnostic_integration.py` | 7 | Diagnostic d'état Supabase |
| `test_database_integration.py` | 3 | Base réelle (`GEONAF_DB_INTEGRATION=1`) |
| `test_db_backup_scripts.py` | 1 | pwsh et binaires PostgreSQL requis |
| `test_postgis_territory_integration.py` | 1 | `test_real_registry_import_and_parity` : données réelles |

Les étendre exigera une revue du manifeste et, pour les scripts Supabase, un serveur PostgreSQL 17.

## Confidentialité, coût et limites

Permissions `contents: read`, checkout sans identifiants persistants, aucune référence
aux secrets du dépôt, aucune étape de publication applicative. Les sorties Docker et
pytest restent privées dans RUNNER_TEMP. Seuls le JUnit reconstruit (identifiants connus,
résultat générique et durée numérique), le bilan de comptage et les versions sont
envoyés comme artefacts pendant 14 jours. Les mots de passe, URLs, traces, propriétés
JUnit et sorties capturées ne sont jamais copiés dans les artefacts. Le diagnostic
public d'un échec sera donc volontairement limité ; améliorer ce diagnostic devra
préserver cette liste de champs autorisés.

Le plafond configuré est 75 minutes-runner par exécution (3 × 25), pas une durée
mesurée. Aucun coût réel n'est encore connu ; vérifier le plan GitHub et ses quotas
avant lancement. Aucun benchmark représentatif de la production : les durées JUnit
et EXPLAIN contrôlent la régression fonctionnelle et l'éligibilité GiST, pas un SLA.

L'avertissement Rasterio `PytestUnraisableExceptionWarning` du budget réseau reste
documenté dans l'intervention précédente : exception de budget remontée par callback
natif, sans fuite démontrée. Aucun code raster, GDAL ou dépendance scientifique modifié.
Les tests de cette matrice ne valident pas Supabase, son pooler, ses certificats ou ses
politiques de plateforme. Aucun accès Supabase n'est nécessaire ni autorisé.

## Publication ultérieure, sur autorisation seulement

Vérifications locales du 5 novembre 2026, sans installation :

- 257 tests ciblés réussis, 4 avertissements (Alembic simulé, TLS, AST/R5 bis,
  stockage et 16 contrôles du nouveau workflow).
- Suite finale `pytest -m "not postgis"` : 1 686 réussis, 56 ignorés,
  139 désélectionnés, 9 010 avertissements ; aucun échec. Les 56 ignorés
  correspondent à 52 cas nécessitant des données absentes de l'environnement
  temporaire et 4 cas nécessitant les outils PostgreSQL locaux.
- Exécution via le harnais temporaire existant : cache pytest désactivé, sorties
  et données de test temporaires, réseau externe interdit, protection des écritures
  scientifiques. Inventaire des 653 fichiers scientifiques inchangé, zéro écriture bloquée.
- Collecte exacte des 6/21/21 identifiants requis ; syntaxe JSON/YAML, AST Python,
  chemins, marqueurs, références immuables et `git diff --check` vérifiés.
- Exécution directe du script CI sur le poste refusée avant création de conteneur.
- Aucun motif de secret ou chemin professionnel détecté dans les sept fichiers créés
  et le fichier de garde-fous ajusté. Les autres fichiers présents au début ont conservé
  leur empreinte. Ce contrôle de motifs ne remplace pas la revue finale des fichiers indexés.

Ces résultats locaux ne démontrent ni l'exécution réelle des migrations, ni la réussite
de la matrice Linux/PostGIS. Les 48 exécutions obligatoires sont préparées, non exécutées.

Le workflow dépend des corrections Alembic, connecteurs et fixtures actuellement non
commitées. Publier seulement les nouveaux fichiers CI ne suffit pas. Revoir et indexer
explicitement les prérequis autorisés, sans `git add .`, après contrôle de confidentialité
de l'ensemble du diff public. Ne pas inclure des résultats scientifiques ou fichiers secrets.

Commandes indicatives, non exécutées :

```powershell
git branch --show-current
git status --short
# git add -- <liste explicite des fichiers CI et prérequis approuvés>
git diff --cached --check
git diff --cached --stat
# Revue locale du contenu indexé avant autorisation finale de publication.
git commit -m "ci: validate D2-D4 on isolated PostGIS runners"
git push -u origin db/d2a-territory-registry
```

Le dernier push lance le workflow. L'autorisation de préparation locale ne couvre
ni ces commandes de publication ni le lancement de GitHub Actions.
