"""Local Streamlit V0.1; scientific processing lives in services."""

import streamlit as st
from PIL import Image

from src.config import APP_TITLE, DATA_DIR, PROJECT_ROOT, TAGLINE
from src.components.preview_cache import cached_map_png
from src.components.interactive_map import cached_ndvi_layer, show_interactive_map
from src.components.temporal_map import cached_temporal_payload, show_temporal_map
from src.components.observation_picker import pick_observations, observation_quality_notices
from src.components.formatting import format_pixels, format_ndvi, format_surface, format_percent
from src.services.local_preview import (
    LocalPreviewError, analyze_local_akanda, list_local_observations, local_preview_key,
)
from src.services.observation_selection import observation_label
from src.geo.scientific_profile import LOCAL_SCIENTIFIC_PROFILES

def _brand_asset(name):
    try:
        with Image.open(PROJECT_ROOT / "assets" / name) as source:
            return source.copy()
    except OSError:
        return None


favicon = _brand_asset("geonaf_favicon.png")
st.set_page_config(page_title=APP_TITLE, page_icon=favicon if favicon is not None else "🌍", layout="wide")
st.title(APP_TITLE)
st.markdown(TAGLINE)
st.page_link("pages/1_Acquisitions_Sentinel2.py", label="Catalogue des acquisitions Sentinel-2")
st.page_link("pages/2_Series_NDVI.py", label="Série temporelle NDVI")
st.page_link("pages/3_Multisource.py", label="Démonstrateur multisource")

try:
    with st.expander("Profil scientifique · V1 expérimentale"):
        profile = st.radio("Profil appliqué", LOCAL_SCIENTIFIC_PROFILES,
                           format_func=lambda item: item.label, key="scientific_profile")
        st.caption(f"{profile.region} · {profile.climate} · {profile.ecosystem}. "
                   f"Usage : {profile.usage}. Version : {profile.version}. "
                   f"Seuil ΔNDVI : {profile.change_threshold:g} ; couverture minimale : "
                   f"{profile.min_comparable_coverage_percent:g} %. {profile.rationale}")
    st.caption(f"Prototype local V0.2 · Sentinel-2 L2A · AOI {profile.aoi_id}")
    if profile.aoi_id == "akanda":
        st.info("AOI : carré d’acquisition de 12 × 12 km autour d’Akanda. "
                "Ce carré n’est pas la limite administrative d’Akanda.")
    else:
        st.info(f"AOI configurée : {profile.aoi_id}. Vérifier son périmètre et l’applicabilité du profil.")
    observations = list_local_observations(DATA_DIR)
    if len(observations) < 2:
        st.error("Deux acquisitions locales vérifiées au minimum sont nécessaires.")
        st.stop()
    earlier, later, submitted, ambiguous = pick_observations(observations)
    label = lambda item: item.acquired_at.strftime("%d/%m/%Y · %H:%M:%S UTC")
    selected = (earlier, later)
    previous_selection = st.session_state.get('selected_observations')
    if not submitted and ((previous_selection is not None and previous_selection != selected)
                          or (previous_selection is None and ambiguous)):
        st.info('Sélection modifiée : cliquer sur « Afficher les dates sélectionnées » pour appliquer ces acquisitions.')
        st.stop()
    key = local_preview_key(earlier, later, DATA_DIR, scientific_profile=profile)
    if st.session_state.get("pair") != key:
        with st.spinner("Vérification des fichiers locaux et calcul du NDVI calibré…"):
            result = analyze_local_akanda(earlier, later, DATA_DIR, scientific_profile=profile)
        st.session_state["result"] = result
        st.session_state["pair"] = key
    st.session_state['selected_observations'] = selected
    result = st.session_state["result"]
except LocalPreviewError as error:
    st.error(str(error))
    st.stop()
except Exception as exc:
    st.error("Affichage indisponible. Vérifier la configuration locale du prototype.")
    st.stop()

status_labels = {"exploration": "Exploration", "not_assessed": "Comparabilité non évaluée",
                 "not_comparable": "Non comparable", "validated": "Résultat validé"}
st.warning(f"Statut scientifique : {status_labels[result.interpretation.status.value]}. "
           "Les cartes et classes restent exploratoires ; aucun changement durable validé dans la V1.")
for restriction in result.interpretation.restrictions:
    st.caption(restriction)
