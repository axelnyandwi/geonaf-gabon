# Messages d'erreur de l'interface Streamlit

Objectif : afficher des messages compréhensibles sans exposer de trace Python, de chemin local, d'URL
signée ni de secret, tout en conservant côté serveur ce qu'il faut pour diagnostiquer un incident.
Mise en place : C3 (30/10/2026), complément C3 (même date), Streamlit 1.65.0.

## Deux catégories, décidées uniquement par type d'exception

| Catégorie | Exemples | Affichage | Journal serveur (`geonaf.ui`) |
|---|---|---|---|
| **A. Erreur fonctionnelle attendue** | acquisition indisponible, couverture insuffisante, paire non comparable, données manquantes, paramètre invalide, budget dépassé | message métier, chemins/URL/secrets masqués par précaution | aucun, sauf si l'erreur a une cause technique (voir ci-dessous) |
| **B. Erreur interne inattendue** | toute `ValueError` générique, exception Python imprévue, erreur de bibliothèque, fichier ou chemin, réseau, authentification, programmation | message générique + **identifiant de diagnostic** `GN-XXXXXXXXXX` ; jamais `str(exc)` | enregistrement structuré `ERROR`, une fois, même identifiant |

Catégorie A, liste explicite (`src/components/errors.py`, `_expected_types`) : `LocalPreviewError`,
`CatalogueError`, `TransferAborted`, `GeoBoundariesError`, `DemoPackError`, `ByteBudgetExceeded`,
`IncompleteSTACSearchError`, `ZoneImportRefused`. Aucune règle d'origine : une `ValueError` générique est
interne même si elle est levée par le code GEONAF. Seules ces classes portent des messages rédigés pour
l'utilisateur.

**Erreur métier avec cause technique.** Une erreur de catégorie A levée pendant le traitement d'une
autre (`raise … from error`, ou même `from None` : `__context__` est conservé) garde son message métier,
suivi d'un identifiant ; la chaîne des causes est journalisée (`WARNING`, catégorie
`functional_with_cause`). Exemple : « Recherche Copernicus indisponible… Identifiant de diagnostic : GN-… ».

**Validations à texte contrôlé par l'interface.** Lorsque les widgets bornent déjà les valeurs, le
composant affiche un texte fixe et l'identifiant, sans le message de l'exception :
« Configuration refusée : seuil, couverture minimale ou masque SCL hors des valeurs autorisées. »,
« Zone d'étude refusée : géométrie illisible, vide ou non surfacique. », « Import refusé : fichier illisible. ».

Certains messages génériques existants (carte indisponible, chiffres clés indisponibles…) sont conservés
et complétés par l'identifiant de diagnostic.

## Configuration Streamlit (`.streamlit/config.toml`)

- `client.showErrorDetails = "none"` : une exception non interceptée n'affiche dans le navigateur qu'un
  message générique de Streamlit, sans type, message ni trace ; ceux-ci restent dans la console du serveur.
- `client.showErrorLinks = false` : pas de lien de recherche externe envoyant le texte de l'erreur.

Débogage local uniquement : `STREAMLIT_CLIENT_SHOW_ERROR_DETAILS=full streamlit run app.py`.

Chaque page (`app.py`, `pages/*.py`) et le fragment de suivi des téléchargements sont entourés de
`page_guard` : une exception imprévue y produit le message générique avec identifiant. `st.stop()` et
`st.rerun()` ne sont pas interceptés (exceptions de contrôle dérivées de `BaseException`).

## Journal serveur structuré

Une ligne `ui_error` suivie d'un objet JSON :

```json
{"incident": "GN-3F9A1C2B04", "context": "s2_transitions.selection", "category": "internal",
 "type": "builtins.RuntimeError", "where": "<projet>/src/indicators/s2_pairs.py:212 s2_pair_selection",
 "frames": ["<projet>/src/components/s2_transitions.py:240 _compute", "…"],
 "chain": [{"type": "builtins.PermissionError", "errno": 13, "where": "…", "message_masked": "…"}],
 "message_masked": "…"}
```

- Le diagnostic repose d'abord sur des champs structurés : types, `errno`/`winerror`/`status_code`,
  emplacements (`<projet>/…`, `<python>/…`, sinon `<autre>/nom_du_fichier`, jamais un chemin privé).
- Les messages d'exception, principal vecteur de fuite, ne figurent qu'en champ secondaire
  `message_masked`, tronqué à 300 caractères : URL réduites à schéma/hôte/chemin (`?<masqué>`), secrets
  masqués (affectations `token=`, `password=`, `signature=`…, en-têtes `Bearer`/`Basic`, identifiants de clé
  AWS, valeurs des variables d'environnement au nom évocateur) et chemins locaux masqués.
- Pas de trace textuelle complète (lignes de source et messages intermédiaires non journalisés).
- Doublons : une erreur déjà journalisée par un gestionnaire interne ne l'est pas une seconde fois ; la
  même défaillance répétée dans les 10 minutes (relances Streamlit) produit une ligne
  `ui_error_repeat {"incident": …, "repeat": N}` sans pile ni message.

Aucun service externe de suivi des erreurs n'est utilisé. Une exception non interceptée par GEONAF reste
journalisée par Streamlit lui-même (trace complète, console du serveur).

## Limites

- Une erreur de catégorie A sans cause technique n'est pas journalisée (comportement voulu).
- `message_masked` repose sur des motifs ; les champs structurés restent exploitables si un message est
  entièrement masqué.
- Une exception non interceptée qui échappe à `page_guard` (code exécuté avant le garde, comme
  `st.page_link`) est journalisée par Streamlit sans masquage, dans la console du serveur uniquement.
