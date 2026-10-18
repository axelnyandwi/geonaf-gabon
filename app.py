"""Local Streamlit V0.1; scientific processing lives in services."""

import streamlit as st
from PIL import Image

from src.config import APP_TITLE, DATA_DIR, PROJECT_ROOT, TAGLINE
from src.components.preview_cache import cached_map_png
from src.components.formatting import format_pixels, format_ndvi, format_surface, format_percent
from src.services.local_preview import (
    LocalPreviewError, analyze_local_akanda, list_local_observations, local_preview_key,
)

def _brand_asset(name):
    try:
        with Image.open(PROJECT_ROOT / "assets" / name) as source:
            return source.copy()
    except OSError:
        return None


logo = _brand_asset("geonaf_logo_dark.png")
favicon = _brand_asset("geonaf_favicon.png")
st.set_page_config(page_title=APP_TITLE, page_icon=favicon if favicon is not None else "🌍", layout="wide")
if logo is not None:
    st.markdown("<style>.st-key-branding {background: #0e1117; border-radius: 8px; padding: 12px;}</style>",
                unsafe_allow_html=True)
    with st.container(key="branding"):
        st.image(logo, width=720)
else:
    st.title(APP_TITLE)
    st.markdown(TAGLINE)
st.caption("Prototype local V0.1 · Sentinel-2 L2A · Akanda")
st.info("AOI : carré d’acquisition de 12 × 12 km autour d’Akanda. "
        "Ce carré n’est pas la limite administrative d’Akanda.")

try:
    observations = list_local_observations(DATA_DIR)
    if {item.acquired_at.date().isoformat() for item in observations} != {"2025-01-15", "2025-01-25"}:
        st.error("Deux acquisitions locales vérifiées sont nécessaires (15 et 25 janvier 2025).")
        st.stop()
    label = lambda item: item.acquired_at.strftime("%d/%m/%Y · %H:%M:%S UTC")
    with st.form("dates"):
        left, right = st.columns(2)
        earlier = left.selectbox("Date 1", observations, index=0, format_func=label)
        later = right.selectbox("Date 2", observations, index=len(observations) - 1, format_func=label)
        st.form_submit_button("Afficher les dates sélectionnées")
    key = local_preview_key(earlier, later, DATA_DIR)
    if st.session_state.get("pair") != key:
        with st.spinner("Vérification des fichiers locaux et calcul du NDVI calibré…"):
            result = analyze_local_akanda(earlier, later, DATA_DIR)
        st.session_state["result"] = result
        st.session_state["pair"] = key
    result = st.session_state["result"]
except LocalPreviewError as error:
    st.error(str(error))
    st.stop()
except Exception:
    st.error("Affichage indisponible. Vérifier la configuration locale du prototype.")
    st.stop()

st.caption(f"Acquisitions : {label(earlier)} → {label(later)}. "
           f"Politique SCL : {result.scl_policy} (4 et 5 ; ombres 2 exclues). "
           f"Grille : {result.common_grid.crs}, résolution {result.common_grid.resolution[0]:g} m. "
           "Calibration BOA issue des XML de chaque produit.")
st.image(cached_map_png(("png-v1", key), result))
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
