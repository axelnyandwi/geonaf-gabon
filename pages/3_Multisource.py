"""Multisource demonstrator page; scientific processing lives in services."""

import streamlit as st

from src.config import PROJECT_ROOT
from src.components.data_source import active_data_dir
from src.components.errors import page_guard
from src.components.multisource import show_multisource

st.set_page_config(page_title="GEONAF · Multisource",
                   page_icon=str(PROJECT_ROOT / "assets" / "geonaf_favicon.png"), layout="wide")
st.page_link("app.py", label="Accueil")
with page_guard("multisource"):
    show_multisource(active_data_dir())
