"""Metadata search page; scientific comparison remains in the existing app."""

import streamlit as st

from src.config import PROJECT_ROOT
from src.components.data_source import active_data_dir
from src.components.errors import page_guard
from src.components.acquisition_catalogue import show_acquisition_catalogue

st.set_page_config(page_title="GEONAF · Acquisitions Sentinel-2",
                   page_icon=str(PROJECT_ROOT / "assets" / "geonaf_favicon.png"), layout="wide")
st.page_link("app.py", label="Accueil")
with page_guard("acquisitions"):
    show_acquisition_catalogue(active_data_dir())
