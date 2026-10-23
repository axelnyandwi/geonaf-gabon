# Référentiels geoBoundaries et comparaison — lot C

## Adaptateur (`src/data/geoboundaries.py`)

Récupère un jeu (`gbOpen`, `gbHumanitarian`) pour un pays et un niveau, puis enregistre chaque unité
dans le registre territorial. Indépendant de Streamlit et du moteur d'analyse ; appel réseau
uniquement sur action explicite (`import_geoboundaries`).

| Étape | Règle |
|---|---|
| Métadonnées | une requête par pays **et niveau** (`/api/current/<jeu>/<ISO3>/<ADMn>/`, quelques Ko) ; un niveau non publié (404) est refusé, jamais inventé |
| Contrôles | pays et niveau conformes à la demande, champs obligatoires, URL HTTPS, licence reconnue, date de production lisible |
| Licences | table explicite → SPDX ; licence inconnue ou non commerciale ⇒ import refusé ; partage à l'identique conservé |
| Téléchargement | seulement si aucun instantané du même jeu/pays/niveau/production n'est stocké ; `ExternalHttpClient` (TLS GEONAF, plafonds, reprises) ; redirections suivies à la main (3 au plus), hôte revérifié à chaque saut, HTTPS obligatoire ; GeoJSON ≤ 32 Mio |
| Stockage | réponse brute écrite une fois (`external/geoboundaries/<jeu>/<ISO3>/<ADMn>/<production>-<sha256>.geojson`) + métadonnées, SHA-256 et date de récupération ; modification détectée |
| Validation | chaque unité validée seule (`aoi_identity`) : unité invalide rejetée et comptée, jamais réparée |
| Enregistrement | `admin_unit`, statut « référentiel communautaire » (gbOpen) ou « référentiel humanitaire (COD) » (gbHumanitarian), jamais « officiel vérifié » ; identifiant `<iso3>-<jeu>-<niveau>-<nom>` (suffixe d'empreinte si le nom est en double) : deux jeux ne partagent jamais un territoire |
| Provenance | source, produit, version (date de production), millésime (`boundaryYearRepresented`), licence SPDX, attribution, URL, date de récupération ; type local repris **tel quel** (`boundaryCanonical`), « non déclaré par la source » sinon |
| Versions | nouvelle production ⇒ nouvelle version des unités ; versions et fichiers antérieurs conservés |

Aucune limite communale n'existe dans geoBoundaries pour le Gabon : Akanda et Owendo ne sont pas créées.

## Comparaison (`src/services/territory_comparison.py`)

`compare_geometries(a, b)` et `compare_territories(registre, id_a, id_b)` :

- surfaces A et B, intersection, union, différence symétrique (km²) ;
- recouvrement = intersection / union (indice de Jaccard, 100 % = identiques) ; parts de A couverte
  par B et de B couverte par A ;
- différence relative de surface = (B − A) / A.

Méthode : géométries validées sans réparation, arêtes densifiées à 0,001° en WGS84 puis projection
équivalente EPSG:6933 (aires vraies sur l'ellipsoïde ; vérifié contre l'aire géodésique `pyproj`
à 1e-4). Chaque résultat porte la mention « écart entre deux représentations, pas un changement du
territoire réel » ; millésimes différents et versions d'un même territoire sont signalés comme tels.
Les enregistrements ne sont ni fusionnés ni modifiés. `match_by_normalized_name` propose des paires
par nom normalisé (accents et casse ignorés), à vérifier ; un nom ambigu n'est pas apparié.

## Validation réelle (manuelle, réseau, répertoire temporaire — 23/10/2026)

| Jeu | Niveau | Unités | Rejets | Téléchargé | Durée | Licence | Millésime | Type local |
|---|---|---|---|---|---|---|---|---|
| gbOpen | ADM0 | 1 | 0 | 55 Ko | 4,6 s | CC-BY-SA-3.0 | 2005 | « Gabon » (tel que publié) |
| gbOpen | ADM1 | 9 | 0 | 108 Ko | 1,9 s | CC-BY-SA-3.0 | 2005 | « Provinces » |
| gbOpen | ADM2 | 49 | 0 | 410 Ko | 3,1 s | CC-BY-3.0 | 2018 | « Department » |
| gbHumanitarian | ADM0 | 1 | 0 | 78 Ko | 1,7 s | CC-BY-3.0-IGO | 2015 | non déclaré |
| gbHumanitarian | ADM1 | 9 | 0 | 115 Ko | 1,9 s | CC-BY-3.0-IGO | 2015 | non déclaré |
| gbHumanitarian | ADM2 | 48 | 0 | 232 Ko | 3,1 s | CC-BY-3.0-IGO | 2015 | non déclaré |

Ré-import gbOpen ADM2 : instantané réutilisé, 0 octet de géométrie, 1,4 s (requête de métadonnées
et ré-enregistrement idempotent). Durées incluant l'espacement d'1 s entre requêtes par hôte.

Départements : 46 paires par nom ; sans correspondance : gbOpen « Doutsita », « Komo-Ocèan »,
« Mpassa » ; gbHumanitarian « Doutsila », « Passa » (orthographes différentes : non appariées
automatiquement). 46 comparaisons en 0,8 s (18 ms chacune).

| Département | gbOpen | gbHumanitarian | Recouvrement | Écart de surface |
|---|---|---|---|---|
| Komo-Mondah | 2 104,0 km² | 5 648,1 km² | 31,0 % | +168,4 % |
| Haute-Banio | 1 931,9 | 750,2 | 31,1 % | −61,2 % |
| Libreville | 184,7 | 271,8 | 36,6 % | +47,1 % |
| Zadiè (meilleur accord) | 10 133,0 | 10 329,4 | 94,6 % | +1,9 % |

Recouvrement médian 76,9 %, minimum 31,0 %. Ces écarts décrivent des représentations divergentes
(sources, millésimes 2018/2015, généralisation), pas une évolution des départements.

## Limites et risques résiduels

- Licences : table limitée aux libellés rencontrés ; un nouveau libellé bloque l'import jusqu'à
  revue (choix prudent). La licence est celle déclarée par geoBoundaries, non vérifiée à la source
  primaire.
- Type local repris tel que publié (« Gabon » pour ADM0 gbOpen, « Unknown » ⇒ non déclaré).
- Pas de lien parent/enfant automatique entre niveaux (niveaux non emboîtés exactement).
- L'identifiant suit le nom : un renommage par la source crée un nouveau territoire.
- Aucun enregistrement réel n'a été écrit dans `data/` ; l'import effectif se fera sur action
  explicite (lot Streamlit).
