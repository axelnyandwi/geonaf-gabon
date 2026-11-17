"""Start-up check of a public consultation (GEONAF_PUBLIC): a misconfigured public process serves no page.

The rules are in :func:`src.demo_mode.public_configuration_problems` (no Streamlit). This adapter passes it
the environment and the top-level keys of the Streamlit secrets; secret values are never read. The page and
the server log name categories only (credentials, database, ...), never a variable name or a value.
"""

import logging
import os

import streamlit as st

from src.demo_mode import PublicConfigurationError, ensure_public_configuration

LOGGER = logging.getLogger("geonaf.public")


def _secret_keys() -> tuple:
    try:
        return tuple(st.secrets.keys())
    except Exception:   # No secrets file (local default) or unreadable: no key to check.
        return ()


def enforce_public_configuration() -> None:
    """Stop the run before any page when the public configuration is refused; no-op outside public mode."""
    try:
        ensure_public_configuration(os.environ, _secret_keys())
    except PublicConfigurationError as error:
        LOGGER.error("%s", error)
        st.error(f"{error} Aucune page n'est servie : la configuration du déploiement doit être corrigée.")
        st.stop()
