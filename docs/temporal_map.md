# Contrat de comparaison temporelle — étapes 1 et 2

> **Note du 26/10/2026** : les valeurs Sentinel-2 de ce document (empreinte `b0356911…`, couvertures, pixels comparables) ont été obtenues avant la correction du découpage AOI avec perte. Valeurs corrigées : [lossless_aoi_clip.md](lossless_aoi_clip.md).

La préparation est indépendante de Streamlit et de JavaScript. Elle consomme
un `TemporalVegetationChangeResult` existant, sans lecture de bandes, calcul
NDVI, harmonisation, classification ou statistique supplémentaire.

## Interface Python

```python
from src.services.temporal_map import TemporalMapCache

cache = TemporalMapCache()  # conserver cette instance au niveau appelant
payload = cache.prepare(result, scientific_key=revision, max_size=1536)
```

`revision` doit identifier les versions scientifiques, les acquisitions,
manifestes, révisions des bandes/SCL/XML, calibration et paramètres. La clé
`local_preview_key` existante convient au cas local. Le cache ajoute la version
du rendu, la taille réellement transmise au préparateur, le contenu de l'AOI,
la grille, la politique SCL, le seuil et les identités des produits. Modifier
directement les tableaux nécessite une nouvelle révision ; ils ne sont pas
rehachés à chaque accès au cache. La vérification d'intégrité des acquisitions
reste celle du pipeline existant.

La fonction sans cache est `prepare_temporal_map(result, max_size=1536)`.
Une taille entière de 1 à 2048 est admise, sous réserve du budget mémoire.
La taille maximale ne provoque pas de suréchantillonnage volontaire.

## Charge utile `geonaf.temporal-map/v1`

| Champ | Contrat |
|---|---|
| `schema_version` | Littéral `geonaf.temporal-map/v1` |
| `result_id` | SHA-256 déterministe du contenu public, hors ce champ ; identité d'affichage, pas empreinte scientifique native |
| `grid` | `width`, `height`, `crs=EPSG:3857`, six coefficients affine, quatre coins WGS84 NW/NE/SE/SW |
| `layers` | Ordre stable : `ndvi_initial`, `ndvi_final`, `delta`, `classes` |
| Chaque couche | `id`, `kind`, `mask_id`, PNG RGBA base64, `legend`, `range`, références `provenance_keys` |
| `masks` | PNG monocanal uint8 pour `initial`, `final`, `comparable` |
| `aoi`, `footprint` | GeoJSON sans attributs privés, coordonnées WGS84 |
| `mask_policy` | SCL 4/5, catégories 0 extérieur, 1 indisponible, 2 valide, règle AOI `all_touched` |
| `classification` | Seuil effectif, delta final moins initial, règles exactes pour les codes 0 à 3 |
| `provenance` | Produits, dates UTC, coefficients BOA par date, grille native, dénominateur de couverture, compte des exclusions hors plage |
| `display` | Taille demandée, version de rendu, plus proche voisin, `quantitative=false`, estimation mémoire des tableaux supplémentaires |

Toutes les images ont les mêmes dimensions et coins. Les catégories de masque
et valeurs sont reprojetées au plus proche voisin. Les observations par date
conservent leurs masques propres ; delta et classes utilisent le masque final
de l'analyse, incluant ses exclusions hors plage. La palette NDVI est fixe
[-1, 1], celle du delta [-2, 2]. Les couleurs sont des représentations
d'affichage ; les valeurs hors de la plage visuelle saturent aux extrémités.
Les classes proviennent exclusivement du tableau scientifique existant.
Les valeurs exactement égales à ±seuil appartiennent à la classe stable.

Un pixel partiellement couvert suit `all_touched` sur la carte. Ses surfaces
restent celles des intersections exactes calculées en amont. Ni les PNG ni
leurs catégories ne permettent de recalculer des surfaces scientifiques.
Les indicateurs restent dans `result.analysis` ; aucune copie de ceux-ci
n'est nécessaire dans cette charge utile.

Seuls les champs de provenance explicitement autorisés sont transmis.
Les chemins, XML, manifestes bruts, attributs AOI et dictionnaires arbitraires
de qualité ne sont pas sérialisés. Aucun fichier satellite n'est écrit.

## Mémoire, concurrence et limites

Le cache LRU conserve au maximum 4 entrées et 64 Mio de JSON sérialisé et de
clés binaires. Les petits surcoûts des objets Python ne sont pas inclus dans
ce compteur. Les réponses sont désérialisées à chaque appel : une modification
par un appelant ne corrompt pas le cache. Aucun résultat scientifique n'y est
retenu. Les préparations d'une même instance sont sérialisées sous verrou ;
la saturation évince l'entrée la moins récemment utilisée. Une réponse trop
grande pour le cache est retournée sans être conservée. Un échec n'est pas
mis en cache. `clear()` et `info()` sont protégés par le même verrou.

