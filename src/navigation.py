"""Pages of the st.navigation router (app.py), defined once for the router and for page links.

Links use ``st.Page`` objects, matched by their script hash rather than by comparing file paths
(path comparison depends on how the project directory is spelled on disk). Routes:
docs/navigation.md.
"""

import streamlit as st

# name: (script relative to app.py, title, url_path); the first page is the default one ("/").
PAGES = {
    "accueil": ("views/accueil.py", "Accueil", None),
    "acquisitions": ("views/acquisitions_sentinel2.py", "Acquisitions Sentinel-2", "Acquisitions_Sentinel2"),
    "serie_ndvi": ("views/serie_ndvi.py", "Série NDVI", "Series_NDVI"),
    "multisource": ("views/multisource.py", "Multisource", "Multisource"),
    "territoires": ("views/territoires.py", "Territoires", "Territoires"),
}
DEFAULT = next(iter(PAGES))


def page(name: str):
    path, title, url_path = PAGES[name]
    if name == DEFAULT:
        return st.Page(path, title=title, default=True)
    return st.Page(path, title=title, url_path=url_path)


def all_pages() -> list:
    return [page(name) for name in PAGES]
