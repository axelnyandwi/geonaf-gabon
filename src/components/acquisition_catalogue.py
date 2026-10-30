"""Streamlit catalogue adapter. Search is explicit; selections are metadata only."""

from datetime import datetime, timedelta, timezone

import streamlit as st

from src.components.errors import show_error
from src.data.catalogue import CatalogueError, CatalogueQuery
from src.geo.scientific_profile import LOCAL_SCIENTIFIC_PROFILES
from src.services.acquisition_catalogue import (
    available_catalogue_aois, load_catalogue_aoi, search_catalogue,
    refresh_local_states, select_acquisitions, catalogue_rows,
)


def show_acquisition_catalogue(data_dir):
    st.title("Catalogue des acquisitions Sentinel-2")
    st.caption("Recherche de métadonnées Copernicus L2A · Téléchargement uniquement sur commande explicite. "
               "La couverture nuageuse globale ne mesure pas la qualité locale de l’AOI.")
    from src.components.acquisition_downloads import download_queue, show_download_status, show_download_commands
    show_download_status(download_queue(str(data_dir.resolve())))
    aois = available_catalogue_aois(data_dir)
    if not aois:
        st.info("Aucune AOI GeoJSON disponible dans le répertoire de données configuré.")
        return
    today = datetime.now(timezone.utc).date()
    aoi_id = st.selectbox("AOI de recherche", aois, index=aois.index("akanda") if "akanda" in aois else 0,
                          key="catalogue_aoi")
    columns = st.columns(2)
    start = columns[0].date_input("Début de période (UTC)", value=today - timedelta(days=30), key="catalogue_start")
    end = columns[1].date_input("Fin de période (UTC)", value=today, key="catalogue_end")
    limit_clouds = st.checkbox("Filtrer la couverture nuageuse globale", key="catalogue_cloud_filter")
    cloud = st.number_input("Maximum nuageux global (%)", min_value=0.0, max_value=100.0, value=100.0,
                            disabled=not limit_clouds, key="catalogue_cloud_max")
    profiles = tuple(profile for profile in LOCAL_SCIENTIFIC_PROFILES if profile.aoi_id == aoi_id)
    profile = st.radio("Profil scientifique de référence", profiles, format_func=lambda item: item.label,
                       key="catalogue_profile") if profiles else None
    st.caption("Le profil documente le contexte ; cette sélection ne calcule ni ne valide un changement territorial.")
    submitted = st.button("Rechercher dans Copernicus", key="catalogue_search")
    try:
        aoi = load_catalogue_aoi(data_dir, aoi_id)
        identity = (profile.profile_id, profile.version, profile.fingerprint) if profile else None
        query = CatalogueQuery(aoi, start, end, cloud if limit_clouds else None, identity)
    except (CatalogueError, ValueError):
        st.session_state.pop("catalogue_selection", None)
        st.session_state.pop("catalogue_result", None)
        st.error("AOI ou période invalide ; respecter l’ordre chronologique et vérifier les données locales.")
        return
    result = st.session_state.get("catalogue_result")
    if submitted:
        # Never keep a stale selection visible after a failed/new search.
        st.session_state.pop("catalogue_result", None)
        st.session_state.pop("catalogue_selection", None)
        st.session_state.pop("catalogue_items_" + query.fingerprint, None)
        try:
            with st.spinner("Lecture de toutes les pages du catalogue Copernicus…"):
                result = search_catalogue(query, data_dir)
            st.session_state["catalogue_result"] = result
        except CatalogueError as error:
            show_error(error, context="catalogue.recherche")
            return
    if result is None or result.query.fingerprint != query.fingerprint:
        st.session_state.pop("catalogue_selection", None)
        st.info("Choisir l’AOI et la période, puis lancer explicitement la recherche.")
        return
    if st.button("Actualiser l’état local", key="catalogue_refresh_local"):
        try:
            result = refresh_local_states(result, data_dir)
        except CatalogueError as error:
            st.session_state.pop("catalogue_selection", None)
            show_error(error, context="catalogue.etat_local")
            return
        st.session_state["catalogue_result"] = result
    st.caption(f"{len(result.records)} acquisitions uniques · recherche UTC : "
               f"{result.searched_at_utc.isoformat()} · identifiant de recherche : {query.fingerprint}")
    if not result.complete:
        st.warning("Recherche partielle : toutes les acquisitions n’ont pas été obtenues.")
    if not result.records:
        st.info("Aucune acquisition trouvée pour cette AOI et cette période.")
        return
    st.dataframe(catalogue_rows(result), hide_index=True, width="stretch")
    by_id = {row.stac_id: row for row in result.records}
    selection_key = "catalogue_items_" + query.fingerprint
    if selection_key in st.session_state:
        st.session_state[selection_key] = [key for key in st.session_state[selection_key] if key in by_id]
    selected = st.multiselect(
        "Acquisitions retenues (sans téléchargement)", list(by_id), key=selection_key,
        format_func=lambda key: by_id[key].acquired_at_utc.strftime("%d/%m/%Y %H:%M:%S UTC") + " · " + key,
    )
    st.session_state["catalogue_selection"] = select_acquisitions(result, selected)
    st.caption(f"{len(selected)} acquisition(s) retenue(s). Les bandes sont signalées selon les liens STAC, "
               "sans vérification de leur contenu. Les empreintes locales seront revérifiées avant analyse.")
    show_download_commands(st.session_state["catalogue_selection"], data_dir)
