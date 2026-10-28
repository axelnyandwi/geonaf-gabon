# Problèmes connus

## `tests/test_pair_selection_cache.py` : trois tests ignorés à tort (constaté le 28/10/2026)

**Statut :** ouvert, correction indépendante à faire. Le fichier de test n'est pas versionné
(travail local) ; il n'a pas été modifié.

**Symptôme.** Les trois cas paramétrés sont ignorés avec le motif « Fichier SCL local
indisponible : 20250115 ».

**Cause.** `_find_scl(date)` cherche `*{date}*SCL*.jp2` récursivement sous `data/raw/sentinel2`
et exige exactement une correspondance. Le fichier du 15/01/2025 existe en deux exemplaires :

| Chemin | SHA-256 (préfixe) |
|---|---|
| `data/raw/sentinel2/S2B_MSIL2A_20250115T093239_N0511_R136_T32NNF_20250115T120528/T32NNF_20250115T093239_SCL_20m.jp2` | `00097c05b76497f3` |
| `data/raw/sentinel2/T32NNF_20250115T093239_SCL_20m.jp2` (copie isolée à la racine) | `00097c05b76497f3` |

Deux correspondances ⇒ saut du test, avec un motif inexact : le fichier n'est pas absent, il est
ambigu. Le saut masque donc un test exécutable.

**Vérification.** Une copie temporaire du test, écartant la copie à la racine, fait passer les
trois cas (3 réussis). Aucune donnée n'a été supprimée ni déplacée.

**Correction attendue (non réalisée).**

1. Accepter plusieurs fichiers SCL correspondants lorsque leurs SHA-256 sont identiques.
2. Sélectionner une source déterministe (par exemple le fichier situé dans le dossier du produit
   SAFE, sinon le premier chemin dans l'ordre lexicographique).
3. Échouer explicitement — ne pas ignorer — si des doublons ont des SHA-256 différents, en listant
   les chemins et empreintes.
4. Réserver le saut au cas d'absence réelle, avec un motif exact.
5. Ne supprimer aucun fichier Sentinel-2 dans le cadre de cette correction.

## `tests/test_downloads.py` : test de concurrence intermittent (constaté le 28/10/2026)

**Statut :** ouvert, non corrigé (traitement non modifié).

**Test :** `test_concurrency_is_bounded_and_cancel_covers_pending_acquisitions`.

**Symptôme.** Un échec isolé pendant une suite complète (`BrokenBarrierError`, journal
`asset_transfer_failed type=BrokenBarrierError` de `src/data/download_control.py`), puis réussite
lors des deux suites complètes suivantes et de cinq exécutions isolées du fichier (63 tests).

**Cause probable.** Le test vérifie que deux téléchargements tournent réellement en parallèle : chaque
premier transfert attend l'autre sur `threading.Barrier(2)` avec un délai de 5 s. Sous la charge de la
suite complète (≈ 3 min, processus Streamlit et rasters en parallèle), le second fil peut démarrer
plus de 5 s après le premier ; la barrière est rompue et le transfert échoue. Le code de production
n'est pas en cause d'après ces observations ; aucune modification de `src/data` ni du test depuis
leur dernière version validée.

**Correction possible (non réalisée).** Délai de barrière plus long ou synchronisation par
événements, en gardant la vérification d'un parallélisme réel ; ou marquage explicite comme test
sensible au temps.

## Accessibilité des tableaux `st.dataframe` (constaté le 28/10/2026)

**Statut :** limite connue, non corrigée.

**Constat.** Les tableaux des chiffres clés et de la section Sentinel-2 affichent des décimales
françaises par un `Styler` pandas (`french_table`) : l'affichage visuel est « 25,47 », mais la grille
Streamlit expose aux technologies d'assistance (cellules `role="gridcell"`) la valeur brute pleine
précision, par exemple « 25.470556226203076 ». Avant ce formatage, la valeur exposée était déjà
anglo-saxonne (« 25.47 »), arrondie dans les données.

**Pourquoi.** Les nombres sont conservés en pleine précision pour garder un tri numérique ; la grille
(Glide Data Grid) lit la valeur des données, pas le texte formaté, pour sa couche accessible.

**Pistes (non réalisées).** Arrondir les données à la précision affichée (perte de précision interne,
tri toujours numérique) ; ou proposer, à côté de la grille, un tableau texte (`st.table`) lisible par
les lecteurs d'écran ; ou vérifier une option de la version de Streamlit utilisée exposant le texte
formaté.
