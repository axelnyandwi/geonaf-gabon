"""Data directory of the running app: local data, else the verified demonstration pack."""

from pathlib import Path

import streamlit as st

import src.config as config
from src.components.errors import show_error
from src.data.demo_pack import DemoPackError, default_cache_root, pack_summary, resolve_data_dir, spec_from_environment


@st.cache_resource(show_spinner="Préparation des données de démonstration (premier chargement)…")
def _resolve(configured: str, spec):
    # One resolution per server process, shared by all sessions. Failures are not
    # cached: the next rerun retries. After a restart, the on-disk install is
    # re-verified and downloaded again only if it is missing or invalid.
    return resolve_data_dir(Path(configured), spec=spec, cache_root=default_cache_root())


def active_data_dir() -> Path:
    """Return the data directory; never shows URLs, paths or exception details."""
    configured = config.DATA_DIR  # Read at call time: honours the effective configuration.
    try:
        source = _resolve(str(configured), spec_from_environment())
    except DemoPackError as error:
        show_error(error, context="donnees.demonstration", level="warning",
                   prefix="Données de démonstration indisponibles : ", suffix=" Réessayer en rechargeant la page.")
        return configured
    if source.origin == "demo_pack" and source.pack is not None:
        lines = pack_summary(source.pack)
        st.caption(f"Données de démonstration : {lines[0]}")
        with st.expander("Provenance et limites des données"):
            for line in lines[1:]:
                st.caption(line)
    return source.path
