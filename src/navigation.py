"""Pages of the st.navigation router (app.py), defined once for the router and for page links.

Links use ``st.Page`` objects, matched by their script hash rather than by comparing file paths
(path comparison depends on how the project directory is spelled on disk). Routes:
docs/navigation.md.
"""

import streamlit as st

# name: (script relative to app.py, title, url_path); the first page is the default one ("/").
PAGES = {
    "tableau_de_bord": ("views/tableau_de_bord.py", "Tableau de bord", None),
    "analyses_temporelles": ("views/analyses_temporelles.py", "Analyses temporelles", "analyses-temporelles"),
    "analyses_multisources": ("views/analyses_multisources.py", "Analyses multisources", "analyses-multisources"),
    "territoires": ("views/territoires.py", "Territoires", "territoires"),
}
DEFAULT = next(iter(PAGES))

# Sections of the temporal analyses page. The URL query parameter « section » holds the label, as the
# widget binding stores the displayed option: /analyses-temporelles?section=S%C3%A9rie+temporelle.
SECTION_KEY = "section"
SECTIONS = {"comparer": "Comparer deux dates", "variation": "Variation NDVI", "serie": "Série temporelle",
            "donnees": "Données et acquisitions"}


def section_query(name: str) -> dict:
    """Query parameters of a link that opens one section of the temporal analyses page."""
    return {SECTION_KEY: SECTIONS[name]}


def page(name: str):
    path, title, url_path = PAGES[name]
    if name == DEFAULT:
        return st.Page(path, title=title, default=True)
    return st.Page(path, title=title, url_path=url_path)


def all_pages() -> list:
    return [page(name) for name in PAGES]
