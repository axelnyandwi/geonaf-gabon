"""NDVI time series page; scientific processing lives in services."""

import streamlit as st

from src.config import DATA_DIR, PROJECT_ROOT
from src.components.ndvi_series import show_ndvi_series

st.set_page_config(page_title="GEONAF · Série NDVI",
                   page_icon=str(PROJECT_ROOT / "assets" / "geonaf_favicon.png"), layout="wide")
st.page_link("app.py", label="Accueil")
show_ndvi_series(DATA_DIR)
