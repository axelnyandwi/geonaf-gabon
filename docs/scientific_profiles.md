# Profils scientifiques V1

Le profil fixe un protocole d'exploration, pas une preuve de changement durable.
Le moteur reste indépendant de Streamlit. Les calculs NDVI, masques, classes et
surfaces restent dans leurs modules existants ; leur interprétation est évaluée
ensuite dans `src/services/temporal_analysis.py`.

## Configuration et identité

`ScientificProfile`, dans `src/geo/scientific_profile.py`, est immuable et contient :
identifiant/version, identifiant AOI, région, climat déclaré, écosystème, usage,
politique SCL, seuil ΔNDVI, couverture comparable minimale (0–100 % de l'AOI),
règles saisonnières, caractère expérimental et justification.

L'empreinte SHA-256 porte sur **tout le contenu**, y compris les règles et les
justifications. Changer un paramètre invalide donc le cache même si la version
n'a pas été incrémentée. Il faut néanmoins versionner les changements de protocole
pour assurer leur traçabilité humaine. Cette empreinte identifie une configuration ;
elle ne prouve pas sa validité scientifique.

Le parcours local résout `aoi_id` dans `data/aoi/<aoi_id>.geojson` (ou le répertoire
de données déjà configuré). Les traversées et liens sortant du répertoire AOI sont
refusés. Un appel direct au moteur fournit `aoi_path` : l'appelant est responsable
de son association à `aoi_id`. L'empreinte du contenu AOI figure dans le résultat.
Les actifs restent vérifiés par les manifestes et SHA-256 existants.

Deux scénarios sont proposés dans Streamlit :

| Profil | Seuil ΔNDVI | Couverture minimale | Portée |
|---|---:|---:|---|
| Gabon équatorial / Akanda | 0,10 | 5 % | Reprise des paramètres opérationnels du prototype |
| Akanda / sensibilité | 0,15 | 50 % | Scénario de sensibilité uniquement |

**Aucune de ces valeurs n'est présentée comme un seuil validé par écosystème.**
Le climat équatorial est un contexte déclaré ; aucun classement climatique ni
modèle de phénologie n'est inféré. Aucun calendrier gabonais validé n'est fourni.
Humidité, marées, saturation du NDVI et nuages résiduels ne sont pas corrigés.

Un autre protocole se configure sans modifier le moteur, par exemple :

```python
from dataclasses import replace
from src.geo.scientific_profile import GABON_EQUATORIAL_EXPERIMENTAL

profile = replace(
    GABON_EQUATORIAL_EXPERIMENTAL,
    profile_id="owendo-study", version="1.0.0-experimental", aoi_id="owendo",
    region="Gabon/Estuaire", climate="contexte à documenter",
    ecosystem="écosystème étudié à documenter", usage="exploration",
)
```

Ajouter les profils souhaités à `LOCAL_SCIENTIFIC_PROFILES` pour les proposer dans
l'interface. Il n'y a ni chargement de code utilisateur, ni gestionnaire de profils
distants, ni modification des acquisitions. `analyze_local_akanda` conserve son nom
pour compatibilité et accepte désormais le paramètre `scientific_profile`.

## Règles saisonnières

`SeasonalRules` contient : exigence de même saison, calendrier mensuel optionnel,
source/version du calendrier et intervalle maximal optionnel en jours réels UTC.
Un calendrier comporte exactement douze étiquettes et exige une source/version.
Les limites mensuelles sont déclaratives : elles ne représentent pas des limites
phénologiques mesurées. Aucun calendrier n'est construit à partir du seul climat.

Sans calendrier, les `SeasonalContext` explicitement fournis sont conservés. Leur
région et écosystème doivent correspondre au profil, et leurs sources/versions
doivent correspondre entre dates. Sinon la saisonnalité est non évaluée. Un
calendrier contredisant un contexte fourni provoque une erreur explicite.

Deux saisons différentes sont non comparables si la règle exige la même saison.
Une limite d'intervalle dépassée entraîne aussi ce statut. Une saison identique,
ou un passage intersaisonnier explicitement autorisé, donne seulement une
évaluation **conditionnelle**. La même étiquette ne garantit ni les mêmes pluies,
ni le même état hydrique, ni la comparabilité phénologique.

## Statuts d'interprétation

| Statut | Conditions V1 |
|---|---|
| `not_comparable` — non comparable | Politique SCL inadaptée, aucune surface comparable, couverture AOI sous le minimum, ou règle saisonnière non satisfaite |
| `not_assessed` — comparabilité non évaluée | Pas d'évaluation saisonnière exploitable et aucun rejet précédent |
| `exploration` | Conditions précédentes satisfaites ; interprétation toujours exploratoire |
| `validated` — résultat validé | Réservé, **jamais produit par cette V1** |

Le statut « résultat validé » nécessiterait une validation indépendante, bornée au
protocole, aux données, à l'AOI et à la persistance du phénomène. Ce mécanisme
n'existe pas ici : ni une saison similaire, ni un profil déclaré non expérimental,
ni un évaluateur externe déclarant les saisons comparables ne suffisent.

Les cartes et classes sont calculées et conservées pour tous ces statuts. Le
statut d'interprétation concerne le protocole global ; il ne remplace pas les
masques de comparabilité pixel par pixel. La couverture minimale ne modifie pas
les surfaces calculées. La déforestation ne peut pas être déduite du seul ΔNDVI.

Pour interpréter un changement terrestre, la politique SCL doit exclure les classes
défectueuses, incertaines, nuageuses et ombragées, et conserver **4 et 5**. `usable_land`
est admissible ; `observation`, `land_surface`, `vegetation` et `bare_soil` empêchent
la qualification de comparabilité du résultat, tout en permettant l'exploration.
Cela évite notamment de supprimer systématiquement les transitions végétation →
sol nu en exigeant une végétation présente aux deux dates.

## Provenance, cache et compatibilité

Le résultat conserve le profil complet, les contextes saisonniers effectivement
utilisés, l'évaluation, les restrictions et le SHA-256 de l'AOI. Le manifeste JSON
GeoTIFF reprend ces données. Les cartes publiques transmettent uniquement une
liste explicite d'identifiants, empreintes et statuts, sans chemins ni textes de
provenance arbitraires. Les cartes temporelles affichent cette provenance publique.

La clé scientifique locale inclut l'empreinte du profil, les révisions des actifs,
les manifestes, l'AOI, SCL et seuil. Les caches PNG et NDVI héritent de cette clé ;
le cache temporel inclut aussi les métadonnées scientifiques dans sa propre clé.
La version de cache locale passe à `local-science-v2-profiles`.

Sans profil, les appels existants conservent les paramètres historiques et le
contrat d'évaluateur saisonnier. Avec profil, une AOI est obligatoire et les
paramètres explicites contradictoires sont refusés. Un évaluateur externe ne peut
pas remplacer silencieusement les règles d'un profil : les deux modes sont exclusifs.

Les tests couvrent deux profils, saisons identiques/différentes, absence de contexte,
intervalle maximal, politiques SCL, couverture, provenance/export, caches et
sélection Streamlit. Ils vérifient la cohérence informatique, pas la validité
écologique des seuils. La non-régression Akanda compare les tableaux et indicateurs
avec et sans le profil de reprise.
