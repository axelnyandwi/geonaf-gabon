"""Temporal analyses: four sections; only the selected section runs (no hidden computation).

Each section imports its component when it runs: nothing is loaded for a hidden section.
"""

import streamlit as st

from src.components.analysis_settings import TERRITORY_LABELS, choose_shared_territory
from src.components.data_source import active_data_dir
from src.components.errors import page_guard
from src.navigation import SECTION_KEY, SECTIONS


with page_guard("analyses_temporelles"):
    st.title("Analyses temporelles")
    st.caption("Évolutions observées par Sentinel-2 sur la zone d'étude : comparaison de deux dates, variation "
               "continue du NDVI, série temporelle, et données disponibles. Résultats exploratoires, non validés.")
    data_dir = active_data_dir()  # Local data, else the verified demonstration pack.
    choice = st.segmented_control("Section", list(SECTIONS.values()), key=SECTION_KEY, default=SECTIONS["comparer"],
                                  required=True, bind="query-params", persist_state="session")
    section = next(name for name, label in SECTIONS.items() if label == choice)
    if section == "donnees":  # Catalogue search covers every AOI file: no study-zone selector here.
        with page_guard("analyses_temporelles.donnees"):
            from src.components.acquisition_catalogue import show_acquisition_catalogue

            show_acquisition_catalogue(data_dir, embedded=True)
        # Separate guard: a Supabase failure never hides the local catalogue. No request until asked.
        st.divider()
        with page_guard("analyses_temporelles.supabase"):
            from src.components.supabase_acquisitions import show_supabase_acquisitions

            show_supabase_acquisitions(key_prefix="donnees_supabase")
    else:
        aoi_id = choose_shared_territory(data_dir)
        if aoi_id is not None:
            from src.components.public_notice import show_data_notice

            # Zone status, Sentinel-2 dates, attribution and limits (same wording as the CSV export).
            show_data_notice(data_dir, aoi_id, TERRITORY_LABELS[aoi_id])
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
