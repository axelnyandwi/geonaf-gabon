"""Dashboard map: the analysed perimeter (acquisition zone) and the administrative limits around it.

Reuses the territory explorer map and stored registry geometries; no network, no scientific
computation (no area share or statistic of the administrative units).
"""

import hashlib

import streamlit as st

from src.components.errors import show_internal_error
from src.components.formatting import format_surface
from src.services import territory_catalog as catalog

# Referential drawn around the zone, first available level: community, then humanitarian.
REFERENTIALS = (("gbOpen", "ADM2"), ("gbHumanitarian", "ADM2"))


def _context(data_dir, aoi_id):
    zone = next((option for option in catalog.historical_zones(data_dir) if option.territory_id == aoi_id), None)
    for release, level in REFERENTIALS:
        units = catalog.admin_territories(data_dir, release, level)
        if units:
            return zone, units, release, level
    return zone, (), None, None


@st.cache_data(max_entries=8, show_spinner=False)
def _payload(aoi_id, digest, _zone, _units):
    return catalog.zone_context_payload(_zone, _units)


def _digest(zone, units) -> str:
    parts = [hashlib.sha256(zone.geometry_path.read_bytes()).hexdigest()]
    parts += [unit.record.fingerprint for unit in units]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def show_zone_map(data_dir, aoi_id: str) -> None:
    from src.components.territory_explorer import show_map

    st.header("Périmètre analysé")
    try:
        zone, units, release, level = _context(data_dir, aoi_id)
        if zone is None:
            st.info("Contour de la zone d'étude absent localement : carte non affichée.")
            return
        payload, overlapping = _payload(aoi_id, _digest(zone, units), zone, units)
    except Exception as error:
        show_internal_error(error, context="tableau_de_bord.carte", level="warning",
                            message="Carte du périmètre indisponible ; les chiffres restent valides.")
        return
    st.caption(f"Périmètre analysé : {zone.label}, {format_surface(zone.area_km2)} km². Toutes les analyses de "
               "GEONAF portent sur ce carré de travail ; ce n'est ni une commune ni une unité administrative.")
    show_map(payload, f"dashboard_{aoi_id}")
    if overlapping:
        st.caption(f"Unités administratives recoupant la zone ({catalog.REFERENTIALS[release]}, "
                   f"{catalog.LEVELS[level]}) : {', '.join(overlapping)}. Repères seulement : limites non "
                   "officielles, aucune statistique n'est calculée par unité. Comparaison des référentiels : "
                   "page Territoires.")
    else:
        st.caption("Aucun référentiel administratif local : seules les limites de la zone d'acquisition sont "
                   "tracées.")
