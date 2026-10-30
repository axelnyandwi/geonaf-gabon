# Messages d'erreur de l'interface Streamlit

Objectif : afficher des messages compréhensibles sans exposer de trace Python, de chemin local, d'URL
signée ni de secret, tout en conservant côté serveur ce qu'il faut pour diagnostiquer un incident.
Mise en place : C3 (30/10/2026), Streamlit 1.65.0.

## Deux catégories

| Catégorie | Exemples | Affichage | Journal serveur |
|---|---|---|---|
| **A. Erreur fonctionnelle attendue** | acquisition indisponible, couverture insuffisante, paire non comparable, données manquantes, paramètre invalide, budget dépassé | message métier, chemins/URL/secrets masqués par précaution | aucun |
| **B. Erreur interne inattendue** | exception Python imprévue, erreur de bibliothèque, fichier ou chemin interne, réseau, authentification, programmation | message générique + **identifiant de diagnostic** `GN-XXXXXXXXXX` ; jamais `str(exc)` | trace complète, une fois, avec le même identifiant |

Catégorie A, liste explicite (`src/components/errors.py`, `_expected_types`) : `LocalPreviewError`,
`CatalogueError`, `TransferAborted`, `GeoBoundariesError`, `DemoPackError`, `ByteBudgetExceeded`,
`IncompleteSTACSearchError`, et les `ValueError` **levées par le code GEONAF lui-même** (dernier cadre
de la trace sous `src/`), dont les messages sont rédigés pour l'utilisateur. Une `ValueError` issue
d'une bibliothèque (numpy, rasterio, json, encodage…) ou une sous-classe non listée (dont
`NetworkConfigurationError`) relève de la catégorie B.

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

## Journal serveur

Enregistreur `geonaf.ui`, niveau `ERROR` :

```
ui_internal_error incident=GN-3F9A1C2B04 context=s2_transitions.selection type=RuntimeError
Traceback (most recent call last):
  File "<projet>/src/…", line …
```

- Racines raccourcies : projet → `<projet>`, environnement Python → `<python>`, dossier personnel → `~`.
- URL : schéma, hôte et chemin conservés ; identifiants, requête et fragment retirés (`?<masqué>`).
- Secrets masqués : affectations `token=`, `password=`, `secret=`, `signature=`…, en-têtes `Bearer`/`Basic`,
  identifiants de clé AWS, et valeurs des variables d'environnement dont le nom évoque un secret.
- Doublons : une erreur déjà journalisée par un gestionnaire interne ne l'est pas une seconde fois ; la
  même défaillance répétée dans les 10 minutes (relances Streamlit) produit une seule ligne
  `ui_internal_error_repeat … count=N` au lieu d'une nouvelle trace.

Aucun service externe de suivi des erreurs n'est utilisé.

## Limites

- Une `ValueError` déclenchée par une fonction intégrée appelée directement dans `src/` (par exemple
  `int("x")`) est classée A : son message, technique mais sans chemin ni secret après masquage, peut
  s'afficher.
- Les erreurs métier (catégorie A) ne sont pas journalisées, et plusieurs sont levées avec `from None`
  (catalogue, geoBoundaries) : la cause technique d'une indisponibilité du catalogue n'est donc conservée
  nulle part. La couche réseau rend volontairement silencieux les journaux de transport.
- Le masquage repose sur des motifs ; un secret de forme inhabituelle et sans nom évocateur ne serait pas
  reconnu dans une trace serveur.
