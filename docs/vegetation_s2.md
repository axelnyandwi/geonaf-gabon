# Végétation observée par Sentinel-2 — estimation expérimentale

**Statut : expérimental, non validé scientifiquement.** Les valeurs ci-dessous démontrent la
reproductibilité numérique du calcul, pas sa justesse.

## Définition

Pour une acquisition Sentinel-2 L2A et une zone d'étude, la surface « à NDVI ≥ 0,4 » est la somme
des surfaces exactes (aire équivalente EPSG:6933, pixels partiellement couverts comptés pour leur
part dans la zone) des pixels **valides** dont le NDVI calibré est ≥ 0,4 (comparaison en float32,
égalité incluse). C'est un indice spectral seuillé, pas une mesure directe de la végétation réelle.

- Seuil, sensibilité, couverture minimale, statut et plafond de fiabilité : profil versionné
  `S2_NDVI_VEGETATION_V0` (`geonaf-vegetation-ndvi-s2` 0.1.0-experimental), seuil 0,4,
  sensibilité 0,3 / 0,4 / 0,5, couverture minimale 30 %, statut `experimental`, fiabilité
  plafonnée à « faible ».
- Validité des pixels : chaîne NDVI existante (calibration BOA du XML, SCL au plus proche voisin,
  NDVI fini dans [−1, 1]) et politique SCL du **profil scientifique** de la zone, inchangé
  (`usable_land` : classes 4 et 5). Une politique acceptant nuages, ombres ou pixels défectueux
  (classes 1, 3, 8, 9, 10 ; aujourd'hui `observation`) est refusée avant tout calcul.

## Partition de la zone (`src/indicators/vegetation_s2.py`)

Chaque pixel reçoit une seule étiquette (première règle satisfaite) :

| Ordre | Catégorie | Règle |
|---|---|---|
| 1 | NDVI ≥ seuil | valide et NDVI ≥ seuil |
| 2 | observée, NDVI < seuil | valide et NDVI < seuil |
| 3 | sans donnée | SCL absent (NoData, classe 0, hors raster) ou partie de la zone hors de la grille analysée ; cause (bord de trace, donnée manquante…) non déterminée |
| 4 | non observée | selon le SCL : 1, 3, 8, 9, 10 (défectueux, ombres, nuages, cirrus) |
| 5 | observée mais exclue | classe observée hors politique (eau 6, zone sombre 2, non classée 7, neige 11…) |
| 6 | autre invalide | classe acceptée, réflectance ou NDVI invalide |
| — | non ventilée | règles 3–6 confondues quand les classes SCL ne sont pas disponibles (mosaïque) |

La somme des catégories égale l'aire de la zone à 10⁻⁶ près (relatif) ; un écart de cet ordre entre
l'aire géodésique de la zone et la somme des poids exacts est un arrondi géométrique (mesuré
≤ 3·10⁻⁹), jamais compté « sans donnée ». La part végétalisée est rapportée à la seule surface
observée exploitable ; **aucune valeur n'est rapportée à la zone entière** (pas d'extrapolation).

## Acquisitions réelles (`src/indicators/s2_inventory.py`)

- Acquisitions locales vérifiées de l'année (manifestes, SHA-256 de chaque actif), groupées par
  prise de vue ; au plus 12 calculées (les plus récentes).
- **Cohérence NDVI/SCL** : pour une tuile, les classes SCL sont lues dans le fichier découpé qui a
  construit le masque NDVI (`analyze_ndvi_observation_with_scl`), puis contrôlées
  (`check_scl_consistency` : même produit, même grille, aucun pixel valide hors politique).
- **Mosaïque multi-tuiles** : surfaces observée et à NDVI ≥ seuil publiées ; ventilation
  « non disponible » (classes SCL des tuiles non fusionnées).
- **Couverture < 30 %** : mesures conservées pour diagnostic, estimation territoriale « Non disponible ».
- Acquisition illisible ou altérée : listée avec un motif public, les autres sont calculées.
- Garde-fous de ressources (`assess_zone`) vérifiés avant tout calcul ; aucun tableau raster n'est
  conservé (≈ 4,8 Ko par inventaire de trois dates).
- Provenance par estimation : produits, instant d'acquisition, ingestion (manifeste), empreintes
  du manifeste et du SCL, calibration, profils scientifique et expérimental (identifiants, versions,
  empreintes), politique SCL, couverture, limites. Attribution : « Contains modified Copernicus
  Sentinel data 2025 », reprise de la note de publication du paquet v3, non revérifiée sur l'avis
  juridique Copernicus (PDF illisible lors de la vérification du 28/10/2026).

## Valeurs de référence — paquet v3, zone d'étude Akanda (non-régression)

Figées dans `tests/test_s2_vegetation_inventory.py` (tolérance relative 10⁻¹², comme
`test_demo_pack_science.py`). Zone : 144,11 km².

