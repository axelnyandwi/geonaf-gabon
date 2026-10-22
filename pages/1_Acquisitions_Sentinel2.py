"""Metadata search page; scientific comparison remains in the existing app."""

import streamlit as st

from src.config import DATA_DIR, PROJECT_ROOT
from src.components.acquisition_catalogue import show_acquisition_catalogue

st.set_page_config(page_title="GEONAF · Acquisitions Sentinel-2",
                   page_icon=str(PROJECT_ROOT / "assets" / "geonaf_favicon.png"), layout="wide")
st.page_link("app.py", label="Accueil")
show_acquisition_catalogue(DATA_DIR)
