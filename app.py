"""GEONAF entrypoint: Streamlit navigation (st.navigation); pages: src/navigation.py."""

import streamlit as st
from PIL import Image

from src.config import APP_TITLE, PROJECT_ROOT
from src.navigation import all_pages


def _brand_asset(name):
    try:
        with Image.open(PROJECT_ROOT / "assets" / name) as source:
            return source.copy()
    except OSError:
        return None


favicon = _brand_asset("geonaf_favicon.png")
st.set_page_config(page_title=APP_TITLE, page_icon=favicon if favicon is not None else "🌍", layout="wide")
st.navigation(all_pages(), position="sidebar").run()
