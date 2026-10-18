# Analyse temporelle Sentinel-2 : saisonnalité

## Cohérence entre sélection et analyse

Les politiques SCL restent nommées et configurables. `usable_land` conserve
les classes 4 et 5 ; `land_surface` conserve les classes 2, 4 et 5. La classe
2 désigne une ombre portée sur les produits PB 05.11. Son inclusion
reste possible explicitement, mais ne convient pas au défaut quantitatif.

Une `TemporalPair` produite par le sélecteur conserve `scl_policy` ainsi que
`scl_coverage_grid`, la grille SCL recadrée à l'AOI qui a servi à mesurer
`common_coverage_percent`. Lorsqu'une telle paire est fournie au service, sa
politique est héritée. Une politique explicitement différente est refusée.
Sans paire sélectionnée, le service utilise `usable_land` ; l'appelant peut fournir une autre politique nommée.
Les paires construites manuellement ou provenant d'anciens appelants peuvent
ne pas renseigner ces métadonnées (`None`) : le service conserve alors son
défaut `usable_land`, mais la couverture de sélection reste de politique et
de grille inconnues et ne peut pas être comparée directement à l'analyse.

Les couvertures de sélection et d'analyse sont deux mesures distinctes. Le
sélecteur mesure l'intersection des masques SCL sur `scl_coverage_grid` ;
l'analyse harmonise NDVI et SCL sur l'intersection des grilles B04, en
rééchantillonnant SCL par plus proche voisin. La résolution et l'emprise
peuvent différer : ne pas interpréter `common_coverage_percent` et
`analysis.comparable_coverage_percent` comme directement équivalents, même
si leurs valeurs sont proches. Les deux excluent les pixels SCL NoData et
appliquent la politique inscrite dans le résultat de chaque étape.

Le filtre qualité `min_usable_land_percent` reste une métrique d'acquisition
calculée par `calculate_scl_quality` sur les classes fiables 4 et 5 ; il ne
change pas avec la politique de couverture commune de la paire. Les indicateurs
par pixel de l'analyse avec AOI utilisent les cellules comparables touchées
par l'AOI (`all_touched`) ; les surfaces et couvertures restent pondérées par
les intersections exactes AOI-cellule.

Lorsque le service reçoit une AOI, il découpe B04, B08 et SCL de chaque date
dans un répertoire temporaire avant le calcul NDVI et l'harmonisation. Le
découpage lit les fenêtres raster recouvrant l'AOI et ses cellules de bord
touchées ; il ne charge donc pas les tuiles complètes en mémoire. La grille de
résultat est l'emprise rectangulaire de ces fenêtres. Les statistiques par
pixel respectent ensuite le masque AOI, et les indicateurs de surface utilisent
les intersections exactes AOI-cellule. Sans AOI, le comportement historique
sur la grille complète reste inchangé. Les fichiers temporaires sont supprimés
à la fin de l'appel.

## Implémenté

- Chaque acquisition conserve son horodatage exact avec fuseau ; le résultat expose à la fois l'horodatage fourni et son équivalent UTC, sans réduire la date au jour ou au mois.
- `SeasonalContext` conserve, si disponibles, une région, un type de végétation, un identifiant de saison fourni par une source, et la version de cette source. Le contrat ne classe pas le climat ni ne déduit la saison depuis la date.
- Sans évaluateur scientifique, le service compare uniquement les identifiants explicites dans un même couple région/type de végétation. Une différence produit un avertissement intersaisonnier, mais ne rejette pas l'analyse. Des identifiants identiques restent signalés comme insuffisants pour prouver la comparabilité.
- Le delta et les classes restent des indicateurs de variation spectrale NDVI ; ils ne constituent pas une confirmation de déforestation.

## Points d'extension

`SeasonalComparabilityEvaluator` est le point d'injection pour un évaluateur configurable selon la région et le type de végétation. Une future implémentation devra s'appuyer sur une caractérisation phénologique validée pour les régions et formations végétales étudiées, documenter ses sources et versions, puis tester les périodes limites et les contextes incomplets. Un résultat `not_comparable` reste descriptif et n'interrompt pas automatiquement le calcul.

La suite du pipeline pourra comparer plusieurs acquisitions au lieu d'une seule paire, afin de distinguer un signal persistant d'une variation saisonnière ou d'une observation atypique. Sentinel-1 pourra apporter une mesure radar complémentaire moins dépendante de la couverture nuageuse, avec ses propres règles de calibration, d'alignement et d'interprétation. Ces analyses ne sont pas implémentées ici.

## À valider avant usage opérationnel

- Le vocabulaire des saisons, son calendrier et ses sources à l'échelle régionale au Gabon.
- La pertinence de ces périodes par type de végétation, y compris mangroves et mosaïques.
- Les règles de comparaison, la gestion des dates de transition, et les incertitudes associées.
- Les critères indépendants nécessaires pour confirmer une déforestation ; une baisse de NDVI seule ne suffit pas.
