# Problèmes connus

## `tests/test_pair_selection_cache.py` : trois tests ignorés à tort (constaté le 28/10/2026)

**Statut :** résolu le 30/10/2026 (C2) ; le test est désormais versionné.

**Symptôme initial.** Les trois cas paramétrés étaient ignorés avec le motif « Fichier SCL local
indisponible : 20250115 ».

**Cause.** Le test dépendait de données locales non versionnées : `_find_scl(date)` cherchait
`*{date}*SCL*.jp2` sous `data/raw/sentinel2` et exigeait exactement une correspondance. Sous Windows,
le SCL du 15/01/2025 existe en deux exemplaires de même SHA-256 (`00097c05b76497f3…`), l'un dans le
dossier du produit, l'autre copié à la racine de `data/raw/sentinel2` : deux correspondances, donc
saut avec un motif inexact. Sous Linux/CI, `data/` (ignoré par Git) est absent : le test aurait été
ignoré dans tous les cas. Ni le code de production ni les fixtures n'étaient en cause.

**Correction.** Le test génère ses données dans `tmp_path` avec une graine fixe, en conservant la
configuration réelle qu'il visait : SCL 120 × 120 px à 20 m en UTM 32N, AOI GeoJSON en WGS84 rognée
dans la tuile, troisième acquisition partageant le fichier SCL de la première. Les trois cas portent
sur trois géométries d'AOI (intérieure, débordant de la tuile, multipartie) au lieu des trois carrés
locaux. Assertions conservées (même clé de grille pour les trois paires, un seul calcul de poids) et
renforcées (grille rognée, trois paires évaluées, aucun échec raster). Vérification : le test échoue si
le cache est neutralisé (`weight_calls == 3`). Aucune donnée locale n'a été modifiée ; le doublon SCL
reste à trancher séparément.

**Limite.** Le test ne vérifie plus le fichier JP2 réel ni les AOI locales `data/aoi/*.geojson` ; la
lecture JP2 relève du pilote GDAL, pas de la logique de cache testée ici.

## `tests/test_downloads.py` : test de concurrence intermittent (constaté le 28/10/2026)

**Statut :** ouvert. Cause identifiée le 30/10/2026 dans le code de production ; correction en attente
de validation (hors périmètre C2).

**Test :** `test_concurrency_is_bounded_and_cancel_covers_pending_acquisitions`.

**Symptôme.** Échec isolé (`BrokenBarrierError`, journal `asset_transfer_failed type=BrokenBarrierError`
de `src/data/download_control.py`) ; réussite lors des relances.

**Cause (Windows uniquement).** Le `BrokenBarrierError` est un effet secondaire. Une sonde sans barrière
(même scénario, 2 workers) montre que, dans 8 exécutions sur 120, une acquisition échoue avant son
premier transfert :

```
DownloadQueue._download → _persist → LocalDownloadStore.write_history → atomic_json
→ os.replace(queue.json.<uuid>.tmp, queue.json)
PermissionError: [WinError 5] Accès refusé
```

L'acquisition passe à l'état `failed` (« Validation ou accès local impossible… »), l'autre fil attend
seul à la barrière et la rompt au bout de 5 s. Avec 1 worker : 0 échec sur 60. `queue.json` n'a qu'un
écrivain, sérialisé par le `RLock` de la file : le refus vient très probablement d'un handle externe
(antivirus, indexation Windows) ouvert sur le fichier juste remplacé lorsque deux remplacements se
suivent de près. Sous Linux, `os.replace` n'est pas exposé à ce refus, ce qui est cohérent avec la CI.

**Pourquoi le test n'a pas été modifié.** Allonger le délai de barrière ne corrige rien : l'acquisition a
déjà échoué. Tolérer cet échec affaiblirait l'assertion fonctionnelle. Le défaut touche aussi l'usage
réel sous Windows : un échec d'écriture du journal d'historique fait échouer un téléchargement valide.

**Corrections possibles (code de production, à valider).** Réessayer brièvement `os.replace` sur
`PermissionError` dans `atomic_json` sous Windows ; et/ou ne pas faire échouer un téléchargement pour
une écriture du journal `queue.json` qui n'est qu'un historique.

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
