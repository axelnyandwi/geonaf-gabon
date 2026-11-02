"""Dashboard: study zone, analysed perimeter map, key figures, Sentinel-2 vegetation indicators, access to analyses.

Detailed temporal analyses (transitions, NDVI variation, series, data) live in views/analyses_temporelles.py.
"""

import streamlit as st

from src.config import APP_TITLE, TAGLINE
from src.navigation import page, section_query
from src.components.data_source import active_data_dir
from src.components.analysis_settings import TERRITORY_LABELS, choose_shared_territory
from src.components.territorial_indicators import show_key_figures
from src.components.s2_vegetation import show_s2_vegetation
from src.components.zone_map import show_zone_map
from src.components.errors import page_guard, show_error, show_internal_error
from src.services.local_preview import LocalPreviewError

# (page, label, section, help) of the access links at the bottom of the dashboard.
ANALYSES = (
    ("analyses_temporelles", "Comparer deux dates", "comparer",
     "Transitions entre classes NDVI sur la surface observée aux deux dates."),
    ("analyses_temporelles", "Variation NDVI", "variation",
     "Écart du NDVI entre deux dates, cartes, surfaces et statistiques ; paramètres avancés."),
    ("analyses_temporelles", "Série temporelle NDVI", "serie", "Suivre le NDVI sur plusieurs dates de la zone d'étude."),
    ("analyses_temporelles", "Catalogue des acquisitions Sentinel-2", "donnees",
     "Rechercher des images Sentinel-2 et les télécharger sur commande explicite."),
    ("multisource", "Démonstrateur multisource", None,
     "Croiser Sentinel-2, WorldCover, OpenStreetMap et Sentinel-1 (descriptif)."),
    ("territoires", "Territoires et frontières", None, "Comparer les référentiels de limites ou importer une zone."),
)

with page_guard("tableau_de_bord"):  # Unexpected errors: generic message + diagnostic identifier.
    st.title("Tableau de bord")
    st.markdown(TAGLINE)
    st.caption(f"{APP_TITLE} : synthèse de la zone d'étude. Les analyses détaillées sont accessibles en bas de page "
               "et dans la navigation.")
    DATA_DIR = active_data_dir()  # Local data, else the verified demonstration pack.
    try:
        aoi_id = choose_shared_territory(DATA_DIR)
        if aoi_id is None:
            st.stop()
        st.info(f"Zone d'étude {TERRITORY_LABELS[aoi_id]} : carré d’acquisition de 12 × 12 km autour de "
                f"{TERRITORY_LABELS[aoi_id]}. Ce carré n’est pas une limite administrative.")
        show_zone_map(DATA_DIR, aoi_id)
        show_key_figures(DATA_DIR, aoi_id, TERRITORY_LABELS[aoi_id])
        show_s2_vegetation(DATA_DIR, aoi_id)
    except LocalPreviewError as error:
        show_error(error, context="tableau_de_bord.analyse")
        st.stop()
    except Exception as error:
        show_internal_error(error, context="tableau_de_bord.analyse",
                            message="Affichage indisponible. Vérifier la configuration locale du prototype.")
        st.stop()
    st.divider()
    st.header("Analyses détaillées")
    columns = st.columns(2)
    for index, (name, label, section, help_text) in enumerate(ANALYSES):
        columns[index % 2].page_link(page(name), label=label, help=help_text,
                                     query_params=section_query(section) if section else None)
