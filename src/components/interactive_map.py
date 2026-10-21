"""Thin Streamlit v2 adapter; map.js also runs outside Streamlit."""

from pathlib import Path

import streamlit as st

from src.services.map_layer import prepare_ndvi_layer


@st.cache_data(max_entries=4, show_spinner=False)
def cached_ndvi_layer(input_key, _result):
    return prepare_ndvi_layer(_result)


@st.cache_resource(show_spinner=False)
def _renderer():
    from streamlit.components.v2 import component

    folder = Path(__file__).parent / "maplibre"
    vendor = (folder / "vendor" / "maplibre-gl.js").read_text(encoding="utf-8")
    module = (folder / "map.js").read_text(encoding="utf-8")
    css = (folder / "vendor" / "maplibre-gl.css").read_text(encoding="utf-8") + "\n" + (folder / "map.css").read_text(encoding="utf-8")
    adapter = """
export default function(component) {
  const {parentElement, data, setStateValue} = component;
  const view = mountNdviMap(parentElement, data, maplibregl, () => setStateValue('error', true));
  return () => view.dispose();
}
"""
    return component("geonaf_ndvi_map", js=vendor + "\n" + module + "\n" + adapter, css=css)


def show_interactive_map(payload):
    result = _renderer()(data=payload, key="geonaf_ndvi_map", height=680,
                         default={"error": False}, on_error_change=lambda: None)
    return not result.error
