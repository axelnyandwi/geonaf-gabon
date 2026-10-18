"""Streamlit-only cache for public PNG bytes, never shared mutable science objects."""

import streamlit as st

from src.components.ndvi_maps import render_ndvi_png


@st.cache_data(max_entries=4, show_spinner=False)
def cached_map_png(input_key, _result):
    # _result is intentionally not hashed: input_key covers acquisitions,
    # file revisions, AOI, SCL and threshold. Bump png-v1 when rendering changes.
    return render_ndvi_png(_result)
