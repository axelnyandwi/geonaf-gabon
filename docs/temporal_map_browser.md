# Module MapLibre temporel autonome — étape 3

## Interface et périmètre

```javascript
import { mountTemporalMap } from "./temporal.js";
const view = mountTemporalMap(container, payload, maplibregl, {
  onError: () => { /* l'appelant peut choisir le repli existant */ },
});
// Au démontage :
view.dispose();
```

Charger les CSS MapLibre locales existantes et `temporal.css`. La charge utile
doit satisfaire `geonaf.temporal-map/v1`. Le module valide notamment version,
images PNG data:, dimensions, coins, échelles NDVI identiques, palettes, seuil
et provenance. Les quatre couches partagent les dimensions et coordonnées du
contrat. Les images scientifiques ne sont ni recalculées ni reclassées.

Les trois vues de bureau sont NDVI initial, NDVI final et changement. La
troisième bascule entre classes existantes et delta continu par `updateImage`.
Les couleurs des classes et leurs bornes viennent du contrat. Les légendes
explicitent que chaque date conserve son observation propre et que le
changement nécessite une observation comparable aux deux dates. Les contours
AOI et emprise raster, les dates UTC et la provenance publique sont conservés.
Les coordonnées du pointeur sont géographiques, sans interrogation quantitative
des PNG. La provenance est affichée en texte, avec liste de champs autorisés ;
les attributs sources du GeoJSON sont écartés.

La caméra comprend centre, zoom, orientation et inclinaison. Le verrou local
de propagation empêche la réentrance synchrone ; la comparaison des caméras
à 1e-9 près évite une propagation supplémentaire d'événements différés déjà
synchronisés. Cette tolérance concerne uniquement la caméra d'affichage.
Il n'existe aucun pont Python ou Streamlit ; seul `onError` notifie une erreur
générique. Les erreurs brutes du moteur ne sont pas transmises.

Sous 761 pixels, une seule carte est créée. Le sélecteur mobile remplace cette
instance et garde sa caméra. Le franchissement du seuil responsive reconstruit
les vues nécessaires en conservant la caméra. Les anciens écouteurs, sources,
images/textures et canevas sont libérés par le retrait des écouteurs et
`map.remove()`. `dispose()` est idempotent, déconnecte le ResizeObserver et
le MediaQueryList, retire les écouteurs DOM et relâche la charge utile.
Une erreur de construction partielle ou une perte de contexte libère les
cartes créées et notifie l'appelant. Celui-ci doit appeler `dispose()` quand
il retire le module, même après erreur.

Les autres fichiers MapLibre, leur moteur embarqué et le repli Matplotlib
restent inchangés. Aucun raccordement à `app.py` n'est réalisé. Aucune nouvelle
dépendance n'est ajoutée.

## Reproduire les tests

```powershell
node --test tests/frontend/test_ndvi_map.mjs tests/frontend/test_temporal_map.mjs
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider
.venv/Scripts/python.exe -B scripts/preview_temporal_map.py
```

Le dernier script vérifie les acquisitions réelles Akanda des 15 et 25 janvier
2025, appelle une fois le service scientifique existant, contrôle son empreinte,
prépare le contrat et écrit `outputs/temporal-map/akanda.html` (ignoré par Git).
Cette page d'environ 7,41 Mo contient les styles, le moteur local, le module,
les PNG et les vérifications navigateur. Elle n'a ni serveur ni téléchargement.
Elle doit être traitée comme un aperçu géographique à partager explicitement,
pas comme un fichier source à versionner.

Ouvrir ce fichier dans un navigateur WebGL, puis exécuter dans sa console :

```javascript
await runTemporalBrowserChecks();
// Instrumentation accessible dans la page de diagnostic uniquement :
temporalDemo.inspect();
temporalDemo.dispose();
```

Le contrôle réel effectue trois cycles montage/démontage, vérifie les contextes
WebGL et canevas, compare les quatre paramètres de caméra, surveille une boucle
d'événements, bascule les couches et contrôle le contenu public. Les tests
unitaires simulent aussi les erreurs de construction, la perte de contexte,
les événements différés et le changement responsive. Les informations de
diagnostic ne constituent pas une nouvelle interface scientifique.

