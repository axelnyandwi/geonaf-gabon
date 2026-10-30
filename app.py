"""Local Streamlit V0.1; scientific processing lives in services."""

import streamlit as st
from PIL import Image

from src.config import APP_TITLE, PROJECT_ROOT, TAGLINE
from src.components.data_source import active_data_dir
from src.components.preview_cache import cached_map_png, shared_pair_analysis
from src.components.interactive_map import cached_ndvi_layer, show_interactive_map
from src.components.temporal_map import cached_temporal_payload, show_temporal_map
from src.components.observation_picker import pick_observations, observation_quality_notices
from src.components.formatting import (
    format_decimal, format_hectares_from_km2, format_pixels, format_ndvi, format_surface, format_percent,
)
from src.services.local_preview import (
    LocalPreviewError, list_local_observations, local_preview_key,
)
from src.services.observation_selection import observation_label
from src.components.analysis_settings import TERRITORY_LABELS, choose_analysis, choose_territory
from src.components.resource_panel import assess_aoi_file, show_assessment
from src.components.territorial_indicators import show_key_figures
from src.components.s2_vegetation import show_s2_vegetation
from src.components.s2_transitions import show_s2_transitions
from src.components.temporal_guide import VARIATION_TITLE, show_temporal_guide
from src.components.errors import page_guard, show_error, show_internal_error

SEASONAL_STATUS = {"not_assessed": "non évaluée", "not_comparable": "non comparable",
                   "conditional": "comparable sous conditions"}
SEASON_RELATION = {"unknown": "saisons inconnues", "different": "saisons différentes", "same": "même saison"}


def _brand_asset(name):
    try:
        with Image.open(PROJECT_ROOT / "assets" / name) as source:
            return source.copy()
    except OSError:
        return None


favicon = _brand_asset("geonaf_favicon.png")
st.set_page_config(page_title=APP_TITLE, page_icon=favicon if favicon is not None else "🌍", layout="wide")
with page_guard("accueil"):  # Unexpected errors: generic message + diagnostic identifier.
    st.title(APP_TITLE)
    st.markdown(TAGLINE)
    st.page_link("pages/1_Acquisitions_Sentinel2.py", label="Catalogue des acquisitions Sentinel-2",
                 help="Rechercher des images Sentinel-2 et les télécharger sur commande explicite.")
    st.page_link("pages/2_Series_NDVI.py", label="Série temporelle NDVI",
                 help="Suivre le NDVI sur plusieurs dates de la zone d'étude.")
    st.page_link("pages/3_Multisource.py", label="Démonstrateur multisource",
                 help="Croiser Sentinel-2, WorldCover, OpenStreetMap et Sentinel-1 (descriptif).")
    st.page_link("pages/4_Territoires.py", label="Territoires et frontières",
                 help="Comparer les référentiels de limites ou importer une zone.")
    DATA_DIR = active_data_dir()  # Local data, else the verified demonstration pack.

    try:
        aoi_id = choose_territory(DATA_DIR, "territory")
        if aoi_id is None:
            st.stop()
        st.info(f"Zone d'étude {TERRITORY_LABELS[aoi_id]} : carré d’acquisition de 12 × 12 km autour de "
                f"{TERRITORY_LABELS[aoi_id]}. Ce carré n’est pas une limite administrative.")
        show_key_figures(DATA_DIR, aoi_id, TERRITORY_LABELS[aoi_id])
        show_s2_vegetation(DATA_DIR, aoi_id)
        st.divider()
        st.header("Comparer deux dates")
        show_temporal_guide(aoi_id)
        show_s2_transitions(DATA_DIR, aoi_id)
        st.divider()
        st.header(VARIATION_TITLE)
        st.caption("Écart du NDVI entre deux dates choisies, pixel par pixel, sur la surface observée aux deux "
                   "dates ; cartes, surfaces en baisse ou en hausse et statistiques.")
        configuration = choose_analysis(aoi_id, "analysis")
        if configuration is None:
            st.stop()
        profile = configuration.effective
        observations = list_local_observations(DATA_DIR)
        if len(observations) < 2:
            st.error("Deux acquisitions locales vérifiées au minimum sont nécessaires.")
            st.stop()
        earlier, later, submitted, ambiguous = pick_observations(observations)
        selected = (earlier, later)
        previous_selection = st.session_state.get('selected_observations')
        if not submitted and ((previous_selection is not None and previous_selection != selected)
                              or (previous_selection is None and ambiguous)):
            st.info('Sélection modifiée : cliquer sur « Afficher les dates sélectionnées » pour appliquer ces acquisitions.')
            st.stop()
        key = local_preview_key(earlier, later, DATA_DIR, scientific_profile=profile)
        if not show_assessment(assess_aoi_file(DATA_DIR / "aoi" / f"{aoi_id}.geojson"), operation="Analyse de la paire"):
            st.stop()
        # Shared by all sessions, bounded in bytes, read-only arrays; the session keeps only the key.
        with st.spinner("Vérification des fichiers locaux et calcul du NDVI calibré…"):
            result = shared_pair_analysis(key, earlier, later, DATA_DIR, profile)
        st.session_state["pair"] = key
        st.session_state['selected_observations'] = selected
    except LocalPreviewError as error:
        show_error(error, context="accueil.analyse")
        st.stop()
    except Exception as error:
        show_internal_error(error, context="accueil.analyse",
                            message="Affichage indisponible. Vérifier la configuration locale du prototype.")
        st.stop()

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
                layer = cached_temporal_payload(key, result, max_size=1536)
                map_ready = show_temporal_map(layer)
            else:
                layer = cached_ndvi_layer(("map-v1", key, 1536), result)
                map_ready = show_interactive_map(layer)
        except Exception as error:
            map_ready = False
            map_error = error
        if map_error is not None:
            show_internal_error(map_error, context="accueil.carte", level="warning",
                                message="Carte interactive indisponible ; affichage des cartes Matplotlib.")
        elif not map_ready:
            st.warning("Carte interactive indisponible ; affichage des cartes Matplotlib.")
    if not interactive or not map_ready:
        st.image(cached_map_png(("png-v1", key), result),
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