Un garde-fou de 256 Mio porte sur une estimation conservatrice des tableaux
supplémentaires (`8*N_native + 96*N_display`), pas sur la mémoire totale du
processus. La charge utile sérialisée est limitée à 32 Mio. L'AOI contrôlée
par le cache est limitée à 8 Mio. Les décodeurs PNG, JSON, GDAL et les réponses
détenues par les appelants ont leurs propres allocations. Ce mécanisme ne
constitue donc pas une garantie de RSS globale. Plusieurs instances multiplient
les budgets : la future intégration devra conserver une instance par périmètre
de cache approprié.

Les emprises hors domaine Mercator et celles détectées comme traversant
l'antiméridien sont refusées explicitement. Les grandes AOI dépassant le
garde-fou nécessiteront une préparation par fenêtres/tuiles. Aucun système
de tuiles ou serveur web n'est introduit ici.

## Reproduction

```powershell
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests/test_temporal_map.py tests/test_map_layer.py tests/test_local_preview.py tests/test_temporal.py tests/test_temporal_acquisition_integration.py tests/test_change_analysis.py tests/test_cloud_mask.py tests/test_ndvi_canonical.py tests/test_geotiff_export.py tests/test_quality.py
.venv/Scripts/python.exe -B scripts/benchmark_temporal_map.py
```

Le benchmark utilise les manifestes strictement vérifiés des acquisitions
Akanda des 15 et 25 janvier 2025 et l'AOI locale d'acquisition. Il vérifie
l'empreinte scientifique historique et l'identité des masques sources
supplémentaires. Trois répétitions comparent l'ancienne préparation d'une seule
couche, les quatre nouvelles couches à cache froid, à cache chaud et après
éviction. Les clés différentes du test de saturation représentent des révisions
de la même paire, pas des observations indépendantes ou d'autres AOI.
Les résultats sont conservés dans `benchmarks/temporal_map_2026-10-21.json`.

Le cache disque système n'est pas purgé. Les mesures `tracemalloc` sont faites
dans une passe séparée et ne représentent ni la RSS totale ni le GPU.
La charge de quatre couches n'est pas directement équivalente à celle d'une
seule couche. Les tests synthétiques couvrent notamment trous, cellules
partielles, changement d'AOI, masques propres et comparables, bornes du seuil,
orientation, UTM, confidentialité, éviction en octets et concurrence.

### Résultats Windows du 21 octobre 2026

Trois répétitions sans tests concurrents, Python 3.10.11 ; les versions des
dépendances et les trois observations sont dans le JSON associé.

| Préparation | Médiane |
|---|---:|
| Ancienne couche unique | 245,394 ms |
| Quatre couches, cache vide | 895,083 ms |
| Quatre couches, cache chaud | 7,720 ms |
| Quatre couches, après éviction | 885,563 ms |

L'analyse scientifique initiale a pris 3,136 s lors de cette exécution. Les
images mesurent 1197 × 1205 pixels. Le JSON des quatre couches et trois masques
fait 6 255 591 octets. Quatre entrées occupent 25 021 108 octets comptabilisés
par le cache ; son plafond est 67 108 864 octets. Le pic `tracemalloc` de la
préparation séparée est de 50 825 875 octets, inférieur à l'estimation prudente
des tableaux de 150 008 168 octets ; ces mesures n'ont pas la même portée.

L'empreinte scientifique reste strictement :

```text
b0356911623b4af85939f9a5bb62f0f01cdff9ffd1dae875ca84b77523cb0e41
```

Tests : 148 ciblés réussis ; suite complète 383 réussis, 3 ignorés et 1 150
avertissements Affine/Rasterio. Les trois tests ignorés recherchent des fichiers
SCL locaux via leur convention de nommage, indépendamment des acquisitions
résolues par manifestes dans le benchmark. Après renforcement des assertions
d'identité des indicateurs et couleurs, les 25 tests du nouveau module ont été
relancés et réussissent (50 avertissements provenant de Rasterio).

Les assertions scientifiques sont exactes, sans tolérance, puisque les tableaux
et indicateurs ne doivent pas être modifiés. Le test de conversion des coins
utilise une tolérance absolue de 1e-12 degré et relative nulle pour le calcul
flottant double précision ; cette tolérance ne porte pas sur les statistiques.

## Périmètre arrêté

Aucun raccordement à Streamlit ou modification du rendu MapLibre n'est fait.
L'ancien adaptateur `cached_ndvi_layer` reste inchangé ; sa taille actuellement
codée dans une clé ne sera raccordée explicitement qu'à l'étape d'intégration.
La nouvelle interface transmet déjà `max_size` sans ambiguïté et les tests le
vérifient. Synchronisation, navigateur et performances GPU restent à valider
dans les étapes suivantes. Aucune nouvelle dépendance n'est requise.