Pour reproduire le scénario mobile : émuler 390 × 844 pixels, choisir chacune
des trois vues et vérifier `temporalDemo.inspect()` : un seul contexte et un
seul canevas actifs. Zoomer avec les boutons et changer de vue conserve la
caméra. Pour vérifier le démarrage hors ligne, créer un contexte navigateur
isolé neuf, activer Offline avant de charger le fichier et ignorer le cache.
Le contenu CSP autorise les scripts embarqués, les images data: et les workers
blob:, avec `connect-src data: blob:`. MapLibre utilise fetch pour décoder les
images data: ; `connect-src 'none'` empêcherait ce décodage local. HTTP(S), les
CDN, les tuiles distantes et les polices distantes restent interdits. Le module
ajoute son propre filtrage des requêtes d'images.

## Résultats obtenus sous Windows / Chrome 155

Les observations brutes figurent dans
`benchmarks/temporal_browser_2026-10-21.json`.

| Mesure | Résultat |
|---|---:|
| Premier montage bureau, appel synchrone | 81,3 ms |
| Premier montage bureau → état stable | 646,6 ms |
| Trois remontages : médiane montage | 42,3 ms |
| Trois remontages : médiane état stable | 519,4 ms |
| Changement de caméra → stabilité | 368,4 ms |
| Bascule delta → stabilité | 283,1 ms |
| Démarrage dans contexte neuf, Offline avant chargement | 540,2 ms après début du montage |
| Remontages Offline : médiane état stable | 442,6 ms |
| Mobile : changement de vue → stabilité | 365,6 à 380,1 ms |

La caméra testée est identique sur les trois cartes : centre [9.4, 0.52], zoom
12.2, orientation 23°, inclinaison 40°. Le compteur d'événements reste stable
pendant l'observation suivant la synchronisation. Le bouton de zoom mobile a
également été actionné dans le navigateur : +1 niveau, sans erreur.

La surveillance observe 3 contextes WebGL en bureau, 1 en mobile et 0 après
chaque démontage, avec 0 canevas restant. Les trois cycles de montage ont créé
12 contextes au total dans ce scénario, sans contexte perdu inattendu ; seuls
3 demeurent actifs avant le dernier démontage. Le démontage final des pages
de test confirme également 0 contexte et 0 canevas. Le compteur utilise des
WeakRef pour ne pas retenir artificiellement les contextes.

`performance.memory` rapporte 20,5 Mo sur les cycles bureau, et 31,2 Mo dans
le contexte séparé Offline/mobile. Ces valeurs restent constantes pendant les
cycles de chaque contexte, mais sont arrondies par Chrome. Elles ne démontrent
pas une absence de fuite et ne mesurent pas la mémoire native/GPU. Les textures
RGBA de trois images 1197 × 1205 représentent à elles seules environ 16,5 Mio,
hors copies, buffers du moteur, workers et caches d'images du navigateur.

Aucune requête HTTP(S), aucun message d'erreur ou avertissement nouveau ne sont
observés après correction de la CSP de la page de diagnostic. DevTools liste
uniquement le fichier local, les workers blob: et les images data:. Les entrées
Resource Timing peuvent omettre ces décodages ; leur nombre nul n'est pas un
compteur exhaustif des ressources. Offline a été configuré avant la première
navigation dans un contexte neuf, cache ignoré ; `navigator.onLine` est pourtant
resté vrai sous cette émulation. Il n'y a pas eu de déconnexion physique du
réseau. Le test confirme le fonctionnement du fichier autonome sans dépendance
réseau ; il ne valide pas encore le futur démarrage d'une application web.

Il n'existe aucun processus Python serveur dans ce test : zoom, navigation,
bascule et remontages ne peuvent donc appeler le pipeline. L'empreinte
scientifique calculée lors de la génération est strictement la référence :

```text
b0356911623b4af85939f9a5bb62f0f01cdff9ffd1dae875ca84b77523cb0e41
```

Tests : 9 JavaScript réussis (dont les 2 existants), 383 Python réussis,
3 ignorés pour des fichiers SCL locaux indisponibles ; 1 150 avertissements
Affine/Rasterio existants. Des captures bureau et mobile ont été visualisées
dans la conversation : données, masques, classes/delta et légendes sont visibles.
L'export des captures vers un fichier a été refusé par la configuration des
racines du connecteur navigateur ; les constats et mesures sont consignés ici.

## Limites restantes

Validation limitée à Chrome/Windows et aux données Akanda. Pas de test Safari,
Firefox ou appareil mobile physique, ni de mesure FPS ou mémoire GPU précise.
Les durées incluent le moteur local, les décodages PNG et les workers ; elles
ne sont pas les temps d'ouverture de Streamlit. La copie publique reste chargée
en mémoire même avec une seule carte mobile. Les grandes AOI nécessiteront
toujours le futur travail de tuilage déjà identifié. L'intégration Streamlit,
son repli automatique et la gestion des sessions restent les étapes suivantes.
