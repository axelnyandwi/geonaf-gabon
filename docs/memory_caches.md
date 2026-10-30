# Mémoire, caches partagés et calculs simultanés (C4b)

Mise en place le 30/10/2026 à partir de l'audit C4a. Aucun algorithme scientifique n'est modifié ;
les résultats sont comparés sans tolérance avant/après (tests cités ci-dessous).

## 1. Cache partagé borné en octets (`src/services/bounded_cache.py`)

- Un seul cache par processus serveur, partagé par toutes les sessions Streamlit.
- Limite : `GEONAF_CACHE_MAX_MIB` (défaut **256 Mio**) de mémoire **distincte** retenue.
- Comptage : chaque tableau numpy est ramené au tableau qui possède sa mémoire (fin de la chaîne
  `.base`) ; un même buffer partagé par plusieurs entrées, ou par une vue et sa base, n'est compté
  qu'une fois. Objets pandas : `memory_usage(deep=True)` ; `bytes` : longueur. Les petits objets
  Python sont ignorés : le chiffre est la mémoire que l'éviction peut réellement libérer.
- Éviction : la moins récemment utilisée d'abord (ordre d'accès, déterministe). Une valeur plus
  grande que tout le budget est rendue mais non conservée.
- Concurrence : verrou global pour la table, verrou par clé pour le calcul (deux sessions
  demandant la même clé ne calculent qu'une fois) ; mesure de taille hors verrou global.
- Sûreté : les tableaux partagés sont rendus non modifiables, ainsi que les tableaux dont dérivent
  leurs vues ; toute écriture en place (`a[...] = …`, `+=`, `out=`) lève une erreur et une vue ne
  peut pas être déverrouillée. Les objets pandas ne sont pas gelés.
- Éviction et résultats : une entrée évincée est recalculée à partir de la même clé ; les calculs
  concernés sont déterministes (`tests/test_bounded_cache.py`,
  `tests/test_shared_results_streamlit.py::test_evicted_result_is_recomputed_identically`).

Utilisateurs :

| Espace | Contenu | Clé |
|---|---|---|
| `accueil.paire` | analyse de paire de l'accueil (≈ 29 Mio sur Akanda v3) | `local_preview_key` : empreinte du profil, SCL, seuil, révisions et empreintes des fichiers des deux acquisitions, chemin et contenu de l'AOI |
| `multisource.ndvi` | NDVI d'une prise de vue | `local_group_ndvi_key` (voir § 2) |
| `multisource.result` | croisement multisource | clé complète du groupe (profil effectif compris) + empreintes des couches + AOI |
| `multisource.osm`, `.radar`, `.map`, `.observation` | couches chargées, carte | mêmes arguments que les anciennes clés Streamlit |

Les sessions ne conservent plus que la clé de la paire (`st.session_state["pair"]`), plus le
résultat (auparavant ≈ 29 Mio par session).

## 2. NDVI multisource indépendant du seuil et de la couverture

`analyze_local_group` lit, dans le profil, `aoi_id`, `scl_policy`, `region`, `ecosystem` et
`seasonal_rules` ; il ne lit ni `change_threshold` ni `min_comparable_coverage_percent`.
`local_group_ndvi_key` retire ces deux paramètres de la clé. Preuve :
`tests/test_ndvi_cache_independence.py` compare, sur données synthétiques et sur le paquet v3,
tous les tableaux (valeurs, NaN, type, forme), scalaires et métadonnées de `NDVIObservation`
calculés avec deux profils différant par ces paramètres : seuls les objets profil diffèrent. Le
test vérifie aussi qu'un changement de politique SCL produit un résultat différent.

L'observation partagée est rattachée au profil effectif (`dataclasses.replace(...,
scientific_profile=profile)`, tableaux partagés) : l'avertissement de couverture minimale du
croisement suit bien le profil choisi (test d'intégration). Le croisement multisource garde sa
clé complète.

## 3. Calculs lourds simultanés (`src/services/compute_slots.py`)

- `GEONAF_MAX_HEAVY_COMPUTATIONS` (défaut **2**) places partagées par le processus ;
  attente bornée `GEONAF_HEAVY_WAIT_SECONDS` (défaut 90 s), puis message « Serveur occupé… ».
- Protégés, uniquement en cas d'absence du cache : analyse de paire, NDVI d'une prise de vue,
  croisement multisource, chargement radar et OSM, inventaire L3, sélection de paires L4, chiffres
  clés, série NDVI. Les lectures en cache, la carte multisource et les interactions légères
  n'attendent jamais.
- Réentrant par fil (un calcul lourd qui en appelle un autre garde sa place : pas
  d'interblocage), place toujours libérée (`finally`), y compris sur erreur.

## 4. Limite mémoire Linux (cgroup)

`resource_guards.cgroup_memory()` lit `/proc/self/cgroup` (v2 `0::/chemin`, v1 contrôleur
`memory`) et `/proc/self/mountinfo` (partie de la hiérarchie visible sous `/sys/fs/cgroup`),
puis chaque niveau du cgroup du processus jusqu'à la racine visible. La mémoire disponible
retenue est la plus faible des valeurs « limite − (utilisation − cache fichier inactif) » des
niveaux limités (limite d'une tranche systemd ou d'un pod comprise). Statuts : `limited`,
`unlimited`, `absent` (pas de contrôleur visible : Windows, macOS), `inaccessible` (fichiers
présents mais illisibles ou malformés). Dans ce dernier cas, la mémoire libre de l'hôte n'est pas
utilisée non plus : seuls les plafonds configurés s'appliquent.

Tests : fausses arborescences v1/v2, limite héritée, niveau le plus restrictif, conteneur avec et
sans espace de noms cgroup, statuts distincts (`tests/test_resource_guards.py`). **Non vérifié sur
un vrai Linux ni sur Streamlit Cloud** (aucun environnement Linux disponible localement).

## 5. Mesures (Windows, Python 3.10, paquet v3 Akanda)

Protocole : processus neufs, sessions `AppTest` dans un même processus, garde-fou de mémoire libre de
l'hôte neutralisé pour rendre les passages comparables ; même banc avant (commit `af46441`) et après.
RSS = mémoire du processus ; « Python » = mémoire retenue tracée par `tracemalloc` (passage séparé).

| Scénario | Avant | Après |
|---|---|---|
| 1 session (accueil) : RSS / pic | 326 / 429 Mio | 325 / 428 Mio |
| Mémoire de la session (`session_state`) | 28,9 Mio | 0,01 Mio (clé seulement) |
| 3 sessions ouvertes : RSS / Python retenu | 389 / 234,5 Mio | 332 / 177,0 Mio |
| Ouverture d'une 2ᵉ / 3ᵉ session | 2,9 / 2,0 s (analyse recalculée) | 1,6 / 0,5 s (résultat partagé) |
| 3 sessions simultanées, caches vides : pic | 570 Mio, 3 analyses de paire | 431 Mio, 1 analyse |
| L3 puis L4 (1ʳᵉ session) | 2,9 s + 2,7 s | 2,9 s + 2,6 s (inchangé) |
| Page multisource puis 1 / 3 / 5 changements de seuil : RSS | 551 / 626 / 670 Mio | 532 / 563 / 598 Mio |
| … recalculs NDVI par changement | 1 | 0 |
| … durée par changement | ≈ 5,1 s | ≈ 5,1 s (dominée par le croisement multisource) |
| 12 changements de seuil | non mesuré | cache plafonné à 250 Mio dès le 7ᵉ, RSS stable ≈ 625 Mio |
| Rotation : 6 sessions ouvertes puis fermées | 288 Mio après chaque fermeture | 318 Mio (résultat partagé conservé), stable |

## Limites

- Un tableau qui possède sa mémoire peut encore être rendu modifiable par un appel explicite
  `setflags(write=True)` (numpy l'autorise) ; les écritures accidentelles sont bloquées.
- Le budget compte la mémoire retenue par le cache, pas les pics transitoires des calculs ni la
  mémoire des caches Streamlit restants (`cached_ndvi_layer`, `cached_map_png`, inventaire,
  sélection, chiffres clés, série : quelques Mio au plus) ni le cache de la carte temporelle
  (borné séparément à 64 Mio).
- Les copies identiques `analysis.delta_ndvi` / `comparison.delta_ndvi` et
  `analysis.comparable_mask` / `comparison.comparison_mask` (≈ 6,9 Mio par analyse de paire,
  valeurs strictement égales sur la paire de référence du paquet v3) ne sont pas fusionnées :
  cela toucherait la construction des objets scientifiques (`change_analysis`).
