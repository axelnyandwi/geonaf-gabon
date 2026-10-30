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

**Statut :** corrigé le 30/10/2026 (C2b) dans `atomic_json` (`src/data/download_store.py`).

**Test :** `test_concurrency_is_bounded_and_cancel_covers_pending_acquisitions`.

**Symptôme.** Échec isolé (`BrokenBarrierError`, journal `asset_transfer_failed type=BrokenBarrierError`
de `src/data/download_control.py`) ; réussite lors des relances.

**Cause (Windows uniquement).** Le `BrokenBarrierError` est un effet secondaire. Une acquisition
échouait avant son premier transfert :

```
DownloadQueue._download → _persist → LocalDownloadStore.write_history → atomic_json
→ os.replace(queue.json.<uuid>.tmp, queue.json)
PermissionError: [WinError 5] Accès refusé
```

L'autre fil attendait alors seul à la barrière et la rompait au bout de 5 s. `queue.json` n'a qu'un
écrivain par file, sérialisé par son `RLock` ; le refus vient d'un handle externe (antivirus, indexation,
lecteur) ouvert sur le fichier juste remplacé, ou d'une autre file écrivant le même journal (deux
sessions Streamlit sur le même répertoire de données). Sous Linux, `os.replace` n'y est pas exposé.

**Rôle de `queue.json`.** Historique seulement : relu au démarrage pour afficher « réestimer avant
reprise » ; aucun transfert n'est repris à partir de lui. La cohérence des téléchargements repose sur
le staging, les manifestes validés et la publication atomique.

**Correction.** Sous Windows, `os.replace` est retenté sur `PermissionError` jusqu'à 5 fois (attente
totale bornée ≈ 0,43 s), avec un avertissement `atomic_json_replace_retried retries=N` sans chemin.
Après épuisement, l'erreur d'origine est levée (pas de masquage d'un refus durable). En cas d'échec,
le fichier temporaire est supprimé et la destination reste la dernière version valide. Sous POSIX,
aucun retry. Le comportement de la file est inchangé : un échec définitif d'écriture fait toujours
échouer l'acquisition.

**Mesures (Windows, 30/10/2026).**

| Scénario | Avant | Après |
|---|---|---|
| Scénario du test sans barrière, 2 workers (acquisitions en échec) | 8 / 120 exécutions | 0 / 120 |
| Test d'origine avec barrière, exécutions isolées | intermittent | 40 / 40 réussis |
| 6 écrivains + 1 lecteur (pause 5 ms), écritures en échec | 256 / 900 (≈ 28 %) | 0 / 1 800 |
| Lecteurs en boucle continue sans pause | — | échecs maintenus (refus durable signalé, voulu) |

**Limite.** Un handle tenu plus de ≈ 0,4 s fait toujours échouer l'écriture, donc l'acquisition. Rendre
l'écriture de l'historique non bloquante pour le téléchargement reste une option non retenue.

## Suite complète Windows en 82 min (constaté le 30/10/2026)

**Statut :** expliqué ; cause environnementale, pas de défaut de code.

**Constat.** Suite du 30/10 : 945 réussis, 1 échec (`test_pack_v3_provenance_and_limits`, délai
`AppTest` de 180 s dépassé), en 4 916 s.

**Cause.** Le poste est entré en veille moderne à 09:53:14 et en est sorti à 11:05:47 (journal Système,
Kernel-Power 506/507), deux secondes après le début du test en échec (09:53:12). Le délai d'`AppTest`
est mesuré en temps réel, veille comprise. Les 82 min se décomposent en ≈ 72 min de veille et
≈ 10 min d'exécution.

**Relance.** 951 réussis, 0 échec, 0 ignoré, en 5 min 08 s (puis 5 min 14 s), charge CPU 10–69 %.
Temps cumulés : 267 s en appel, 35 s en préparation. Fichiers les plus coûteux :
`test_s2_transitions_streamlit.py` (52 s, `AppTest`), `test_demo_pack_regression.py` (43 s, dont 39 s
pour la comparaison volontaire tuiles complètes / sous-ensembles), `test_s2_pairs.py` (23 s),
`test_s2_vegetation_streamlit.py` (20 s). Ces tests utilisent les données locales complètes (paquet v3,
tuiles), absentes en CI où ils sont ignorés. Aucune connexion Internet ; aucune contention entre fils
mesurée hors `atomic_json`.

**Précaution.** Empêcher la mise en veille pendant une suite complète.

## WorldCover : recherche d'identifiants AWS inutile à chaque lecture de tuile (constaté le 30/10/2026)

**Statut :** corrigé le 30/10/2026 (C2c), `src/data/remote_window.py` et `src/data/worldcover.py`.

**Constat.** Chaque lecture de fenêtre WorldCover ouvrait 2 connexions vers `169.254.169.254` (service de
métadonnées d'instance cloud), ≈ 1 s de délai chacune, sur un poste hors AWS.

**Cause.** Les octets passent par l'`opener` de `RangeFile` (HTTP Range borné, client GEONAF) ; GDAL
n'accède pas au réseau. Mais `rasterio.open` choisit une session d'après le nom du jeu de données
(`Session.cls_from_path`, rasterio 1.4.4) : l'URL `https://esa-worldcover.s3.eu-central-1.amazonaws.com/…`
contient `amazonaws.com`, d'où une `AWSSession` dont la chaîne d'identifiants boto3 interroge le service
de métadonnées. Sentinel-1 n'était pas concerné : son nom est une clé S3 sans schéma (session neutre),
et ses accès authentifiés passent par le client S3 de GEONAF, jamais par rasterio.

**Correction.** Paramètre explicite `HttpRangeSource(..., anonymous=True)`, passé uniquement par
WorldCover (bucket public ESA). Pour une telle source, `RangeFile` ouvre le raster sous un nom neutre et
unique (`anonymous-range/<uuid>.tif`) : rasterio le classe comme local et n'ouvre aucune session cloud.
Par défaut (`anonymous=False`, `S3RangeSource`), le nom reste le libellé d'origine : rien n'est déduit
du nom de domaine. Aucune option GDAL ni variable d'environnement n'est modifiée.

**Option écartée.** `rasterio.Env(AWS_NO_SIGN_REQUEST="YES")` évite aussi la session AWS, mais rasterio
pose cette option pour tout le processus lorsque la lecture a lieu dans le fil principal (pytest, scripts)
et seulement pour le fil courant ailleurs (Streamlit, workers) : isolation non garantie.

**Mesures (30/10/2026).**

| Lecture d'une fenêtre, magasin vierge | Avant | Après |
|---|---|---|
| Hors ligne (tuile synthétique, 5 lectures) | 2,06 s, 2 connexions IMDS | 0,026 s, 0 |
| Réseau réel (Akanda ≈ 1 km, 2 lectures) | 3,9–4,1 s, 2 connexions IMDS | 1,9–2,1 s, 0 |
| Pixels, métadonnées, octets, requêtes | référence | identiques |

**Limites.** Le script manuel `scripts/check_worldcover_akanda_live.py` a son propre `RangeFile` et
garde l'ancien comportement. Les lectures réelles dépendent du réseau du poste au moment de la mesure.

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
