"""GEONAF entrypoint: Streamlit navigation (st.navigation); pages: src/navigation.py."""

import streamlit as st
from PIL import Image

from src.components.public_access import enforce_public_configuration
from src.config import APP_TITLE, PROJECT_ROOT
from src.navigation import all_pages


def _brand_asset(name):
    try:
        with Image.open(PROJECT_ROOT / "assets" / name) as source:
            return source.copy()
    except OSError:
        return None


favicon = _brand_asset("geonaf_favicon.png")
current = st.navigation(all_pages(), position="sidebar")
st.set_page_config(page_title=f"{APP_TITLE} · {current.title}", page_icon=favicon if favicon is not None else "🌍",
                   layout="wide")
enforce_public_configuration()   # GEONAF_PUBLIC: refuse credentials, database access or writes before any page.
current.run()