| Acquisition | Observée exploitable | NDVI ≥ 0,4 | Part | Exclue (eau…) | Non observée | Publiée |
|---|---|---|---|---|---|---|
| 15/01/2025 09:32 UTC | 96,16 km² (66,73 %) | 68,92 km² | 71,68 % | 24,99 km² | 22,97 km² | oui, fiabilité faible |
| 20/01/2025 09:33 UTC | 3,92 km² (2,72 %) | 0,85 km² | 21,77 % | 17,83 km² | 122,36 km² | non (< 30 %) |
| 25/01/2025 09:31 UTC | 92,97 km² (64,51 %) | 61,61 km² | 66,28 % | 17,84 km² | 33,30 km² | oui, fiabilité faible |

Sensibilité (part de la surface observée) : 15/01 — 82,5 / 71,7 / 56,9 % ; 25/01 — 78,1 / 66,3 /
50,7 % pour 0,3 / 0,4 / 0,5. Les deux dates publiées ne sont pas comparables entre elles : leurs
surfaces observées diffèrent.

## Performances (28/10/2026, Windows, Python 3.10 ; `scripts/benchmark_vegetation_s2.py`)

Un processus par cas ; trois acquisitions vérifiées par zone. Détail :
`docs/benchmarks/vegetation_s2_2026-10-28.json`.

| Source / zone | Froid | Chaud | Pic Python (tracemalloc) | Pic processus (après imports) |
|---|---|---|---|---|
| Paquet v3 / Akanda | 3,53 s | 2,51 s | 46,8 Mio | 217,7 Mio (122,6) |
| Local / Akanda (tuiles complètes JP2) | 4,78 s | 3,67 s | 46,8 Mio | 227,6 Mio (122,3) |
| Local / Libreville | 4,28 s | 3,41 s | 46,8 Mio | 229,9 Mio (122,7) |
| Local / Owendo | 4,31 s | 3,41 s | 46,8 Mio | 227,6 Mio (122,3) |

« Chaud » = même inventaire relancé dans le processus (poids surfaciques en cache, fichiers en cache
système) ; la vérification SHA-256 et le NDVI sont toujours recalculés. Non mesuré sur Streamlit
Cloud.

## Interface (`src/components/s2_vegetation.py`)

Section « Végétation observée par Sentinel-2 », sous les chiffres clés, avec les badges
« Estimation expérimentale » et « Non validée scientifiquement » et un avertissement permanent :
résultats non comparables à WorldCover 2021 ni entre acquisitions aux masques d'observation
différents.

- **Aucun calcul au chargement** : bouton « Calculer l'estimation Sentinel-2 » (une fois par
  session et par zone), garde-fous de ressources vérifiés juste avant (mémoire libre de l'hôte
  comprise, hors cache).
- **Cache** (`st.cache_resource`, 4 entrées) de l'inventaire agrégé, sans tableau raster ; clé :
  zone (SHA-256), année, profils scientifique et expérimental (empreintes), et pour chaque prise de
  vue l'identité des fichiers et l'empreinte du manifeste (`s2_inventory_key`). Changer de date ne
  recalcule rien ; toute modification d'un fichier change la clé, et l'intégrité SHA-256 complète
  est revérifiée à chaque calcul.
- **Date par défaut** : la plus récente atteignant la couverture minimale (25/01/2025 dans le paquet
  v3). Les autres restent sélectionnables ; une acquisition sous 30 % (20/01/2025) affiche
  « Non disponible », le motif, le badge « Non publiable » et ses mesures dans une section séparée
  « Données de diagnostic (non publiables) ».
- **Carte** : surface à NDVI ≥ 0,4 (km², ha), part de la surface observée exploitable, surface et
  part exploitables, surfaces observée mais exclue / non observée / sans donnée, fiabilité, date,
  source, seuil.
- **Détails repliables** : ventilation et définitions, sensibilité 0,3 / 0,4 / 0,5 (pour une date
  non publiable, uniquement dans ses données de diagnostic), provenance
  (produits, dates, profils et empreintes, politique SCL, méthode, motifs de fiabilité, limites,
  empreintes des manifestes). L'attribution Copernicus est affichée comme **non revérifiée**.

### Temps de la page d'accueil (`scripts/benchmark_s2_vegetation_streamlit.py`, paquet v3)

AppTest, un processus par mode (caches froids), trois séries le 28/10/2026 :

| Mesure | Avant L3c | Après L3c |
|---|---|---|
| Chargement initial (chiffres clés et analyse de la paire compris) | 11,0 – 13,5 s | 10,9 – 11,7 s |
| Premier calcul Sentinel-2 (clic) | — | 2,9 – 3,1 s |
| Changement d'acquisition (rerun, agrégat en cache) | — | 0,30 – 0,33 s |
| Autre session, même processus (cache partagé) | — | 0,33 – 0,36 s |

L'écart de chargement initial est dans le bruit de mesure : la section n'affiche qu'un bouton.

## Limites

Voir `VEGETATION_S2_LIMITS` (`src/indicators/provenance.py`) : indice seuillé ≠ végétation ;
seuil non calibré en climat équatorial ; mangroves (marées, inondation, classement en eau ou
zone sombre) ; zones humides herbacées ; végétation urbaine à 10 m ; nuages fins et erreurs
Sen2Cor ; saison non évaluée ; surface non observée non estimée ; non comparable à WorldCover 2021.
