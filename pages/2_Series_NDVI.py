"""NDVI time series page; scientific processing lives in services."""

import streamlit as st

from src.config import PROJECT_ROOT
from src.components.data_source import active_data_dir
from src.components.errors import page_guard
from src.components.ndvi_series import show_ndvi_series

st.set_page_config(page_title="GEONAF · Série NDVI",
                   page_icon=str(PROJECT_ROOT / "assets" / "geonaf_favicon.png"), layout="wide")
st.page_link("app.py", label="Accueil")
with page_guard("series_ndvi"):
    show_ndvi_series(active_data_dir())