st.caption(f"Profil {profile.profile_id} · {profile.version} · empreinte {profile.fingerprint}. "
           f"Saisonnalité : {result.seasonal_assessment.status} ({result.seasonal_assessment.season_relation}).")

st.caption(f"Acquisitions : {observation_label(earlier)} → {observation_label(later)}. "
           f"Politique SCL : {result.scl_policy}. "
           f"Grille : {result.common_grid.crs}, résolution {result.common_grid.resolution[0]:g} m. "
           "Calibration BOA issue des XML de chaque produit.")
for notice in observation_quality_notices(result):
    st.warning(notice)
temporal = st.checkbox("Comparaison temporelle interactive", value=True, key="temporal_comparison")
interactive = temporal or st.checkbox("Carte NDVI interactive (date 2)", value=True, key="interactive_map")
map_ready = False
if interactive:
    try:
        if temporal:
            layer = cached_temporal_payload(key, result, max_size=1536)
            map_ready = show_temporal_map(layer)
        else:
            layer = cached_ndvi_layer(("map-v1", key, 1536), result)
            map_ready = show_interactive_map(layer)
    except Exception:
        map_ready = False
    if not map_ready:
        st.warning("Carte interactive indisponible ; affichage des cartes Matplotlib.")
if not interactive or not map_ready:
    st.image(cached_map_png(("png-v1", key), result))
if map_ready and temporal:
    st.caption("Comparaison interactive : les NDVI conservent le masque propre à chaque date ; "
               "classes et ΔNDVI utilisent uniquement les observations comparables aux deux dates. "
               "La grille d’affichage est reprojetée ; les indicateurs restent ceux de la grille scientifique native.")
elif map_ready:
    st.caption("Carte interactive : NDVI de la date 2. Gris : observation masquée "
               "(nuages, ombres, eau, invalides) ; fond sombre : extérieur de l’AOI. "
               "La grille d’affichage est reprojetée ; les statistiques restent celles de la grille scientifique native.")
else:
    st.caption("Les cartes NDVI montrent les pixels exploitables de chaque date. "
               "Le delta utilise uniquement leur intersection valide aux deux dates. "
               "Le gris inclut les pixels masqués (nuages, ombres, eau, invalides) ; il ne signifie pas NDVI nul.")

analysis = result.analysis
stats = analysis.statistics
columns = st.columns(4)
columns[0].metric("Couverture comparable / AOI", f"{format_percent(analysis.comparable_coverage_percent)} %")
columns[1].metric("Surface comparable", f"{format_surface(analysis.comparable_area_m2 / 10_000)} ha")
columns[2].metric("AOI non comparable", f"{format_percent(100 - analysis.comparable_coverage_percent)} %")
columns[3].metric("ΔNDVI moyen", format_ndvi(stats["mean"]))
st.table({"Statistique ΔNDVI (pixels comparables)": ["Nombre", "Médiane", "Écart-type", "Minimum", "Maximum"],
          "Valeur": [format_pixels(stats["count"])] +
                    [format_ndvi(stats[name]) for name in ("median", "std", "min", "max")]})
t = result.threshold
st.table({"Variation": [f"Baisse : Δ < −{format_ndvi(t)}", f"Sous le seuil : −{format_ndvi(t)} ≤ Δ ≤ +{format_ndvi(t)}", f"Hausse : Δ > +{format_ndvi(t)}"],
          "Surface observée (ha)": [format_surface(analysis.areas_ha[name]) for name in ("decrease", "stable", "increase")]})
st.caption("Surfaces pondérées par l’intersection exacte avec l’AOI ; statistiques descriptives par pixel. "
           "Les seuils délimitent des variations d’indice, pas une significativité statistique.")
st.warning("Une variation du NDVI ne démontre pas une déforestation. "
           "Nuages résiduels, saisonnalité, humidité, marées et saturation du NDVI peuvent contribuer au signal. "
           "Les zones observables ne représentent pas nécessairement toute l’AOI. Validation indépendante non réalisée.")
for warning in result.seasonal_assessment.warnings:
    st.caption(warning)
st.caption("Données locales uniquement ; aucun téléchargement. Les dates affichées sont celles des acquisitions, "
           "pas celles de l’ingestion. Le résultat reste en session ; les modifications des fichiers invalident le cache. "
           "Les images sont des aperçus ; les calculs conservent la résolution native.")
