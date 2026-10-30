"""Territory selection and boundary comparison page; logic lives in services."""

import streamlit as st

from src.config import PROJECT_ROOT
from src.components.data_source import active_data_dir
from src.components.errors import page_guard
from src.components.territory_explorer import show_territories

st.set_page_config(page_title="GEONAF · Territoires",
                   page_icon=str(PROJECT_ROOT / "assets" / "geonaf_favicon.png"), layout="wide")
st.page_link("app.py", label="Accueil")
with page_guard("territoires"):
    show_territories(active_data_dir())
