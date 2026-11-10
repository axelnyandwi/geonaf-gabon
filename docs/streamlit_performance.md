# Streamlit V0.1 : formatage et performance

> **Note du 26/10/2026** : les valeurs Sentinel-2 de ce document (empreinte `b0356911…`, couvertures, pixels comparables) ont été obtenues avant la correction du découpage AOI avec perte. Valeurs corrigées : [lossless_aoi_clip.md](lossless_aoi_clip.md).

Mesures locales du 18 octobre 2026 sur les acquisitions réelles des 15 et
25 janvier 2025 et le carré d’acquisition Akanda. Aucun accès réseau aux données.
Ces vérifications sont informatiques et ne constituent pas une validation scientifique.

## Protocole et résultats

Commande depuis la racine :

```powershell
.venv\Scripts\python.exe -B scripts/benchmark_streamlit.py
```

Le script manuel utilise `Streamlit AppTest` pour mesurer un premier passage
avec cache applicatif vide, une soumission de la même paire puis une simple
réexécution. Il instrumente les services réellement appelés et les lectures
raster. Les phases ci-dessous sont exclusives : les sous-phases instrumentées
sont soustraites de leur parent. Les lectures JP2 regroupent rouge, NIR et SCL ;
le calcul BOA/NDVI exclut les lectures, et la phase SCL couvre l’alignement et
l’application de sa politique. Les statistiques incluent le calcul des surfaces.

| Mesure (s) | Avant, premier | Après, premier | Avant, même paire | Après, même paire |
|---|---:|---:|---:|---:|
| Total AppTest | 37,526 | 29,832 | 27,028 | 0,203 |
| Vérification des sources et métadonnées | 1,307 | 1,187 | 1,090 | 0 |
| Lecture JP2 | 0,692 | 0,571 | 0,647 | 0 |
| Autres lectures raster | 0,539 | 0,461 | 0,464 | 0 |
| Calcul BOA / NDVI | 0,193 | 0,175 | 0,169 | 0 |
| Masques SCL | 0,257 | 0,239 | 0,270 | 0 |
| Harmonisation temporelle, hors SCL | 0,068 | 0,056 | 0,054 | 0 |
| Statistiques et surfaces temporelles | 23,948 | 19,679 | 19,027 | 0 |
| Construction des figures | 0,262 | 0,236 | 0,222 | 0 |
| Encodage PNG séparé | inclus dans pyplot | 1,809 | inclus dans pyplot | 0 |
| Rendu / sérialisation Streamlit côté Python | 3,457 | 0,361 | 2,482 | 0,132 |

La simple réexécution passe de **2,864 s à 0,200 s**. Les étapes non exécutées
sur cache valide sont indiquées par zéro, ce ne sont pas des mesures arrondies
de calculs extrêmement rapides. Le total inclut aussi les imports, le découpage
AOI, les écritures temporaires, les contrôles de clés et autres opérations non
isolées ; il n’est donc pas la somme des seules lignes instrumentées.

Une observation par scénario, sans purge du cache disque du système. Ces temps
ne constituent pas un benchmark statistique. L’algorithme de surfaces n’a pas
été modifié : son écart entre les premiers passages relève de la variabilité
d’exécution, et ne démontre pas une accélération algorithmique. Le coût du
premier calcul demeure. Les durées du navigateur, de son affichage effectif
et du transport HTTP ne sont **pas mesurées** par AppTest.

## Optimisations et invalidation

- Le résultat scientifique reste propre à la session (`st.session_state`).
  Une nouvelle soumission identique n’entraîne plus de recalcul. Aucun objet
  scientifique mutable n’est partagé entre utilisateurs par `st.cache_resource`.
- La clé couvre les deux acquisitions (produits et dates), les manifestes,
  B04/B08, SCL, XML BOA, AOI, politique SCL, seuil et version de calcul.
  Les gros fichiers utilisent identité, taille, mtime et ctime en nanosecondes ;
  les manifestes et l’AOI sont également hachés par contenu. Les erreurs ou
  fichiers manquants bloquent l’affichage plutôt que de servir un ancien résultat.
- À chaque cache miss, la vérification SHA-256 complète des actifs précède le
  calcul existant. Le contrôle léger des révisions à chaque interaction ne
  remplace pas cette vérification. Il n’est pas une garantie contre une modification
  malveillante qui préserverait tous les attributs de fichiers contrôlés.
- `st.cache_data` conserve au maximum quatre images PNG, uniquement en mémoire,
  avec la même clé d’entrée et une version de rendu. Le résultat métier est exclu
  du hachage Streamlit car ses mappings immuables ne se prêtent pas à la
  sérialisation ; l’invalidation repose explicitement sur la clé d’entrée.
- Les images passent à 1 500 × 500 pixels, suffisants pour cet aperçu. Aucun
  sous-échantillonnage des rasters scientifiques ni arrondi des statistiques.
  Les fonctions de formatage retournent seulement des chaînes d’affichage :
  séparateur de milliers français, NDVI à quatre décimales, surfaces et
  pourcentages à deux décimales. Les exports ne passent pas par ces fonctions.

En cas de modification des algorithmes ou des options de rendu, incrémenter
respectivement les versions `akanda-science-v1` et `png-v1`. Une nouvelle
session force le recalcul scientifique ; vider le cache Streamlit vide les PNG.

## Identité scientifique

L’empreinte combinée est identique avant/après et sur les trois scénarios :

```text
b0356911623b4af85939f9a5bb62f0f01cdff9ffd1dae875ca84b77523cb0e41
```

Elle couvre les deux NDVI, le delta, les masques/classes finaux, les statistiques,
les surfaces et la couverture comparable. Pour imposer cette référence au script :

```powershell
.venv\Scripts\python.exe -B scripts/benchmark_streamlit.py --expected-digest 995a1dbfe14c3ec6b1585411ffa59e180f23a151765dad5dff4d60fb354e073b
```

Ce contrôle suppose les mêmes fichiers sources et versions de dépendances.
La calibration BOA, SCL 4/5, les seuils et les services scientifiques restent inchangés.
