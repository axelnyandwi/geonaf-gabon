# Identité géométrique des zones d'étude et limites administratives (diagnostic)

## Empreinte de géométrie normalisée (`src/geo/aoi_identity.py`)

`load_aoi_identity(path, aoi_id)` valide strictement la zone puis calcule son empreinte :

1. chaque entité doit être un Polygon/MultiPolygon non vide et valide, avec un CRS et des
   coordonnées WGS84 finies et dans la plage ; sinon `ValueError` — **aucune réparation** ;
2. reprojection en EPSG:4326, fusion (`union_all`) ;
3. forme canonique (`shapely.normalize` : point de départ et orientation des anneaux, ordre des
   parties), coordonnées arrondies à 1e-7° (≈ 1 cm) ;
4. SHA-256 de `geonaf-aoi-geometry-v1` + WKB.

Les propriétés (noms) et la mise en forme du fichier n'entrent pas dans l'empreinte. Comportement
conservateur : une même surface décrite autrement (par ex. deux moitiés fusionnées, qui gardent des
sommets colinéaires) a une autre empreinte ⇒ nouvelle récupération, jamais une réutilisation
erronée. `file_sha256` (octets du fichier) reste utilisé par les caches existants.

## Réutilisation des couches stockées

| Règle | Où |
|---|---|
| Une fenêtre n'est utilisée que pour le même identifiant de zone (les zones voisines se recouvrent) | `layers_for_aoi` |
| Une fenêtre enregistrée pour **une autre empreinte** n'est jamais utilisée | `layers_for_aoi`, `fetch_worldcover_windows`, `reusable_window` (S1) |
| Une fenêtre antérieure aux empreintes n'est utilisée que si elle couvre l'emprise de la zone à une cellule près | idem |
| Les nouvelles fenêtres sont rangées sous `<produit>_<zone>-<empreinte[:12]>.tif` et portent `aoi_geometry_sha256` dans leur provenance ; rien n'est écrasé | WorldCover, Sentinel-1 |
| Clé du cache radar de la page multisource : empreinte géométrique incluse | `cached_radar` |

Correctif : une fenêtre Sentinel-1 stockée était auparavant renvoyée **sans contrôle de
couverture** ; une zone élargie sous le même nom recevait l'ancienne fenêtre. Elle est désormais
refusée (sans empreinte) ou remplacée par une nouvelle fenêtre (avec empreinte).

Compatibilité : les 9 fenêtres historiques (WorldCover, S1 VV/VH × Akanda, Libreville, Owendo)
couvrent leur zone à moins d'une cellule (écart maximal 0,74 cellule, WorldCover Akanda) et restent
utilisées ; les résultats d'Akanda sont inchangés (tests de non-régression). OSM n'est pas lié à
l'empreinte : la requête porte sur l'emprise, le découpage au polygone est fait à l'analyse.

## Diagnostic des limites administratives du Gabon (23/10/2026, réseau, rien stocké)

Diagnostic manuel local, non versionné (`scripts/check_geoboundaries_gabon_live.py`) : client
`ExternalHttpClient`, liste d'hôtes propre au diagnostic, redirections suivies à la main après
contrôle d'hôte.

| Jeu | Niveaux | ADM2 | Année / source | Licence |
|---|---|---|---|---|
| gbOpen | ADM0, ADM1 (9 provinces), ADM2 (49 « Department ») | 35–572 sommets, 0 invalide | ADM0/1 : 2005, Wikipedia ; ADM2 : 2018, Wikimedia | ADM0/1 CC BY-SA 3.0 (partage à l'identique) ; ADM2 CC BY 3.0 |
| gbHumanitarian | ADM0, ADM1, ADM2 (48) | 41–568 sommets, 0 invalide | 2015, SALB / HDX COD-AB | CC BY 3.0 IGO |
| gbAuthoritative | — (HTTP 404) | | | |

Constats :

- **Aucun niveau communal** : Akanda et Owendo n'existent dans aucun jeu geoBoundaries ; ADM2 =
  départements. « Libreville » est un département.
- **Désaccord majeur entre jeux** pour les mêmes unités : Libreville 184,7 km² (gbOpen) contre
  271,8 km² (gbHumanitarian) ; Komo-Mondah 2 104 km² contre 5 648 km². La zone d'acquisition
  Akanda recoupe Libreville à 37,3 % (gbOpen) contre 7,0 % (gbHumanitarian). Komo-Océan n'existe
  que dans gbOpen (49 contre 48 unités). Totaux nationaux différents entre niveaux d'un même jeu
  (ADM1 259 093 km², ADM2 262 372 km² dans gbOpen) : les niveaux ne sont pas emboîtés exactement.
- Géométries **grossières** (médiane ≈ 170 sommets par département) : adaptées à des statistiques
  départementales, pas à des limites communales ou côtières précises.
- Aucune de ces limites n'est officielle au sens d'une publication gouvernementale vérifiée.
- **OSM** (base 23/10/2026, étiquettes seulement) : départements Komo-Océan, Komo-Mondah,
  Libreville (`admin_level=6`) ; niveau 9 : « Commune de Libreville », deux relations
  « 1er Arrondissement » redondantes, « 2ème Arrondissement d'Akanda » ; **aucune relation de
  commune d'Akanda ni d'Owendo**.

## Performances sur polygones administratifs réels (benchmark manuel local, non versionné)

Grille UTM 10 m (résolution Sentinel-2) couvrant l'unité :

| Unité | Sommets | Surface | Cellules | Identité | Masque | Poids exacts |
|---|---|---|---|---|---|---|
| Libreville (gbOpen) | 35 | 184,7 km² | 3,7 M | 0,01 s | 0,02 s | 3,4 s |
| Libreville (gbHumanitarian) | 80 | 271,8 km² | 4,8 M | 0,01 s | 0,02 s | 2,4 s |
| Komo-Mondah (gbOpen) | 150 | 2 104 km² | 37,9 M | 0,01 s | 0,12 s | 21,1 s |
| Komo-Mondah (gbHumanitarian, 3 parties) | 437 | 5 648 km² | 99,5 M | 0,01 s | 0,24 s | 39,7 s |

Σ poids / aire géodésique : écart ≤ 3e-15. Le coût dépend du nombre de cellules plus que des
sommets ; à l'échelle d'un département, une grille 10 m dépasse 1 tuile Sentinel-2, l'emprise
Overpass bornée (0,5°) et plusieurs centaines de Mo de mémoire (tableau float64 de 99,5 M cellules
≈ 800 Mo).

## Limites

- Résidus flottants : une cellule hors zone mais adjacente à une arête peut recevoir ≈ 1e-6 m²
  (mesuré : 0,00013 m² au total sur une AOI en L) ; sans effet sur les surfaces affichées.
- L'empreinte dépend de la précision 1e-7° après reprojection : une zone stockée dans un autre CRS
  peut changer d'empreinte.
