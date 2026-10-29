# Transitions de classe NDVI entre deux acquisitions Sentinel-2 — L4b

Statut : **expérimental, fiabilité plafonnée à « faible »**. Interface : L4c (section ci-dessous).
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

## Interface (L4c, `src/components/s2_transitions.py`)

Sous-section « Comparaison temporelle sur surface commune » de « Végétation observée par
Sentinel-2 » (page d'accueil), indépendante de l'estimation d'une date.

- **Toujours visibles** : badges « Estimation », « Expérimental », « Non validée
  scientifiquement » ; avertissement permanent (une transition de classe spectrale ne démontre ni
  déforestation, ni reboisement, ni modification réelle de l'occupation du sol) ; explication
  distincte du seuil de classe NDVI 0,4 et du seuil de variation ΔNDVI 0,10 des analyses
  historiques, qui n'intervient pas ici.
- **États** : avant activation (bouton « Comparer deux acquisitions », aucun calcul ni inventaire
  au chargement) ; calcul en cours (indicateur d'attente) ; résultat disponible ; résultat non
  disponible (motif, badge « Non publiable », aucun chiffre) ; erreur de traitement (message
  public, sans chemin ni détail d'exception) ; garde-fou en refus (aucun calcul).
- **Sélection** : liste des paires (paire par défaut = la plus récente publiable, 15/01 → 25/01
  sur le paquet v3) ; paires écartées avec leur motif ; tableau triable de toutes les paires. Une
  paire non calculée par l'évaluation par défaut n'affiche **aucun chiffre** tant que le lecteur
  ne demande pas son calcul (« Calculer cette paire », `s2_pair_selection(only=…)`, mêmes
  contrôles et même chaîne).
- **En priorité** : surface commune (km², ha, % de la zone d'étude), surfaces NDVI ≥ 0,4 → < 0,4 et
  < 0,4 → ≥ 0,4, ΔNDVI moyen pondéré, fiabilité et statut ; mention « zone d'étude, pas la commune
  administrative ».
- **Repliables** : quatre catégories (tableau triable, surfaces restées au-dessus / au-dessous,
  solde net présenté comme différence de surfaces, ni perte ni gain réel) ; sensibilité 0,3 / 0,4 /
  0,5 ; diagnostic par cellules de 1 km² (projection à surfaces équivalentes, attribution par le
  centre du pixel, sans valeur de preuve de représentativité) ; provenance et limites.
- **Cache** : `st.cache_resource` sur `s2_pairs_key` (zone, acquisitions, manifestes, profils,
  limite, paire demandée) ; résultats agrégés seulement. L'inventaire L3 est réutilisé (même cache).
- **Garde-fou** : `show_assessment` avant tout calcul, avec la mémoire libre de l'hôte ou du
  conteneur (cgroup, voir `docs/resource_guards.md`).

### Performances de l'interface (29/10/2026, Windows, AppTest ; `scripts/benchmark_s2_transitions_streamlit.py`)

Un processus neuf par mesure ; AppTest exécute toute la page d'accueil (chiffres clés et analyse
scientifique de paire comprises), sans navigateur. Détail :
`docs/benchmarks/s2_transitions_streamlit_2026-10-29.json`.

| Mesure (paquet v3) | Avant L4c | Après L4c |
|---|---|---|
| Chargement initial (4 mesures) | 12,48 · 12,51 · 12,55 · 13,92 s | 13,15 · 13,25 · 13,50 · 13,83 s |
| Rendu initial de la sous-section seule | — | ≈ 1 ms |
| Premier calcul (inventaire L3 + paire) | — | 6,2 à 7,7 s |
| Changement de paire (paire écartée) | — | 0,42 s |
| Autre session, même processus (cache) | — | 0,42 à 0,52 s |
| Pic mémoire du processus | ≈ 397 Mio | ≈ 471 Mio |

Données locales (tuiles complètes) : chargement 14,02 s avant, 11,71 s après ; premier calcul
8,75 s ; changement de paire 0,45 s ; autre session 0,48 s ; pic 399 → 485 Mio.

L'écart de chargement initial entre médianes (≈ 12,5 s → ≈ 13,4 s) ne vient pas de la
sous-section, dont le rendu mesuré directement prend ≈ 1 ms : il relève de la variabilité du reste
de la page (analyse de paire scientifique, poste partagé). Le pic mémoire après calcul (+ 74 à
86 Mio) correspond à l'inventaire et au calcul de la paire, lancés seulement à la demande.

### Contrôle mobile (Playwright, 390 × 844, paquet v3)

Aucun élément ne dépasse la largeur de l'écran (contrôle des boîtes englobantes de la zone
principale, hors défilement interne des tableaux) ; tri numérique vérifié sur le tableau des
catégories (2,79 < 6,73 < 21,75 < 53,19). Corrigé pendant le contrôle : diagnostic par cellules
affiché en liste (la colonne des valeurs sortait de l'écran), date d'ingestion formatée, libellés
de la liste des paires raccourcis (tronqués sur téléphone). Les colonnes au-delà de la largeur
d'un tableau (« Part de la surface commune », « Motif ») se lisent par défilement horizontal
interne du tableau.
