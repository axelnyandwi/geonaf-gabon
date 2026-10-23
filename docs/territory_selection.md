# Sélection territoriale, comparaison des frontières et garde-fous dans Streamlit — lot D

Page `pages/4_Territoires.py` (« Territoires et frontières », lien depuis l'accueil). Interface :
`src/components/territory_explorer.py`, carte `src/components/maplibre/territory.js`, garde-fous
`src/components/resource_panel.py`. Logique sans Streamlit : `src/services/territory_catalog.py`.

## Sélection

| Type | Origine | Statut affiché |
|---|---|---|
| Zone d'étude GEONAF | `data/aoi/<id>.geojson` (lecture seule, non inscrite au registre) | « zone d'acquisition : ni une commune ni une autre unité administrative » |
| Territoire administratif | registre, après chargement explicite d'un référentiel | « référentiel communautaire / humanitaire : non officiel, non vérifié » |
| Zone personnalisée | registre, après import | « fourni par l'utilisateur : statut administratif non vérifié » |

Pour un territoire administratif : référentiel (gbOpen, gbHumanitarian), niveau (pays, provinces,
départements), unité ; fiche nom, type local déclaré, surface, source, production, millésime,
licence (partage à l'identique signalé), date de récupération, identifiant/version, limites.
Le chargement d'un référentiel est un **bouton explicite** ; changer une option ne déclenche aucun
accès réseau. Le contour exact (coordonnées arrondies à 1e-6° pour l'affichage) est dessiné sur une
carte MapLibre embarquée, sans fond externe.

## Comparaison des frontières

Niveau → paire d'unités de même nom normalisé (Libreville par défaut). Deux contours distincts
(gbOpen orange plein, gbHumanitarian bleu pointillé), partie commune et parties propres colorées,
légende ; surfaces, partie commune, différence symétrique, recouvrement (commun / union), parts
couvertes, écart relatif ; provenance des deux sources côte à côte. Les unités sans correspondance
sont listées (jamais masquées). Avertissement permanent : un écart n'est jamais un changement
temporel réel.

## Garde-fous branchés

| Page | Opération | Contrôles |
|---|---|---|
| Accueil | analyse de la paire | cellules, mémoire, tuiles S2 |
| Série NDVI | calcul de la série (avant le bouton) | idem |
| Multisource | NDVI + croisements | idem + emprise OSM + volume OSM (taille de l'instantané stocké, sinon inconnu) |
| Territoires | analyse multisource future du territoire | idem, volume OSM inconnu |

Un **refus** est une erreur qui bloque avant tout calcul lourd. Un avertissement ou un inconnu est
une légende (verdict en gras et motifs) dans les pages d'analyse, un encadré dans la page
Territoires. Chaque verdict rappelle qu'il s'agit d'une estimation calibrée sur le poste de
développement, pas d'une garantie sur un serveur public. Seuils mémoire adaptés à la machine :
avertissement au-delà de 50 % de la mémoire libre, refus au-delà de 90 % (ou 2 Gio).

Correctif de calibration de ce lot : le refus à 50 % de la mémoire libre bloquait l'analyse
multisource d'Akanda sur le poste chargé (1,08 Gio libre, estimation 838 Mio, pic réel mesuré
526–670 Mio). Test de non-régression ajouté.

## Import GeoJSON

Intégré (réutilise `assess_import`, `aoi_identity`, le registre) : une zone par opération (les
entités du fichier sont fusionnées), taille contrôlée avant lecture, géométrie valide sans
réparation, sommets et parties bornés ; nom, auteur/organisme, licence, date de la source et
confirmation des droits obligatoires ; statut toujours « fourni par l'utilisateur » (aucune option
« officiel » dans l'interface). Identifiant `import-<nom>-<empreinte>` ; ré-import identique
idempotent.

## Validation navigateur (23/10/2026, données réelles)

- Akanda : zone d'acquisition, contour 12 × 12 km, 144,11 km², verdict « inconnu » (volume OSM non
  estimé sur cette page).
- Chargement explicite gbOpen ADM2 (49 unités) puis gbHumanitarian ADM2 (48 unités).
- Libreville gbOpen : 184,70 km², millésime 2018, CC-BY-3.0, « Department » ; verdict avertissement
  (mémoire 718 Mio > 645 Mio, seuil adapté à 1 291 Mio libres).
- Libreville gbHumanitarian : 271,76 km², millésime 2015, CC-BY-3.0-IGO, type non déclaré ;
  avertissement (4,8 M cellules > 4 M).
- Comparaison Libreville : commune 122,42 km², différence symétrique 211,61 km², recouvrement
  36,65 % ; non appariées affichées (gbOpen : Doutsita, Komo-Ocèan, Mpassa ; gbHumanitarian :
  Doutsila, Passa).
- Refus : département Abanga-Bignè (145,7 M cellules, 21,8 Gio estimés) bloqué avant calcul.
- Non-régression : accueil Akanda (verdict autorisé, 375 Mio estimés) et multisource Akanda
  (autorisé, 838 Mio, OSM 23 Mio) avec les valeurs historiques (NDVI médian 0,5050, couverture
  63,78 %, 36 012 bâtiments OSM, 635 km de voies).

Écritures dans `data/` lors de la validation : `external/geoboundaries/{gbOpen,gbHumanitarian}/GAB/ADM2/`
et 97 enregistrements `territories/records/` (unités administratives ; aucune zone historique).

## Performances

- Rendu serveur de la page Territoires (AppTest, comparaison incluse) : 1,4–1,9 s.
- Chargement d'un référentiel ADM2 : 2–5 s (métadonnées, téléchargement 230–410 Ko, 48–49
  enregistrements), instantané réutilisé ensuite.
- Mémoire du serveur Streamlit : 181 Mio après sélection et comparaison ; 314 Mio (pic 421) après
  l'analyse de paire d'Akanda ; 523 Mio (pic 550) après le multisource d'Akanda.

## Limites

- Les analyses (paire, série, multisource, chiffres clés) restent limitées aux zones historiques :
  le territoire choisi n'est pas encore transmis aux pages d'analyse.
- Pas de fond de carte ; pas de dessin interactif.
- Appariement par nom seulement ; orthographes divergentes non rapprochées.
- Import non vérifié dans le navigateur (couvert par les tests du service).
