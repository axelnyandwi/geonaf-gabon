# Transitions de classe NDVI entre deux acquisitions Sentinel-2 — L4b

Statut : **expérimental, fiabilité plafonnée à « faible »**. Non branché sur l'interface (L4c).
Moteur de calcul : L4a (`src/indicators/transitions_s2.py`, profil `S2_NDVI_TRANSITION_V0`).
Connexion aux acquisitions réelles : `src/indicators/s2_pairs.py` ; assemblage :
`build_s2_transition_indicators` (`src/indicators/assembly.py`).

## Sélection des paires

1. **Inventaire L3 réutilisé** (`s2_vegetation_inventory`) : acquisitions vérifiées de l'année
   (manifestes stricts, SHA-256), avec leur couverture exploitable individuelle. Aucun
   téléchargement.
2. **Planification pure** (`plan_pairs`, aucune lecture de fichier) : toutes les paires
   chronologiques. La surface commune étant incluse dans la surface exploitable de chaque date,
   `min(couverture individuelle)` est une **borne supérieure** de la couverture commune.
   - borne < 30 % (minimum du profil) : paire **écartée avant calcul**, motif conservé ;
   - borne ≥ 30 % : paire candidate seulement. **La borne ne prouve jamais la comparabilité.**
   - mosaïques multi-tuiles écartées : la chaîne de paires historique ne lit qu'une tuile par date.
   - ordre des candidates : date la plus récente d'abord, puis date antérieure la plus récente.
