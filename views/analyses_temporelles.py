"""Temporal analyses: four sections; only the selected section runs (no hidden computation).

Each section imports its component when it runs: nothing is loaded for a hidden section.
"""

import streamlit as st

from src.components.analysis_settings import TERRITORY_LABELS, choose_shared_territory
from src.components.data_source import active_data_dir
from src.components.errors import page_guard

SECTIONS = {"comparer": "Comparer deux dates", "variation": "Variation NDVI", "serie": "Série temporelle",
            "donnees": "Données et acquisitions"}
SECTION_KEY = "section"   # Also the URL query parameter: /analyses-temporelles?section=serie.

with page_guard("analyses_temporelles"):
    st.title("Analyses temporelles")
    st.caption("Évolutions observées par Sentinel-2 sur la zone d'étude : comparaison de deux dates, variation "
               "continue du NDVI, série temporelle, et données disponibles. Résultats exploratoires, non validés.")
    data_dir = active_data_dir()  # Local data, else the verified demonstration pack.
    section = st.segmented_control("Section", list(SECTIONS), format_func=SECTIONS.get, key=SECTION_KEY,
                                   default="comparer", required=True, bind="query-params", persist_state="session")
    if section == "donnees":  # Catalogue search covers every AOI file: no study-zone selector here.
        with page_guard("analyses_temporelles.donnees"):
            from src.components.acquisition_catalogue import show_acquisition_catalogue

            show_acquisition_catalogue(data_dir, embedded=True)
    else:
        aoi_id = choose_shared_territory(data_dir)
        if aoi_id is not None:
            st.info(f"Zone d'étude {TERRITORY_LABELS[aoi_id]} : carré d’acquisition de 12 × 12 km autour de "
                    f"{TERRITORY_LABELS[aoi_id]}. Ce carré n’est pas une limite administrative.")
            # Each section has its own guard: an error stays inside the section.
            with page_guard(f"analyses_temporelles.{section}"):
                if section == "comparer":
                    from src.components.s2_transitions import show_s2_transitions
                    from src.components.temporal_guide import show_temporal_guide

                    st.header("Comparer deux dates")
                    show_temporal_guide(aoi_id)
                    show_s2_transitions(data_dir, aoi_id)
                elif section == "variation":
                    from src.components.pair_analysis import show_pair_analysis

                    show_pair_analysis(data_dir, aoi_id, context="analyses_temporelles.variation")
                else:
                    from src.components.ndvi_series import show_ndvi_series

                    show_ndvi_series(data_dir, aoi_id, embedded=True)
