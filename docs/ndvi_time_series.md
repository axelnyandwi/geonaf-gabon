# Séries temporelles NDVI — lot 3

## Usage

Accueil → « Série temporelle NDVI ». Choisir un profil scientifique, une période
UTC, le seuil de lacune (jours) et au moins trois acquisitions locales vérifiées,
puis cliquer sur « Calculer la série ». Aucun calcul n'est lancé par un simple
rerun ; un changement de paramètre demande un nouveau clic. Aucun téléchargement.

## Chaîne de calcul

`src/services/ndvi_time_series.py` (aucune dépendance Streamlit) applique à chaque
date **exactement la chaîne de l'analyse de paire** :

1. revalidation du contrat `Sentinel2Acquisition` et contexte saisonnier du profil ;
2. découpage AOI (`all_touched`) de B04, B08 et SCL ;
3. NDVI calibré avec les `BOA_ADD_OFFSET` et `BOA_QUANTIFICATION_VALUE` du XML du produit ;
4. lecture sur la grille commune (`_read_ndvi_on_grid`, `scl_policy_mask_on_grid`)
   avec la politique SCL du profil (`usable_land` : classes 4 et 5) ;
5. exclusion des NDVI hors [−1, 1] (comptés).

Grille : `build_series_grid` (ajout à `src/geo/temporal.py`) reprend les règles de
`build_common_grid` pour N rasters : CRS, résolution et origine de la première date,
emprise = intersection de toutes les dates, cellules alignées vers l'intérieur. Pour
deux dates, les deux fonctions donnent la même grille (testé).

## Deux familles de statistiques

- **Propres à chaque date** : pixels valides à cette date, touchés par l'AOI. Les
  ensembles de pixels diffèrent entre dates (nuages, ombres) : ces valeurs **ne
  forment pas une tendance**. Le graphique les affiche en points non reliés.
- **Pixels communs** : pixels valides à *toutes* les dates retenues. Seule cette
  courbe compare les mêmes pixels.

Une date est retenue si sa couverture valide de l'AOI (pondérée par l'intersection
exacte cellule/AOI, EPSG:6933) atteint la couverture minimale du profil. Une date
exclue garde ses statistiques propres et un motif explicite.

Statut (jamais « validé ») :

| Statut | Conditions |
|---|---|
| `not_comparable` | moins de 3 dates retenues, couverture commune sous le minimum, politique SCL inadaptée, saisons différentes si le profil l'exige, durée supérieure à l'intervalle maximal du profil |
| `not_assessed` | aucun calendrier saisonnier dans le profil (cas des profils V1) |
| `exploration` | conditions précédentes satisfaites |

Les « dates manquantes » affichées regroupent : dates exclues (motif), lacunes
supérieures au seuil entre dates retenues ou aux bornes de la période, et
acquisitions locales de la période non sélectionnées. Une lacune ne signifie pas
qu'aucune acquisition n'existe chez Copernicus : utiliser le catalogue.

## Export CSV

En-tête `# clé: valeur` (provenance) puis une ligne par date. Lecture :
`pandas.read_csv(path, comment="#")`. Provenance : profil, version et empreinte,
SHA-256 de l'AOI, période, politique SCL, grille, couverture commune, statut, date
de calcul. Par date : identifiant produit, date d'acquisition UTC, saison, inclusion
et motif, statistiques propres et communes, calibration BOA, SHA-256 des quatre
actifs du manifeste. Aucun chemin local n'est écrit.

## Ressources et caches

Mémoire bornée : une seule date en mémoire à la fois. Les masques de validité sont
compactés à 1 bit par cellule (`np.packbits`) et les NDVI temporaires relus lors
d'une seconde passe, au lieu de conserver N tableaux float32. Les poids de surface
AOI sont calculés une fois par grille (cache existant). Les fichiers temporaires B04
et B08 découpés sont supprimés dès le NDVI écrit ; tout est effacé en fin d'appel.

Cache Streamlit : `st.cache_resource(max_entries=8)` sur le résultat (statistiques
uniquement, sans tableau raster). La clé inclut identité, taille, dates de
modification et SHA-256 des manifestes de chaque date, SHA-256 de l'AOI, empreinte
du profil, période et seuil de lacune. Sur un défaut de cache, la vérification
SHA-256 complète des actifs est refaite.

Mesures du 22/10/2026 (poste local, 3 dates réelles Akanda, grille 1201 × 1201) :
moteur seul 4,9 s, +103 Mio de RSS (base 121 Mio) ; page complète sur serveur froid,
du clic à l'affichage des graphiques, 8,2 s (vérification SHA-256 comprise). Le coût
croît linéairement avec le nombre de dates ; ces chiffres ne sont pas une garantie.

## Résultat réel Akanda (janvier 2025)

| Date | Couverture valide | Retenue | NDVI moyen (date) | NDVI moyen (communs) |
|---|---:|---|---:|---:|
| 15/01 | 66,18 % | oui | 0,4993 | 0,4991 |
| 20/01 | 2,69 % | non (< 5 %) | 0,2833 | — |
| 25/01 | 63,78 % | oui | 0,4641 | 0,4663 |

Pixels communs : 827 865 (57,398 %), identiques à la paire 15/25 de référence ; la
différence des moyennes communes (−0,0328) égale le ΔNDVI moyen de la paire. Statut
`not_comparable` : deux dates exploitables seulement. Le 20/01 est presque
entièrement nuageux.

## Limites

- Pas de modèle de tendance (pente, test statistique) : volontairement absent.
- Pas de correction phénologique, d'humidité, de marée ni de saturation.
- Toutes les dates sont recalculées à chaque nouvelle combinaison (pas de cache par date).
- Grilles de CRS différents : alignement par la règle de la première date ; non testé
  sur données réelles multi-tuiles.
