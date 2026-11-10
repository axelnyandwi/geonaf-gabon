# Validation ciblée du marquage 03 après 0001

## Contrôles et origine de la référence

`03_mark_environment_test.sql` reste une seule instruction atomique pour les contrôles et
le marquage. Les tables et les deux vues sont verrouillées pendant les vérifications.
Un refus précède le seul `COMMENT ON SCHEMA` ; il ne marque pas le schéma.

La référence est issue d’une application neuve de `02_migration_0001_application.sql` sur
PostgreSQL 16.4 / PostGIS 3.6.2 réel, dans `extensions`, avec `search_path` vide lors de
l’extraction. Le corps 0001 est inchangé ; son SHA-256 vérifié est
`719e7e847f28404255491c0c00923f130cbd35151cd63488ed1ba3391c0bf971`.

Le script réutilise les fonctions catalogues déjà employées par les diagnostics 09 et 11 :

- Comparaison dans les deux sens des 65 contraintes : table, nom et définition complète
  rendue par `pg_get_constraintdef(..., false)`. Cela inclut les expressions CHECK, cibles
  et actions des clés étrangères, colonnes et ordre des clés, différabilité et distinction
  des valeurs NULL des contraintes uniques. Une contrainte non validée est refusée.
- Comparaison des deux définitions complètes de vues avec `pg_get_viewdef(..., false)`,
  en plus de leur présence et de `security_invoker=true`.
- Conservation des contrôles existants : inventaires, colonnes, RLS, index, fonctions,
  déclencheurs, données initiales, historique unique 0001 avec empreinte et canal attendus.

Les expressions ne sont pas normalisées ou simplifiées : une différence de rendu catalogue
entre versions peut provoquer un refus conservateur. Il faut alors examiner la différence ;
ne pas retirer le contrôle pour permettre un marquage.

Ce contrôle ne prétend pas prouver une équivalence intégrale de tous les catalogues : il ne
compare pas exhaustivement les propriétaires, ACL, politiques RLS, paramètres physiques,
états des séquences, commentaires, options des vues autres que `security_invoker`, règles
additionnelles et dépendances externes à un manifeste de référence.
Les précontrôles de sécurité et 04 restent nécessaires. Le contenu de l’historique ne prouve
pas cryptographiquement que le corps annoncé a réellement été exécuté par un administrateur.

## Identité de la cible et récupération

Les structures et l’historique ne prouvent pas l’identité du projet Supabase. Pour une
exécution réelle, I2 reste nécessaire : relever dans le tableau de bord le nom, la référence
et l’heure UTC, dans la fiche privée, juste avant l’écriture. Dans la copie d’exécution de 03,
seuls les deux champs documentés sont à renseigner : confirmation I2 et démarrage d’instance
issu d’un diagnostic 11 frais. Ne jamais mettre la référence réelle dans un fichier versionné.
Le démarrage confirme la continuité ; une confirmation saisie manuellement n’est pas une
identification automatique ou une attestation cryptographique du projet.

Sans ces confirmations, ou avec un démarrage différent, 03 refuse. Les tests locaux utilisent
l’identité indépendante de la base isolée déjà prévue par le dépôt (autorisation, UUID et
commentaire de base), puis remplissent uniquement ces deux champs. Ils n’attestent aucun I2
Supabase et ne suppriment aucun contrôle du script.

Après réussite de 02 et refus de 03, arrêter les écritures, conserver l’erreur, refaire I2 et
les diagnostics en lecture seule. Ne pas relancer 02 automatiquement. 99 reste bloqué tant
qu’une marque de test légitime n’existe pas ; ne jamais commenter le schéma à la main pour
le débloquer. Toute suppression exige son autorisation propre. Exécuter les scripts entiers,
depuis un éditeur dont l’atomicité a été vérifiée par 00, sans administration concurrente.

## Tests et workflow existant

`tests/test_supabase_mark_postgis_integration.py` applique réellement le fichier 02 entier
sur une base locale isolée, puis exécute le fichier 03 entier, avec un rôle non superutilisateur
pour le parcours PostGIS. Les refus comparent avant/après l’empreinte catalogue 09 existante,
le contenu des tables et les définitions des vues.

Les 18 scénarios couvrent : parcours positif et répétition ; S0 malgré les confirmations ;
contraintes absentes, CHECK modifié à nom et nombre constants, clé étrangère modifiée,
ordre de clé primaire, sémantique NULL d’unicité, contrainte non validée ; vues absentes,
filtrées, triées différemment ou sans sécurité invoker ; historique absent, vide, falsifié ou
avec une migration supplémentaire ; confirmation absente, démarrage périmé et identité
indépendante locale incorrecte.

Le workflow `.github/workflows/postgis-validation.yml` est réutilisé sans modification.
Son manifeste `scripts/ci/postgis-tests.json` impose les 18 scénarios sur la variante
`extensions` ; S0 et identité locale sont aussi obligatoires sur `plain` et `public`.
Le rapport existant échoue si un scénario obligatoire est absent, ignoré ou en échec.
La variante PostGIS du workflow utilise PostgreSQL 16 / PostGIS 3.6.4 avec les images déjà
épinglées. Aucun changement de dépendance scientifique n’est requis.

Commandes locales ciblées, après configuration des capacités d’isolation existantes :

```text
# Base réelle avec PostGIS dans extensions
python -m pytest tests/test_supabase_mark_postgis_integration.py tests/test_supabase_mark_test.py tests/test_supabase_sql.py tests/test_sql_char_concatenation.py tests/test_postgis_ci_workflow.py -q --tb=short -rs

# Base distincte, sans PostGIS ni rôles GEONAF préexistants
python -m pytest tests/test_supabase_mark_test.py tests/test_supabase_corrections_integration.py tests/test_supabase_mark_postgis_integration.py::test_s0_cannot_become_test_even_with_operator_confirmations tests/test_supabase_mark_postgis_integration.py::test_independent_local_database_identity_is_required -q --tb=short -rs
```

Ne pas lancer les contre-tests qui exigent l’absence de PostGIS sur la base PostGIS.
Les résultats locaux et GitHub doivent être rapportés séparément. La validation locale
PostgreSQL 16.4 / PostGIS 3.6.2 ne constitue pas une validation PostgreSQL 17 / PostGIS 3.3.7.
Aucun accès Supabase ni lancement GitHub Actions n’est nécessaire à cette préparation.

## Résultats locaux du 06/11/2026

- PostgreSQL 16.4 / PostGIS 3.6.2 dans un cluster temporaire, connexion de migration non
  superutilisateur : **404 réussis, 1 ignoré** (registre du paquet de démonstration absent).
  Les 18 nouveaux scénarios PostGIS sont tous réussis, sans simulation de PostGIS.
- PostgreSQL 17.11 dans un cluster temporaire distinct, sans PostGIS : **57 réussis**.
- Régression reproduite avant correction : un CHECK modifié sous le même nom et à nombre
  constant était accepté ; le contre-test complet le refuse après correction.
- Une sélection mixte initiale avait donné quatre échecs liés à l’environnement des tests
  exigeant l’absence de PostGIS/rôles. Leur exécution séparée, ci-dessus, est réussie.
- Les trois jobs GitHub restent à exécuter après autorisation de publication/lancement.
  Aucun résultat local ne vaut attestation de leur exécution ni de celle sur Supabase.
