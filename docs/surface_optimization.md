# Optimisation des surfaces — 21 octobre 2026

> **Note du 26/10/2026** : les valeurs Sentinel-2 de ce document (empreinte `b0356911…`, couvertures, pixels comparables) ont été obtenues avant la correction du découpage AOI avec perte. Valeurs corrigées : [lossless_aoi_clip.md](lossless_aoi_clip.md).

## Résultat

Sur Akanda, la médiane du calcul des surfaces passe de **17,884 s à 1,831 s** sans poids préexistants (**×9,77**). Avec les poids en cache, elle est de **24,0 ms**. L'analyse scientifique complète passe de **22,801 s à 6,783 s** (**×3,36**), ou **4,861 s** avec les poids déjà disponibles.

Les poids Akanda sont identiques bit à bit à l'oracle. Écarts maximaux mesurés : **0 m² par cellule**, **0 m² pour les agrégats**, **0 point de pourcentage de couverture**. Aucun ajustement de la référence ni arrondi supplémentaire.

```text
b0356911623b4af85939f9a5bb62f0f01cdff9ffd1dae875ca84b77523cb0e41
```

Les NDVI, delta, masques des deux dates, classes, comptes, statistiques et grille sont comparés. L'empreinte couvre les tableaux et indicateurs sélectionnés par le benchmark existant ; elle ne constitue pas une validation indépendante des observations.

## Algorithme et conservation géométrique

Le calcul démarre sur des blocs de 256 × 256 cellules, soit 65 536 cellules, sous le plafond de 100 000. Les six coefficients affines sont conservés. Les sommets partagés sont transformés ensemble, une fois par bloc, vers EPSG:6933.

L'enveloppe d'un bloc utilise tous ses sommets projetés, y compris les sommets intérieurs. Chaque quadrilatère à arêtes droites utilisé par l'ancien algorithme appartient à cette enveloppe. Ce raisonnement ne suppose pas une projection linéaire, ni que les quatre coins extérieurs suffisent à décrire l'emprise projetée.

Une enveloppe entièrement couverte par l'AOI autorise le calcul vectorisé des aires de quadrilatères, sans polygone individuel. Une enveloppe disjointe donne des poids nuls. Les blocs ambigus sont subdivisés ; les petits groupes restants utilisent les mêmes prédicats et intersections Shapely que l'oracle. Les trous et concavités empêchent donc une classification intérieure abusive.

La formule d'aire conserve l'origine locale en x et l'ordre des opérations de l'aire de l'anneau utilisée par l'oracle GEOS. Aucun remplacement par une aire nominale constante, densification des arêtes, changement de masque SCL, de rasterisation `all_touched`, de CRS ou de géoréférencement.

Les réductions gardent les anciennes bandes et l'ordre des sommes pour les largeurs jusqu'à 100 000 cellules. Au-delà, les bandes sont aussi segmentées en colonnes pour borner la mémoire : l'ordre des additions peut alors différer, avec comparaison sous les tolérances explicites. Ce cas extrême est testé sur 100 001 colonnes ; il n'affecte pas Akanda.

## Cache, invalidation et mémoire

Le cache LRU interne est limité à **4 entrées et 67 108 864 octets (64 Mio)** de matrices `float64`. L'éviction précède l'allocation de la nouvelle matrice. Les entrées trop grandes sont exclues ; l'agrégation passe alors par un flux de blocs bornés.

La clé contient les dimensions, l'affine complète, la résolution, le WKT du CRS source, le CRS cible, l'empreinte SHA-256 du WKB de la géométrie AOI projetée et unionnée, la version de l'algorithme et les paramètres de découpage. Elle ne dépend pas uniquement d'un chemin ou d'une date de fichier. Les masques et classes ne sont pas mis en cache : une même matrice de poids sert à plusieurs observations.

