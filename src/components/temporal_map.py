"""Streamlit adapter for the standalone temporal renderer and bounded cache."""

from pathlib import Path

import streamlit as st

from src.services.temporal_map import TemporalMapCache


@st.cache_resource(show_spinner=False)
def _payload_cache():
    # Only immutable public payload bytes are shared, never scientific arrays.
    return TemporalMapCache(max_entries=4, max_bytes=64*1024**2)


def cached_temporal_payload(input_key, result, *, max_size=1536):
    """Reuse the scientific revision and explicitly forward the display size."""
    return _payload_cache().prepare(result, scientific_key=input_key, max_size=max_size)


@st.cache_resource(show_spinner=False)
def _renderer():
    from streamlit.components.v2 import component

    folder = Path(__file__).parent/'maplibre'
    vendor = (folder/'vendor/maplibre-gl.js').read_text(encoding='utf-8')
    module = (folder/'temporal.js').read_text(encoding='utf-8')
    css = (folder/'vendor/maplibre-gl.css').read_text(encoding='utf-8')+'\n'+(folder/'temporal.css').read_text(encoding='utf-8')
    adapter = """
export default function(component) {
  const {parentElement, data, setStateValue} = component;
  let view;
  try {
    view = mountTemporalMap(parentElement, data, maplibregl,
      {onError: () => setStateValue('error', true)});
    parentElement.__geonafTemporalView = view;
  } catch {
    setStateValue('error', true);
  }
  return () => {
    view?.dispose();
    delete parentElement.__geonafTemporalView;
  };
}
"""
    return component('geonaf_temporal_map', js=vendor+'\n'+module+'\n'+adapter, css=css)


def show_temporal_map(payload):
    """Only rendering errors return to Python; navigation has no state bridge."""
    if payload.get('schema_version') != 'geonaf.temporal-map/v1':
        raise ValueError('Contrat temporel invalide.')
    result = _renderer()(data=payload, key='geonaf_temporal_'+payload['result_id'],
                         height='content', default={'error': False},
                         on_error_change=lambda: None)
    return not result.error
