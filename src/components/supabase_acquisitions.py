"""Acquisitions recorded in Supabase geonaf-test, read over HTTPS on explicit request only (D5).

Reusable panel: ``show_supabase_acquisitions(key_prefix=...)``. Nothing is requested when a page loads; the
HTTPS call happens on the button click only, and its result stays in the visitor's session (never the token,
which is read on the server and sent only in the request's Authorization header).
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from src.components.errors import show_error
from src.data.edge_acquisitions import MAX_RECORDS, EdgeAcquisitionsError
from src.services import supabase_acquisitions as service

TITLE = "Acquisitions enregistrées dans Supabase geonaf-test"
SCOPE = ("Métadonnées de manifestes d'ingestion Sentinel-2, lues en HTTPS depuis l'environnement de test "
         "Supabase. Une acquisition n'est ni une preuve de couverture effective de la zone (nuages et pixels "
         "valides non évalués ici), ni un résultat d'indicateur : aucun changement territorial ne peut en être "
         "déduit.")


def _secrets() -> dict:
    """The two keys from Streamlit secrets, if any; a missing or unreadable secrets file means none."""
    try:
        return {key: st.secrets[key] for key in service.KEYS if key in st.secrets}
    except Exception:  # No secrets.toml (local default) or unreadable file: never shown, treated as absent.
        return {}


def show_supabase_acquisitions(*, key_prefix: str = "supabase_acquisitions") -> None:
    st.subheader(TITLE)
    st.caption(SCOPE)
    state_key = f"{key_prefix}_result"
    try:
        config = service.resolve_config(_secrets(), os.environ)
    except EdgeAcquisitionsError as error:
        st.session_state.pop(state_key, None)
        show_error(error, context="supabase_acquisitions.configuration", level="warning")
        return
    if config is None:
        st.session_state.pop(state_key, None)
        st.info("Consultation Supabase non configurée sur ce serveur : les données locales restent la seule source.")
        return
    if st.button(f"Consulter {service.SOURCE_LABEL}", key=f"{key_prefix}_consult"):
        st.session_state.pop(state_key, None)          # Never keep an older result next to a failed request.
        try:
            with st.spinner(f"Lecture HTTPS de {service.SOURCE_LABEL}…"):
                st.session_state[state_key] = service.consult(config)
        except EdgeAcquisitionsError as error:
            show_error(error, context="supabase_acquisitions.consultation")
            return
    consultation = st.session_state.get(state_key)
    if consultation is None:
        st.caption("Aucune requête n'est envoyée tant que la consultation n'est pas demandée.")
        return
    count = len(consultation.acquisitions)
    st.caption(f"Source : {service.SOURCE_LABEL} (environnement de test, fonction Edge geonaf-acquisitions) · "
               f"consulté le {consultation.consulted_at_utc.strftime('%Y-%m-%d %H:%M')} UTC · "
               f"{count} acquisition(s), {MAX_RECORDS} au plus par consultation.")
    if not count:
        st.info(f"Aucune acquisition enregistrée dans {service.SOURCE_LABEL}.")
        return
    st.dataframe(pd.DataFrame(service.display_rows(consultation)), hide_index=True, width="stretch")
    if consultation.truncated:
        st.warning(f"Limite de {MAX_RECORDS} acquisitions atteinte : la liste peut être incomplète.")
    st.caption("Dates d'acquisition (prise de vue) et d'ingestion (récupération par GEONAF) distinctes ; ancienneté "
               "comptée depuis l'acquisition, à la date de consultation. Grille = grille raster de la bande rouge, "
               "pas l'emprise des pixels valides. Ces lignes ne sont pas rattachées à une zone d'étude ni à une "
               "collectivité.")
