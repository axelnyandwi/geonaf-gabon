"""Local Streamlit V0.1; scientific processing lives in services."""

import streamlit as st
from PIL import Image

from src.config import APP_TITLE, PROJECT_ROOT, TAGLINE
from src.components.data_source import active_data_dir
from src.components.analysis_settings import TERRITORY_LABELS, choose_territory
from src.components.pair_analysis import show_pair_analysis
from src.components.territorial_indicators import show_key_figures
from src.components.s2_vegetation import show_s2_vegetation
from src.components.s2_transitions import show_s2_transitions
from src.components.temporal_guide import show_temporal_guide
from src.components.errors import page_guard, show_error, show_internal_error
from src.services.local_preview import LocalPreviewError


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
    except LocalPreviewError as error:
        show_error(error, context="accueil.analyse")
        st.stop()
    except Exception as error:
        show_internal_error(error, context="accueil.analyse",
                            message="Affichage indisponible. Vérifier la configuration locale du prototype.")
        st.stop()
    show_pair_analysis(DATA_DIR, aoi_id)
