# Accueil : structure, libellés et accessibilité (C5)

Consolidation de l'interface du 30/10/2026, sans changement de calcul, de seuil, de profil ni du
paquet v3. Constats mesurés sur le paquet v3 (Akanda) avec `AppTest` et un navigateur (390 px et
1 280 px, thèmes clair et sombre).

## Structure de l'accueil

| Titre | Contenu |
|---|---|
| H2 Chiffres clés du territoire | Surfaces (WorldCover 2021), voies et bâtiments (OSM), chacun avec nature, source, date, périmètre et fiabilité. La ligne « Évolutions temporelles » est visible sans dépliage : aucune évolution sans deux référentiels comparables. |
| H3 Végétation observée par Sentinel-2 | Surface à NDVI ≥ 0,4 d'une acquisition, sur la surface observée. |
| H2 Comparer deux dates | Guide « Trois lectures différentes » (transitions de classes NDVI, variation continue du NDVI, couverture du sol WorldCover), seuils lus dans les profils. |
| H3 Transitions entre classes NDVI (surface commune) | Ancienne « Comparaison temporelle sur surface commune ». |
| H2 Variation continue du NDVI (analyse détaillée) | Ancienne « Analyses scientifiques » : récapitulatif (dates comparées, surface observée aux deux dates en km² et en %, seuil ΔNDVI), cartes, métriques, tableaux ; identifiants, empreintes, grille et saisonnalité dans « Détails techniques de l'analyse ». |

Les modes « Analyse standard » et « Analyse avancée » sont inchangés.

## Libellés

- « AOI » remplacé par « zone d'étude » dans les textes visibles de l'accueil et du multisource.
- Surface commune en km² (hectares en info-bulle), comme dans la section des transitions.
- « Variation moyenne du NDVI » (moyenne par pixel) distinguée, en info-bulle, de la moyenne pondérée
  par la surface affichée dans les transitions.
- Restrictions et avertissements saisonniers affichés une seule fois ; états saisonniers traduits
  (`not_assessed` → « non évaluée »).
- Garde-fou de ressources autorisé : une ligne (« calcul autorisé »), détail en info-bulle ; les
  avertissements, verdicts inconnus et refus restent affichés en entier.
- Nombres et dates au format français : résumé du paquet (« 15/01/2025 », « 66,73 % »), seuils du
  profil (« 0,10 »), radar (« −7,3 dB »), légende de la carte temporelle (« ±0,10 »).
- Multisource : « Bâtiments OSM touchant la zone » explique l'écart avec les chiffres clés (objets qui
  recoupent la zone, contre rattachement par point représentatif).

## Accessibilité

- Contraste des badges (thème clair, `.streamlit/config.toml` `[theme.light]`) : orange 3,2:1 → 5,5:1,
  gris 3,5:1 → 6,0:1, vert 4,5:1 → 5,6:1 (WCAG AA : 4,5:1 pour 14 px). Thème sombre : aucun texte sous
  le seuil, couleurs inchangées.
- Hiérarchie des titres continue : H1 → H2 → H3 (la section des transitions était un H4 sous un H3).
- Image de repli des cartes NDVI avec légende textuelle.
- 390 px : aucun débordement horizontal ; les tableaux défilent dans leur cadre.

## Limites restantes

- `lang="en"` sur la page HTML : Streamlit ne permet pas de le régler ; les lecteurs d'écran peuvent
  lire le français avec une voix anglaise.
- En-têtes des tableaux `st.table` : 3,7:1 (style par défaut de Streamlit).
- Navigation latérale automatique : noms de fichiers (« app », « Series NDVI »), sans accents ; la
  corriger demande de renommer les pages ou de passer à `st.navigation`.
- Deux champs de date cachés internes à Streamlit sans nom accessible.
- Page d'accueil longue sur mobile (neuf cartes de chiffres clés avant les analyses).
