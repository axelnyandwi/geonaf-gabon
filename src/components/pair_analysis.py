"""Detailed NDVI variation between two dates (pair analysis), moved out of the entrypoint.

Unchanged computation and display; ``return`` replaces ``st.stop()`` so that a missing
prerequisite ends this section only, not the sections rendered after it. Map and image
helpers are looked up on their modules at call time, as when this code ran in app.py.
"""

import streamlit as st

from src.components.analysis_settings import choose_analysis
from src.components.errors import show_error, show_internal_error
from src.components.formatting import (
    format_decimal, format_hectares_from_km2, format_pixels, format_ndvi, format_surface, format_percent,
)
from src.components import interactive_map, preview_cache, temporal_map
from src.components.observation_picker import pick_observations, observation_quality_notices
from src.components.resource_panel import assess_aoi_file, show_assessment
from src.components.temporal_guide import VARIATION_TITLE
from src.services import local_preview
from src.services.local_preview import LocalPreviewError
from src.services.observation_selection import observation_label

SEASONAL_STATUS = {"not_assessed": "non évaluée", "not_comparable": "non comparable",
                   "conditional": "comparable sous conditions"}
SEASON_RELATION = {"unknown": "saisons inconnues", "different": "saisons différentes", "same": "même saison"}


def show_pair_analysis(data_dir, aoi_id: str, *, context: str = "accueil.analyse") -> None:
    try:
        st.header(VARIATION_TITLE)
        st.caption("Écart du NDVI entre deux dates choisies, pixel par pixel, sur la surface observée aux deux "
                   "dates ; cartes, surfaces en baisse ou en hausse et statistiques.")
        configuration = choose_analysis(aoi_id, "analysis")
        if configuration is None:
            return
        profile = configuration.effective
        observations = local_preview.list_local_observations(data_dir)
        if len(observations) < 2:
            st.error("Deux acquisitions locales vérifiées au minimum sont nécessaires.")
            return
        earlier, later, submitted, ambiguous = pick_observations(observations)
        selected = (earlier, later)
        previous_selection = st.session_state.get('selected_observations')
        if not submitted and ((previous_selection is not None and previous_selection != selected)
                              or (previous_selection is None and ambiguous)):
            st.info('Sélection modifiée : cliquer sur « Afficher les dates sélectionnées » pour appliquer ces acquisitions.')
            return
        key = local_preview.local_preview_key(earlier, later, data_dir, scientific_profile=profile)
        if not show_assessment(assess_aoi_file(data_dir / "aoi" / f"{aoi_id}.geojson"), operation="Analyse de la paire"):
            return
        # Shared by all sessions, bounded in bytes, read-only arrays; the session keeps only the key.
        with st.spinner("Vérification des fichiers locaux et calcul du NDVI calibré…"):
            result = preview_cache.shared_pair_analysis(key, earlier, later, data_dir, profile)
        st.session_state["pair"] = key
        st.session_state['selected_observations'] = selected
    except LocalPreviewError as error:
        show_error(error, context=context)
        return
    except Exception as error:
        show_internal_error(error, context=context,
                            message="Affichage indisponible. Vérifier la configuration locale du prototype.")
        return

    analysis = result.analysis
    stats = analysis.statistics
    common_km2 = analysis.comparable_area_m2 / 1e6
    t = result.threshold
    # One summary line: what is compared, on which surface, with which threshold.
    st.markdown(f"**Dates comparées** : {earlier.acquired_at:%d/%m/%Y %H:%M} UTC → {later.acquired_at:%d/%m/%Y %H:%M} UTC"
                f"  \n**Surface observée aux deux dates** : {format_surface(common_km2)} km² "
                f"({format_percent(analysis.comparable_coverage_percent)} % de la zone d'étude)"
                f"  \n**Seuil de variation ΔNDVI** : ±{format_decimal(t, 2)} (profil {profile.label})")
    status_labels = {"exploration": "Exploration", "not_assessed": "Comparabilité non évaluée",
                     "not_comparable": "Non comparable", "validated": "Résultat validé"}
    st.warning(f"Statut scientifique : {status_labels[result.interpretation.status.value]}. "
               "Les cartes et classes restent exploratoires ; aucun changement durable validé dans la V1.")
    # Restrictions and seasonal warnings overlap: each distinct note is shown once.
    for note in dict.fromkeys((*result.interpretation.restrictions, *result.seasonal_assessment.warnings)):
        st.caption(note)
    for notice in observation_quality_notices(result):
        st.warning(notice)
    temporal = st.checkbox("Comparaison temporelle interactive", value=True, key="temporal_comparison")
    interactive = temporal or st.checkbox("Carte NDVI interactive (date 2)", value=True, key="interactive_map")
    map_ready = False
    map_error = None
    if interactive:
        try:
            if temporal:
                layer = temporal_map.cached_temporal_payload(key, result, max_size=1536)
                map_ready = temporal_map.show_temporal_map(layer)
            else:
                layer = interactive_map.cached_ndvi_layer(("map-v1", key, 1536), result)
                map_ready = interactive_map.show_interactive_map(layer)
        except Exception as error:
            map_ready = False
            map_error = error
        if map_error is not None:
            show_internal_error(map_error, context="accueil.carte", level="warning",
                                message="Carte interactive indisponible ; affichage des cartes Matplotlib.")
        elif not map_ready:
            st.warning("Carte interactive indisponible ; affichage des cartes Matplotlib.")
    if not interactive or not map_ready:
        st.image(preview_cache.cached_map_png(("png-v1", key), result),
                 caption="Cartes NDVI des deux dates et variation ΔNDVI sur la surface observée aux deux dates.")
    if map_ready and temporal:
        st.caption("Comparaison interactive : les NDVI conservent le masque propre à chaque date ; "
                   "classes et ΔNDVI utilisent uniquement les observations comparables aux deux dates. "
                   "La grille d’affichage est reprojetée ; les indicateurs restent ceux de la grille scientifique native.")
    elif map_ready:
        st.caption("Carte interactive : NDVI de la date 2. Gris : observation masquée "
                   "(nuages, ombres, eau, invalides) ; fond sombre : extérieur de la zone d'étude. "
                   "La grille d’affichage est reprojetée ; les statistiques restent celles de la grille scientifique native.")
    else:
        st.caption("Les cartes NDVI montrent les pixels exploitables de chaque date. "
                   "Le delta utilise uniquement leur intersection valide aux deux dates. "
                   "Le gris inclut les pixels masqués (nuages, ombres, eau, invalides) ; il ne signifie pas NDVI nul.")

    columns = st.columns(4)
    columns[0].metric("Surface observée aux deux dates", f"{format_surface(common_km2)} km²",
                      help=f"Soit {format_hectares_from_km2(common_km2)} ha ; seule surface sur laquelle porte l'analyse.")
    columns[1].metric("Part de la zone d'étude observée", f"{format_percent(analysis.comparable_coverage_percent)} %")
    columns[2].metric("Part non comparable", f"{format_percent(100 - analysis.comparable_coverage_percent)} %",
                      help="Nuages, ombres ou pixels exclus à l'une des deux dates : non estimée.")
    columns[3].metric("Variation moyenne du NDVI", format_ndvi(stats["mean"]),
                      help="Moyenne de ΔNDVI (date 2 − date 1) des pixels comparables, chaque pixel comptant pour un. "
                           "La section « Transitions » affiche une moyenne pondérée par la surface de chaque pixel.")
    st.table({"Statistique ΔNDVI (pixels comparables)": ["Nombre de pixels", "Médiane", "Écart-type", "Minimum",
                                                         "Maximum"],
              "Valeur": [format_pixels(stats["count"])] +
                        [format_ndvi(stats[name]) for name in ("median", "std", "min", "max")]})
    st.table({"Variation": [f"Baisse : Δ < −{format_ndvi(t)}", f"Sous le seuil : −{format_ndvi(t)} ≤ Δ ≤ +{format_ndvi(t)}",
                            f"Hausse : Δ > +{format_ndvi(t)}"],
              "Surface observée (km²)": [format_surface(analysis.areas_ha[name] / 100)
                                         for name in ("decrease", "stable", "increase")],
              "Surface observée (ha)": [format_surface(analysis.areas_ha[name]) for name in ("decrease", "stable", "increase")]})
    st.caption("Surfaces pondérées par l’intersection exacte avec la zone d'étude ; statistiques descriptives par pixel. "
               "Les seuils délimitent des variations d’indice, pas une significativité statistique.")
    st.warning("Une variation du NDVI ne démontre pas une déforestation. "
               "Nuages résiduels, saisonnalité, humidité, marées et saturation du NDVI peuvent contribuer au signal. "
               "Les zones observables ne représentent pas nécessairement toute la zone d'étude. Validation indépendante non réalisée.")
    with st.expander("Détails techniques de l'analyse"):
        seasonal = result.seasonal_assessment
        st.caption(f"Acquisitions : {observation_label(earlier)} → {observation_label(later)}.")
        st.caption(f"Masque des nuages et ombres (politique SCL) : {result.scl_policy}. "
                   f"Grille : {result.common_grid.crs}, résolution {format_decimal(result.common_grid.resolution[0], 0)} m. "
                   "Réflectance de surface (BOA) calibrée à partir des métadonnées XML de chaque produit.")
        st.caption(f"Profil {profile.profile_id} · {profile.version} · empreinte {profile.fingerprint}. "
                   f"{profile.region} · {profile.climate} · {profile.ecosystem}. Usage : {profile.usage}.")
        st.caption(f"Saisonnalité : {SEASONAL_STATUS.get(seasonal.status, seasonal.status)} "
                   f"({SEASON_RELATION.get(seasonal.season_relation, seasonal.season_relation)}).")
        st.caption("Prototype local V0.2 · Sentinel-2 L2A. Données locales uniquement ; aucun téléchargement. "
                   "Les dates affichées sont celles des acquisitions, pas celles de l’ingestion. Les modifications "
                   "des fichiers invalident le cache. Les images sont des aperçus ; les calculs conservent la "
                   "résolution native.")
