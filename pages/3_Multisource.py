"""Multisource demonstrator page; scientific processing lives in services."""

import streamlit as st

from src.config import DATA_DIR, PROJECT_ROOT
from src.components.multisource import show_multisource

st.set_page_config(page_title="GEONAF · Multisource",
                   page_icon=str(PROJECT_ROOT / "assets" / "geonaf_favicon.png"), layout="wide")
st.page_link("app.py", label="Accueil")
show_multisource(DATA_DIR)
