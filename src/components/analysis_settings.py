"""Shared territory and analysis-mode controls; profile logic stays in src.geo.scientific_profile."""

from pathlib import Path

import streamlit as st

from src.components.errors import show_error
from src.geo.scientific_profile import (
    ADVANCED_BOUNDS, ADVANCED_SCL_POLICIES, REFERENCE_PROFILES, SENSITIVITY_PRESET, advanced_configuration,
    standard_configuration,
)

TERRITORY_LABELS = {"akanda": "Akanda", "libreville": "Libreville", "owendo": "Owendo"}
SCL_LABELS = {"usable_land": "Végétation et sols nus (SCL 4, 5) — recommandé",
              "vegetation": "Végétation seule (SCL 4)", "bare_soil": "Sols nus seuls (SCL 5)"}
MODES = ("Analyse standard", "Analyse avancée")


def available_territories(data_dir) -> list[str]:
    """Territories with a reference profile and a local AOI file."""
    root = Path(data_dir) / "aoi"
    return [aoi_id for aoi_id in TERRITORY_LABELS if aoi_id in REFERENCE_PROFILES and (root / f"{aoi_id}.geojson").is_file()]


def choose_territory(data_dir, key: str) -> str | None:
    territories = available_territories(data_dir)
    if not territories:
        st.error("Aucune AOI locale disponible (Akanda, Libreville, Owendo).")
        return None
    return st.selectbox("Territoire", territories, format_func=TERRITORY_LABELS.get, key=key)


def _apply_sensitivity(prefix):
    st.session_state[f"{prefix}_threshold"] = SENSITIVITY_PRESET["change_threshold"]
    st.session_state[f"{prefix}_coverage"] = SENSITIVITY_PRESET["min_comparable_coverage_percent"]


def _reset(prefix, reference):
    st.session_state[f"{prefix}_threshold"] = reference.change_threshold
    st.session_state[f"{prefix}_coverage"] = reference.min_comparable_coverage_percent
    st.session_state[f"{prefix}_scl"] = reference.scl_policy


def choose_analysis(aoi_id: str, prefix: str):
    """Return an AnalysisConfiguration; invalid advanced values fall back to nothing (None)."""
    mode = st.radio("Mode d'analyse", MODES, horizontal=True, key=f"{prefix}_mode",
                    help="Standard : profil expérimental de référence du territoire. Avancée : paramètres "
                         "personnalisés, versionnés et tracés.")
    reference = REFERENCE_PROFILES[aoi_id]
    if mode == MODES[0]:
        configuration = standard_configuration(aoi_id)
        st.caption(f"Profil de référence **{reference.label}** · seuil ΔNDVI {reference.change_threshold:g} · "
                   f"couverture minimale {reference.min_comparable_coverage_percent:g} % · masque SCL "
                   f"« {SCL_LABELS.get(reference.scl_policy, reference.scl_policy)} ». "
                   f"Statut : expérimental, non validé. {reference.rationale}")
        return configuration
    for name, value in (("threshold", reference.change_threshold),
                        ("coverage", reference.min_comparable_coverage_percent), ("scl", reference.scl_policy)):
        st.session_state.setdefault(f"{prefix}_{name}", value)
    columns = st.columns([2, 2, 3])
    low, high = ADVANCED_BOUNDS["change_threshold"]
    threshold = columns[0].number_input("Seuil de variation ΔNDVI", min_value=low, max_value=high, step=0.01,
                                        format="%.2f", key=f"{prefix}_threshold")
    low, high = ADVANCED_BOUNDS["min_comparable_coverage_percent"]
    coverage = columns[1].number_input("Couverture minimale (%)", min_value=low, max_value=high, step=1.0,
                                       key=f"{prefix}_coverage")
    policy = columns[2].selectbox("Masque SCL", ADVANCED_SCL_POLICIES, format_func=lambda p: SCL_LABELS.get(p, p),
                                  key=f"{prefix}_scl")
    buttons = st.columns(2)
    buttons[0].button("Scénario de sensibilité (0,15 ; 50 %)", key=f"{prefix}_preset",
                      on_click=_apply_sensitivity, args=(prefix,))
    buttons[1].button("Revenir au profil de référence", key=f"{prefix}_reset", on_click=_reset,
                      args=(prefix, reference))
    try:
        configuration = advanced_configuration(aoi_id, change_threshold=threshold,
                                               min_comparable_coverage_percent=coverage, scl_policy=policy)
    except ValueError as error:
        show_error(error, context="analyse.configuration", prefix="Configuration refusée : ")
        return None
    if configuration.overrides:
        changes = " ; ".join(f"{name} = {value:g}" if isinstance(value, float) else f"{name} = {value}"
                             for name, value in configuration.overrides)
        st.warning(f"Configuration avancée **non validée** dérivée de {reference.profile_id} : {changes}. "
                   f"Version {configuration.effective.version} · empreinte "
                   f"{configuration.effective.fingerprint[:16]}… (incluse dans la provenance et les caches).")
        if policy != "usable_land":
            st.caption("Ce masque exclut les transitions végétation ↔ sol nu : les variations sont marquées "
                       "non comparables et la carte NDVI interactive peut être indisponible.")
    else:
        st.caption("Paramètres identiques au profil de référence : aucune configuration dérivée.")
    return configuration
