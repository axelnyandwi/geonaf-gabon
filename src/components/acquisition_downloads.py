"""Explicit commands and polling; workers never call Streamlit."""

from pathlib import Path

import streamlit as st

from src.components.errors import page_guard, public_text, show_error
from src.data.download_control import DownloadBudget, TransferAborted
from src.services.download_queue import DownloadQueue


@st.cache_resource
def download_queue(data_dir):
    return DownloadQueue(Path(data_dir))


@st.fragment(run_every=1.0)
def show_download_status(queue):
    # A fragment rerun runs alone, outside the page guard: it needs its own.
    with page_guard("telechargements.suivi"):
        _show_download_status(queue)


def _show_download_status(queue):
    snapshot = queue.snapshot()
    was_busy = st.session_state.get("downloads_was_busy", False)
    st.session_state["downloads_was_busy"] = snapshot["busy"]
    if was_busy and not snapshot["busy"]:
        # Commands outside this fragment were rendered disabled; refresh them once.
        st.rerun(scope="app")
    if snapshot["error"]:
        st.warning(public_text(snapshot["error"]))
    rows = snapshot["rows"]
    if not rows:
        return
    st.subheader("File de téléchargements")
    finished = sum(row["state"] in ("completed", "failed", "cancelled", "skipped") for row in rows)
    st.progress(finished / len(rows), text=f"{finished}/{len(rows)} acquisitions terminées · {snapshot['phase']}")
    st.caption(f"Volume reçu, tentatives incluses : {snapshot['network_bytes'] / 1024**2:.2f} Mio. "
               "completed = intégrité et calibration validées ; skipped = produit local revérifié.")
    st.dataframe([{"Identifiant STAC": row["id"], "État": row["state"],
                   "Reçu (Mio)": round(row["bytes"] / 1024**2, 2),
                   "Volume estimé (Mio)": round(row["expected_bytes"] / 1024**2, 2),
                   "Progression": ("Validé" if row["state"] in ("completed", "skipped") else
                                   f"{min(99, int(100 * row['bytes'] / max(1, row['expected_bytes'])))} %"),
                   "Erreur": row["error"], "Durée (s)": round(row["seconds"], 2)} for row in rows],
                 hide_index=True, width="stretch")
    if snapshot["busy"]:
        if st.button("Annuler les téléchargements", key="downloads_cancel"):
            queue.cancel()
            st.info("Annulation demandée ; arrêt au prochain contrôle de bloc ou délai réseau.")
    elif snapshot["phase"] == "ready" and any(row["state"] == "pending" for row in rows):
        if st.button("Télécharger les acquisitions estimées", key="downloads_start"):
            try:
                queue.start()
                st.rerun()
            except TransferAborted as error:
                show_error(error, context="telechargements.lancement")
    elif snapshot["phase"] == "finished":
        st.caption("Actualiser l’état local du catalogue pour voir les produits disponibles. "
                   "Pour reprendre : conserver la sélection, réestimer, puis lancer explicitement.")


def show_download_commands(selection, data_dir):
    if not selection.records:
        return
    queue = download_queue(str(Path(data_dir).resolve()))
    with st.expander("Téléchargements sécurisés"):
        st.caption("Une estimation ne télécharge pas de bandes. Seul le bouton de lancement transfère les actifs. "
                   "Les budgets portent sur le lot ; les fichiers existants ne sont pas écrasés.")
        columns = st.columns(3)
        storage = columns[0].number_input("Budget stockage temporaire (Mio)", min_value=64, max_value=16384,
                                          value=512, key="downloads_storage")
        network = columns[1].number_input("Budget réseau (Mio)", min_value=64, max_value=16384,
                                          value=320, key="downloads_network")
        workers = columns[2].selectbox("Transferts simultanés", (1, 2), key="downloads_workers")
        memory = st.number_input("Budget buffers de transfert (Mio)", min_value=32, max_value=256,
                                 value=64, key="downloads_memory")
        reserve = st.number_input("Réserve disque libre (Mio)", min_value=64, max_value=16384,
                                  value=512, key="downloads_reserve")
        if st.button("Estimer les téléchargements sélectionnés", disabled=queue.busy, key="downloads_estimate"):
            try:
                queue.prepare(selection, DownloadBudget(storage * 1024**2, network * 1024**2,
                                                        memory * 1024**2, reserve * 1024**2, workers))
                st.rerun()
            except (TransferAborted, ValueError) as error:
                show_error(error, context="telechargements.estimation")
