"""Display of resource guards before a costly operation; estimates live in src.services.resource_guards."""

import hashlib
from pathlib import Path

import streamlit as st

from src.services.resource_guards import OK, REFUSED, UNKNOWN, WARNING, ResourceLimits, assess_zone

VERDICTS = {OK: "autorisé", WARNING: "avertissement", UNKNOWN: "inconnu", REFUSED: "refus"}
NOTE = ("Estimation prudente calibrée sur le poste de développement : ce n'est pas une garantie de fonctionnement "
        "sur un serveur public.")


def _number(value) -> str:
    if value is None:
        return "—"
    text = f"{value:,.2f}" if abs(value) < 10 and value != int(value) else f"{value:,.0f}"
    return text.replace(",", " ").replace(".", ",")


def verdict(assessment) -> str:
    statuses = {check.status for check in assessment.checks}
    for status in (REFUSED, WARNING, UNKNOWN):
        if status in statuses:
            return status
    return OK


@st.cache_data(max_entries=32, show_spinner=False)
def _cached_assessment(geometry_digest, _geometry, osm_wanted, osm_bytes, limits_fingerprint, _limits):
    # Keyed on the geometry digest and limits; host free memory is read once per entry.
    return assess_zone(_geometry, limits=_limits, osm_wanted=osm_wanted, osm_estimated_bytes=osm_bytes)


def assess_aoi_file(aoi_path, *, osm_wanted: bool = False, osm_estimated_bytes: int | None = None):
    import geopandas as gpd

    path = Path(aoi_path)
    geometry = gpd.read_file(path).to_crs("EPSG:4326").geometry.union_all()
    return assess_geometry(geometry, hashlib.sha256(path.read_bytes()).hexdigest(), osm_wanted=osm_wanted,
                           osm_estimated_bytes=osm_estimated_bytes)


def assess_geometry(geometry, digest: str, *, osm_wanted: bool = False, osm_estimated_bytes: int | None = None):
    limits = ResourceLimits.from_environment()
    return _cached_assessment(digest, geometry, osm_wanted, osm_estimated_bytes, limits.fingerprint, limits)


def show_assessment(assessment, *, operation: str, prominent_warnings: bool = False) -> bool:
    """Verdict before an operation; returns False (and shows why) when it must not start.

    A refusal is always an error. Warnings are a caption in existing analysis
    pages (their scientific warnings keep priority) and a warning box where
    ``prominent_warnings`` is set (territory page).
    """
    status = verdict(assessment)
    summary = " · ".join(f"{c.label} : {_number(c.value)} {c.unit} ({VERDICTS[c.status]})" for c in assessment.checks)
    reasons = " ".join(c.reason for c in assessment.checks if c.status in (WARNING, UNKNOWN))
    if status == REFUSED:
        st.error(f"{operation} bloquée avant tout calcul : "
                 + " ".join(check.reason for check in assessment.refusals))
    elif status in (WARNING, UNKNOWN) and prominent_warnings:
        st.warning(f"{operation} : {VERDICTS[status]} de ressources. {reasons}")
    st.caption(f"Garde-fous de ressources ({operation}) — verdict : **{VERDICTS[status]}**. "
               + (f"{reasons} " if status in (WARNING, UNKNOWN) else "") + f"{summary}. {NOTE} {assessment.note}")
    return status != REFUSED
