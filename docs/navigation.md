# Navigation V1 : quatre pages (02/11/2026)

Réorganisation de l'interface Streamlit, sans changement de calcul, de seuil, de profil ni du paquet
v3. Les valeurs scientifiques affichées sont identiques avant et après (comparaison E0, ci-dessous).

## Pages

`app.py` est un routeur : `st.navigation` (barre latérale) sert quatre pages définies une seule fois
dans `src/navigation.py`, et fixe une seule configuration de page (onglet « GEONAF · <page> », mise en
page large, icône). Le contenu des pages est dans `views/` ; il n'y a plus de dossier `pages/`.

| Page | Fichier | Contenu |
|---|---|---|
| Tableau de bord | `views/tableau_de_bord.py` | Zone d'étude, carte du périmètre analysé, chiffres clés, végétation observée par Sentinel-2, liens vers les analyses détaillées |
| Analyses temporelles | `views/analyses_temporelles.py` | Quatre sections, une seule exécutée à la fois (ci-dessous) |
| Analyses multisources | `views/analyses_multisources.py` | Démonstrateur multisource inchangé (croisements, couches externes, carte) |
| Territoires | `views/territoires.py` | Explorateur inchangé : sélection, comparaison des référentiels, import GeoJSON |

### Sections des analyses temporelles

Sélecteur `st.segmented_control` (obligatoire, lié au paramètre d'URL `section`, conservé pour la
session). Seule la section affichée est exécutée et seul son composant est importé : aucun calcul
pour une section masquée. Chaque section a son propre garde d'erreur (`page_guard`) : une erreur reste
dans sa section.

| Section | Contenu (composants inchangés) |
|---|---|
| Comparer deux dates | Guide « Trois lectures différentes », transitions entre classes NDVI (`s2_transitions`) |
| Variation NDVI | Analyse de paire (`pair_analysis`, ancien corps de `app.py`) : profil et paramètres avancés, dates, cartes NDVI et temporelle, métriques, tableaux, détails techniques |
| Série temporelle | Série NDVI (`ndvi_series`) |
| Données et acquisitions | Catalogue Sentinel-2 et téléchargements (`acquisition_catalogue`) ; refusés en mode lecture seule |

### Zone d'étude partagée

`choose_shared_territory` (clé `geonaf_zone`, `persist_state="session"`) : la même zone est utilisée par
le tableau de bord, les analyses temporelles et les analyses multisources, et conservée d'une page à
l'autre. La page Territoires garde son propre explorateur ; la recherche du catalogue garde son choix
d'AOI.

### Carte du tableau de bord

`src/components/zone_map.py` réutilise la carte de l'explorateur (`territory_catalog.zone_context_payload`) :
trait plein pour la zone d'acquisition (carré de travail de 12 × 12 km, pas une limite
administrative) ; tirets pour les limites des unités administratives qui la recoupent, premier
référentiel local disponible (gbOpen ADM2, sinon gbHumanitarian ADM2), découpées autour de la zone.
Une frontière commune à deux unités est tracée une fois. Géométries du registre local uniquement,
sans réseau ; aucune surface, part ni statistique par unité.

## Routes

| Avant (multipage automatique) | Après (V1) |
|---|---|
| `/` (accueil) | `/` (Tableau de bord) |
| `/` sections « Comparer deux dates » et « Variation continue du NDVI » | `/analyses-temporelles?section=Comparer+deux+dates`, `?section=Variation+NDVI` |
| `/Series_NDVI` | `/analyses-temporelles?section=S%C3%A9rie+temporelle` |
| `/Acquisitions_Sentinel2` | `/analyses-temporelles?section=Donn%C3%A9es+et+acquisitions` |
| `/Multisource` | `/analyses-multisources` |
| `/Territoires` | `/territoires` |

Les anciennes routes ne sont plus servies (décision D6). Le paramètre `section` contient le libellé
affiché : la liaison d'URL de `st.segmented_control` enregistre l'option affichée.

## Tests et vérifications

- `tests/test_navigation_v1.py` : une seule section exécutée, section par défaut, erreur confinée à sa
  section, section et zone conservées après un changement de page, liens du tableau de bord vers les
  sections, zone partagée par le multisource, configuration de page unique.
- `tests/test_zone_map.py` : contenu de la carte du périmètre.
- `tests/streamlit_pages.py` : ouverture d'une section comme un visiteur (tableau de bord d'abord).
- `tests/test_demo_read_only.py` : lecture seule sur le tableau de bord, les quatre sections et les
  deux autres pages.
- `scripts/ui_reference.py` : valeurs affichées sur le paquet v3, comparées à la référence
  `docs/benchmarks/ui_reference_e0.json` (avant réorganisation) par groupe. Exemple :
  `python scripts/ui_reference.py --layout v1 --pack <paquet v3 décompressé> --output v1.json` puis
  `python scripts/ui_reference.py --compare docs/benchmarks/ui_reference_e0.json v1.json`.

`AppTest` ne transmet pas les paramètres d'URL aux widgets liés et n'affiche pas les composants
MapLibre : liens directs et cartes sont vérifiés dans un navigateur (Playwright, 1 280 px et 390 px).

## Contextes d'erreur (journal `geonaf.ui`)

| Avant | Après |
|---|---|
| `accueil`, `accueil.analyse` | `tableau_de_bord`, `tableau_de_bord.analyse`, `tableau_de_bord.carte` |
| `accueil.analyse` (analyse de paire) | `analyses_temporelles.variation` |
| - (section des transitions de l'accueil) | `analyses_temporelles.comparer` |
| `series_ndvi`, `acquisitions` | `analyses_temporelles.serie`, `analyses_temporelles.donnees` |
| `multisource` | `analyses_multisources` (les contextes internes `multisource.*` sont inchangés) |