3. **Garde-fous de ressources** (`assess_zone`, calibré sur l'analyse de paire) avant tout calcul.
4. **Évaluation** dans cet ordre, au plus `max_pairs` paires (défaut 3, configurable) :
   chaîne historique `analyze_local_akanda` (vérification SHA-256, NDVI calibré, politique SCL du
   profil scientifique, masque commun), puis `measure_pair_result` et `pair_assessment` (L4a).
   La **couverture commune réelle** décide de la publication. L'évaluation s'arrête à la première
   paire publiable, qui est la paire par défaut.
5. Statuts : `publiable`, `non publiable` (calculée mais refusée, ou erreur publique),
   `écartée avant calcul`, `non évaluée` (limite, garde-fou ou paire plus récente retenue). Seules
   les paires publiables donnent des valeurs ; les autres gardent leur motif.

Contrôle de cohérence : une couverture commune réelle supérieure à la borne de l'inventaire
(entrées incohérentes) rend la paire non publiable.

## Cohérence scientifique

- Masque commun et surface commune : ceux de la chaîne historique, bit à bit
  (`analysis.comparable_area_m2`) ; aucune seconde définition des pixels comparables.
- Politiques SCL acceptant nuages, ombres, cirrus ou pixels défectueux refusées avant calcul.
- Grilles non alignées : paire non publiable, aucune transition conservée (`measure = None`).
- Seuil de **classe** NDVI 0,4 (profil de végétation, imbriqué dans le profil de transition) ≠
  seuil de **variation** ΔNDVI 0,10 du profil scientifique, qui n'est pas utilisé ici.
- Statistiques historiques, `PAIR_DIGEST` et empreintes des profils inchangés.
- Indicateurs `s2.pair.*` distincts de WorldCover, OSM (`build_indicators`) et de L3
  (`s2.vegetation_km2`).

## Provenance et indicateurs

`pair_provenance` (JSON, aucun chemin local) : deux dates d'acquisition et d'ingestion,
identifiants produits, collection, item STAC, empreintes des manifestes et du SCL, satellites et
orbite relative (lus dans l'identifiant produit ; l'orbite absolue n'est pas enregistrée dans les
manifestes vérifiés : `null`), profils scientifique / végétation / transition avec leurs
empreintes, seuils de classe et de variation, borne et couverture commune réelle, comparabilité
saisonnière de la chaîne, motifs, avertissements, statut et fiabilité.

`build_s2_transition_indicators` produit trois indicateurs (estimation expérimentale) : surface
commune (`s2.pair.common_km2`), ΔNDVI moyen pondéré (`s2.pair.delta_ndvi_mean`, avec moyennes et
médianes par date) et surface ayant changé de classe (`s2.pair.class_transitions_km2`, ventilée
en quatre transitions avec sensibilité 0,3 / 0,4 / 0,5 et solde, qui n'est pas un gain ni une
perte de végétation). Limites : `TRANSITION_S2_LIMITS` (`src/indicators/provenance.py`).

## Valeurs de référence — paquet v3, zone d'étude Akanda (non-régression)

Figées dans `tests/test_s2_pairs.py` avec la tolérance relative 10⁻¹² des autres références du
paquet (sommes float64 de poids projetés ; écart Windows/Linux observé ≤ 6·10⁻¹⁵). Calculées le
29/10/2026 (Windows, Python 3.10). Reproductibilité, pas validation scientifique.

| Paire | Borne | Statut |
|---|---|---|
| 15/01 → 25/01/2025 | 64,51 % | **publiable** (par défaut), fiabilité faible |
| 20/01 → 25/01/2025 | 2,72 % | écartée avant calcul |
| 15/01 → 20/01/2025 | 2,72 % | écartée avant calcul (calculée de force : refusée, < 30 %) |

Paire 15/01 → 25/01 (intervalle 10 jours, avertissement « intervalle court ») :

| Grandeur | Valeur |
|---|---|
| Surface commune | 84,47 km² (845 383 pixels) |
| Couverture commune réelle | 58,61 % de 144,11 km² |
| NDVI moyen pondéré 15/01 → 25/01 | 0,4976 → 0,4648 |
| ΔNDVI moyen / médian pondéré | −0,0329 / −0,0264 |
| NDVI ≥ 0,4 puis < 0,4 | 6,73 km² |
| NDVI < 0,4 puis ≥ 0,4 | 2,79 km² |
| Restée ≥ 0,4 | 53,19 km² |
| Restée < 0,4 | 21,75 km² |
| Diagnostic par blocs de 1 km² | 140 blocs, 78 % avec ≥ 10 % de surface commune, 18 % sans |

Les surfaces valides par date de la chaîne de paires sont exactement les surfaces observées L3
(96,16 et 92,97 km²) : sur ces données, la borne est bien une borne supérieure.

## Performances (29/10/2026, Windows, Python 3.10 ; `scripts/benchmark_transitions_s2.py`)

Deux processus neufs par zone (inventaire, puis paires à partir de l'inventaire sérialisé).
Détail : `docs/benchmarks/transitions_s2_2026-10-29.json`.

| Source / zone | Inventaire L3 | Sélection | Calcul froid / chaud | Pic Python | Pic processus (après imports) | Cache |
|---|---|---|---|---|---|---|
| Paquet v3 / Akanda | 4,53 s | 0,04 ms | 3,93 / 2,80 s | 98,8 Mio | 268,5 Mio (122,6) | 7 660 o |
| Local / Akanda | 5,32 s | 0,03 ms | 4,91 / 3,75 s | 98,8 Mio | 282,5 Mio (122,7) | 7 660 o |
| Local / Libreville | 5,21 s | 0,04 ms | aucune paire calculée | — | 122,8 Mio | 6 237 o |
| Local / Owendo | 5,44 s | 0,04 ms | aucune paire calculée | — | 122,9 Mio | 6 222 o |

Libreville (bornes 0,14 / 16,59 / 0,14 %) et Owendo (0,12 / 0,00 / 0,00 %) : aucune paire
n'atteint 30 %, toutes sont écartées sans calcul. « Chaud » : poids surfaciques en cache et
fichiers en cache système ; la vérification SHA-256 et la chaîne sont toujours recalculées.

## Cache et limites pour Streamlit Cloud

- Clé : `s2_pairs_key` = clé de l'inventaire (empreinte de la zone, révisions des fichiers et
  manifestes de chaque acquisition, profils scientifique et de végétation) + empreinte du profil
  de transition + `max_pairs`. Pas un contrôle d'intégrité : les SHA-256 sont vérifiés à chaque
  calcul.
- Objet mis en cache : nombres seulement (aucun tableau raster, testé), ≈ 7,7 ko.
- Non mesuré sur Streamlit Cloud. Sur ce poste, une paire ajoute ≈ 146 Mio au-dessus de la base
  après imports (pic processus), et 3 à 5 s par paire évaluée ; `max_pairs` borne le pire cas
  (3 paires ≈ 15 s). La mémoire réellement disponible sur l'hébergeur n'est pas garantie : les
  garde-fous lisent la mémoire libre de l'hôte et refusent avant calcul.
- `tracemalloc` ne voit qu'une partie de la mémoire (tampons GDAL non tracés) : le pic processus
  fait foi.

## Limites

Voir `TRANSITION_S2_LIMITS` : transitions de classe spectrale sans attribution de cause ; seuil
non calibré ; surface non commune non estimée ; conditions d'acquisition non neutralisées ;
erreurs possibles du SCL ; comparabilité saisonnière non évaluée ; non comparable aux autres
sources. Satellites et orbite relative déduits de l'identifiant produit (convention ESA).
