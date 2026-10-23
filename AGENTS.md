# GEONAF — règles de travail

## Produit et périmètre

- GEONAF est une plateforme géospatiale Python pour observer, comprendre, prédire et simuler les phénomènes territoriaux, avec une ambition de déploiement à l’échelle africaine.
- Le MVP couvre Libreville, Akanda et Owendo. Il utilise Copernicus Sentinel-2 ; l’intégration de Sentinel-1 est prévue.
- Préserver la modularité entre ingestion, préparation, contrôle qualité, analyse géospatiale, modèles et interface. Garder la logique métier indépendante de Streamlit pour pouvoir remplacer l’interface sans la réécrire. Ne pas engager de migration prématurée.

## Changements et décisions

- Avant une modification importante, examiner les conventions et l’architecture existantes, puis expliquer l’objectif, le fonctionnement, les fichiers concernés et les risques.
- Expliciter les hypothèses importantes, les interprétations possibles et les incertitudes. Demander une clarification lorsqu’elles changent matériellement la solution.
- Privilégier un code simple, pédagogique et des changements ciblés. Expliquer les algorithmes, traitements géospatiaux, calculs et choix d’architecture ; ne pas livrer du code sans les explications nécessaires.
- Définir des critères de réussite vérifiables avant l’exécution et vérifier les résultats. Décrire les tests, leur portée, leurs limites et les risques résiduels sans exagérer leur valeur.
- Ne jamais supprimer ou écraser des données satellites, notebooks, scripts expérimentaux ou résultats sans autorisation explicite.
- Ne jamais effectuer de commit, push, déploiement, suppression ni changement majeur d’architecture sans validation explicite.

## Sécurité et confidentialité

- Ne jamais exposer ni versionner des secrets, fichiers `.env`, identifiants CDSE, données confidentielles ou chemins professionnels.
- Ne jamais placer de secret dans le code, les logs, Git ou les prompts. Ne pas afficher leur valeur pendant un diagnostic ; ne rapporter que leur présence, leur emplacement et le risque, avec masquage.
- Avant tout commit, vérifier `.gitignore`, les fichiers indexés et les risques de divulgation.

## Reproductibilité scientifique et données

- Documenter les sources, dates d’acquisition et d’ingestion, AOI, systèmes de coordonnées, résolutions, masques, paramètres et transformations utiles à la reproduction.
- Prévoir l’ingestion quotidienne des nouvelles acquisitions Sentinel-1 et Sentinel-2 via les interfaces adaptées, avec contrôles qualité.
- Distinguer date d’acquisition, date d’ingestion et ancienneté des observations. Ne jamais promettre une image de la veille si aucune acquisition exploitable n’existe.

## Observation territoriale et incertitude administrative

- GEONAF doit pouvoir observer toute zone géographique, qu’une frontière administrative officielle existe ou non.
- Séparer rigoureusement zone observée, unité administrative et statistique officiellement attribuable à une collectivité ; une valeur calculée sur une zone n’est attribuée à une collectivité que si son périmètre est celui de cette collectivité.
- Ne jamais présenter les zones d’acquisition historiques (carrés de 12 × 12 km d’Akanda, Libreville, Owendo) comme des communes ou d’autres unités administratives.
- Conserver pour chaque périmètre sa provenance, sa version, sa licence, son statut juridique et son identité géométrique ; ne jamais réutiliser un résultat ou un cache associé à une autre géométrie.
- Ne déclarer une frontière officielle qu’après vérification de l’autorité émettrice et du document justificatif ; à défaut, la qualifier de référentiel communautaire, humanitaire ou fourni par l’utilisateur.
- Ne jamais interpréter une divergence entre référentiels, ni une nouvelle version d’un périmètre, comme un changement temporel réel du territoire.
- Afficher les incertitudes et limites des périmètres avec les indicateurs publics.
- Préserver la compatibilité avec les zones personnalisées, les grilles géographiques stables et l’extension à l’ensemble du continent africain.

## Tests et validation

- Ajouter des tests pytest aux fonctions critiques. Distinguer les tests automatisés des scripts de diagnostic manuels, notamment ceux qui effectuent des appels réseau.
- Rapporter les commandes et résultats réellement obtenus. Signaler les échecs, avertissements, limites de couverture et validations non réalisées.

## Skills disponibles et choix selon la tâche

Utiliser les skills seulement quand leur objectif correspond au travail ; ne pas inventer de skill ni supposer qu’un skill absent est installé. Si un skill recommandé n’est pas disponible, le signaler et appliquer les conventions du dépôt.

- Planification : `grill-me` pour clarifier un plan ; `to-spec` pour formaliser une spécification à partir d’une décision déjà discutée.
- Architecture : `codebase-design` pour concevoir ou approfondir les interfaces et modules ; consulter aussi les ADR du dépôt lorsqu’ils existent.
- Tests Python : `ecc:python-testing` pour pytest et ses fixtures ; `tdd` lorsqu’un comportement doit être développé test d’abord.
- Diagnostic : `diagnosing-bugs` pour examiner une anomalie ; établir un cas reproductible avant de conclure.
- Revue de code : `code-review` pour vérifier un diff selon les conventions et la demande.
- Pour les échanges courants, employer le style concis de `caveman` : réponse directe et sans remplissage, tout en conservant les faits techniques. Garder des explications complètes lorsque la sécurité, la précision, les étapes ou la pédagogie le demandent.
- Appliquer `karpathy-guidelines` aux tâches de code quand elles sont pertinentes : expliciter les hypothèses, réduire la complexité, limiter le diff et vérifier des critères de réussite.

## Communication

- Répondre brièvement en français, avec les détails nécessaires pour comprendre les décisions, les résultats et leurs limites.