Les matrices internes sont en lecture seule. `prepare_pixel_area_weights` retourne une copie indépendante que l'appelant peut modifier. Le verrou protège les recherches, promotions LRU, constructions, évictions et invalidations. Une construction échouée n'est pas publiée ; une invalidation concurrente ne laisse pas publier de résultat ancien après sa fin.

Le verrou sérialise les constructions et peut retarder un accès chaud pendant une autre construction. Le cache reste local au processus. Ces choix privilégient une borne mémoire et une concurrence simples ; ils ne constituent pas un cache distribué.

Les 64 Mio concernent le stockage du cache, pas toute la mémoire du processus : les rasters scientifiques, les géométries, les copies appartenant aux appelants et les temporaires de tuile s'y ajoutent. La fonction publique qui demande explicitement une matrice complète doit toujours allouer sa sortie, même si elle dépasse le plafond du cache. Les autres caches préexistants du dépôt ne sont pas modifiés.

## Protocole et mesures

Données locales Sentinel-2 des 15 et 25 janvier 2025, carré d'acquisition Akanda 12 × 12 km. Grille 1201 × 1201, EPSG:32632, résolution 10 m, politique `usable_land`, seuil 0,1. Cette AOI n'est pas une limite administrative.

Python 3.10.11 ; NumPy 2.2.6 ; Rasterio 1.4.4 ; Shapely 2.1.2 ; PyProj 3.7.1 ; GeoPandas 1.1.4. Machine du diagnostic : Ryzen 7 PRO 7730U, 8 cœurs/16 threads, environ 15,31 Gio de RAM.

Trois répétitions par mode, ordre alterné, sans autre benchmark Python en parallèle. Imports, préparation du cache, empreintes et assertions sont exclus du chronométrage des surfaces. Le cache froid signifie cache des poids vidé ; le cache disque du système n'est pas purgé. L'analyse complète refait notamment la vérification d'intégrité et les lectures, même en mode poids chauds.

| Opération / mode | Répétition 1 | Répétition 2 | Répétition 3 | Médiane |
|---|---:|---:|---:|---:|
| Surfaces, oracle | 17,756 s | 17,884 s | 18,054 s | 17,884 s |
| Surfaces, froid | 1,831 s | 1,856 s | 1,820 s | 1,831 s |
| Surfaces, chaud | 24,003 ms | 24,511 ms | 21,451 ms | 24,003 ms |
| Surfaces, cache saturé | 2,022 s | 2,166 s | 1,889 s | 2,022 s |
| Analyse complète, oracle | 22,801 s | 22,031 s | 24,722 s | 22,801 s |
| Analyse complète, froid | 6,628 s | 6,817 s | 6,783 s | 6,783 s |
| Analyse complète, poids chauds | 4,861 s | 4,851 s | 4,926 s | 4,861 s |

Le scénario saturé commence chaque fois avec **4 entrées de 16 Mio**, soit exactement **64 Mio**. La demande Akanda évince l'entrée la moins récemment utilisée. Après calcul : **4 entrées, 61 870 856 octets**, sans dépassement du plafond de stockage.

AOI synthétiques distinctes sur une grille 256 × 256, EPSG:6933, résolution 10 m ; trois répétitions par mode :

| AOI | Oracle, médiane | Froid, médiane | Chaud, médiane |
|---|---:|---:|---:|
| Rectangle à bords partiels | 623,5 ms | 210,4 ms | 3,1 ms |
| Polygone avec trou | 869,6 ms | 154,7 ms | 3,0 ms |
| Polygone concave | 968,1 ms | 272,9 ms | 3,1 ms |
| Multipolygone | 978,7 ms | 189,8 ms | 3,4 ms |

Aucune régression de performance dans ces cas. Les objectifs proposés ×3 sur les surfaces Akanda et moins de 200 ms avec poids chauds sont atteints. Ils ne garantissent pas le même facteur sur toute géométrie complexe.

