# Analyse temporelle Sentinel-2 : saisonnalité

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