La mémoire résidente du processus est échantillonnée toutes les 10 ms lorsque `psutil` est disponible. Pics observés pendant les appels de surfaces : oracle **393,3 Mio**, froid **255,5 Mio**, chaud **254,5 Mio**, cache saturé **307,5 Mio**. Ce sont des pics du processus, avec des états d'allocateur et de cache différents ; un pic entre échantillons peut être manqué. Ne pas les interpréter comme des bornes absolues ou comme un gain mémoire garanti.

Les valeurs brutes et les métadonnées publiques figurent dans [le rapport JSON](benchmarks/surface_weights_2026-10-21.json).

## Vérifications

Tolérances par poids et agrégats : `rtol=1e-12`, `atol=1e-8 m²`. Couverture : au plus `1e-9` point de pourcentage. À 144 km², la composante relative représente environ 0,000144 m² ; aucune approximation géométrique n'est introduite pour respecter ces tolérances.

Les 35 nouveaux cas paramétrés couvrent l'oracle figé, les frontières partielles, trous, concavités, multipolygones, contacts sans surface, bandes fines, jointures de tuiles, rotation/cisaillement, plusieurs CRS et une projection non linéaire contrôlée. Ils couvrent aussi la borne des blocs, les grilles très larges, les copies indépendantes, l'invalidation par chaque entrée géométrique, l'éviction par octets et nombre, plusieurs masques, les constructions concurrentes, leurs échecs et l'invalidation pendant une construction.

Une modification de contenu AOI avec taille et `mtime` conservées provoque bien un nouveau calcul.

Commandes exécutées :

```powershell
.venv/Scripts/python.exe -B -m pytest -q -rs -p no:cacheprovider
node --test tests/frontend/test_ndvi_map.mjs
.venv/Scripts/python.exe -B scripts/benchmark_surface_weights.py --output docs/benchmarks/surface_weights_2026-10-21.json
.venv/Scripts/python.exe -B scripts/benchmark_map_layer.py
git diff --check
```

Résultats finaux : **280 tests Python réussis, 3 ignorés, 1100 avertissements** ; **2 tests JavaScript réussis** ; `git diff --check` réussi. Les trois tests ignorés sont ceux du cache de sélection de paires : leur recherche ne trouve pas de fichier SCL local correspondant au 15 janvier 2025. Les données utilisées par le benchmark Akanda sont, elles, présentes et vérifiées.

Les avertissements Python sont les dépréciations Affine/Rasterio déjà présentes. Git signale aussi une future conversion LF/CRLF d'un fichier préexistant hors périmètre. Aucun nouveau correctif de ces avertissements.

Avant implémentation, les 245 tests existants exécutés ont réussi ; le premier nouveau test de cache échouait comme attendu, faute d'API de cache. Une AOI trop courte dans un nouveau test d'intérieur a ensuite été corrigée. Aucun échec ne subsiste dans la suite finale.

Pour reproduire sans créer de rapport :

```powershell
.venv/Scripts/python.exe -B scripts/benchmark_surface_weights.py
```

L'option `--output` exige un nom de fichier neuf et refuse d'écraser un résultat existant. `psutil` reste facultatif : sans lui, les timings sont disponibles et les champs RSS sont nuls. Aucun téléchargement de données ni modification des sources satellites.

## Fichiers de cette intervention

- `src/geo/change_analysis.py` : seul module de production modifié.
- `tests/area_weights_oracle.py` : ancien algorithme figé, réservé aux tests et benchmarks.
- `tests/test_area_optimization.py` : tests supplémentaires.
- `scripts/benchmark_surface_weights.py` : benchmark manuel reproductible.
- `docs/benchmarks/surface_weights_2026-10-21.json` : mesures brutes publiques.
- `docs/surface_optimization.md` : présent rapport.

Les modifications préexistantes du dépôt sont conservées. Streamlit, MapLibre, l'ingestion et la calibration ne sont pas modifiés par cette intervention. Aucun commit créé.
