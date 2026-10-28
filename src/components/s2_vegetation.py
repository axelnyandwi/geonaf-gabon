"""« Végétation observée par Sentinel-2 » section; calculations live in src.indicators.s2_inventory.

Nothing is computed at the initial load: the reader starts the estimate
explicitly, once per session and zone. The aggregated inventory (a few numbers
per acquisition, no raster) is cached per process on the zone, the datatakes'
file identities and the profiles, so changing the selected date never
recomputes. Resource guards run before the computation.
"""

from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st

from src.components.formatting import format_hectares_from_km2, format_percent, format_surface
from src.components.resource_panel import assess_aoi_file, show_assessment
from src.indicators.assembly import build_s2_vegetation_indicator
from src.indicators.presentation import attribution, fr
from src.indicators.profiles import S2_NDVI_VEGETATION_V0
from src.indicators.s2_inventory import BREAKDOWN_UNAVAILABLE, s2_inventory_key, s2_vegetation_inventory

VEGETATION_PROFILE = S2_NDVI_VEGETATION_V0
YEAR = 2025
NOT_COMPARABLE = ("Résultats Sentinel-2 non directement comparables à la surface végétalisée WorldCover 2021 "
                  "(classification, autre année, autre surface de référence), ni entre acquisitions dont les "
                  "masques d'observation diffèrent : un écart entre deux dates n'est pas une évolution.")
# Category definitions: causes are those given by the SCL classification, never inferred from geography.
DEFINITIONS = {
    "observed": "Pixels valides selon la politique SCL du profil scientifique : la seule surface sur laquelle "
                "l'estimation porte.",
    "vegetated": "Pixels observés exploitables dont le NDVI calibré est supérieur ou égal au seuil.",
    "not_vegetated": "Pixels observés exploitables dont le NDVI est inférieur au seuil.",
    "excluded_observed": "Pixels observés, mais dont la classe SCL est hors de la politique (eau, zones sombres, "
                         "non classées…) : écartés, pas estimés.",
    "not_observed": "Pixels classés nuage, ombre, cirrus ou défectueux par le SCL : sol non visible.",
    "other_invalid": "Classe SCL acceptée mais réflectance ou NDVI invalide.",
    "no_data": "Aucune donnée SCL (valeur absente ou classe 0) ou partie de la zone hors de la grille analysée ; "
               "la cause (bord de trace, donnée manquante…) n'est pas déterminée.",
    "unsplit": "Non exploitable ; motif non ventilé (mosaïque de plusieurs tuiles).",
}


@st.cache_resource(max_entries=4, show_spinner=False)
def cached_inventory(key, data_dir, aoi_id):
    # Guards already checked by the caller (with the host's free memory); not repeated inside the cache.
    return s2_vegetation_inventory(Path(data_dir), aoi_id, year=YEAR, profile=VEGETATION_PROFILE,
                                   available_bytes=None)


def _km2(m2):
    return format_surface(m2 / 1e6)


def _when(estimate):
    return f"{estimate.acquired_at_utc:%d/%m/%Y %H:%M} UTC"


def _option(estimate):
    status = "" if estimate.assessment.available else " · non publiable"
    return (f"{_when(estimate)} · couverture exploitable "
            f"{format_percent(estimate.measure.observed_coverage_percent)} %{status}")


def _published(indicator, estimate):
    m = estimate.measure
    with st.container(border=True):
        st.caption(indicator.label)
        st.markdown(f"<div style='font-size:1.7rem;font-weight:600;line-height:1.2'>"
                    f"{escape(format_surface(indicator.value))} <span style='font-size:1rem;font-weight:400'>"
                    f"km²</span></div>", unsafe_allow_html=True)
        st.caption(f"soit {format_hectares_from_km2(indicator.value)} ha  \n"
                   f"{format_percent(m.vegetated_share_percent)} % de la surface observée exploitable")
        st.markdown(":violet-badge[Estimation] :red-badge[Expérimental] "
                    f":orange-badge[Fiabilité : {indicator.reliability}]")
        _context_lines(estimate)


def _context_lines(estimate):
    m = estimate.measure
    lines = [f"Surface observée exploitable : {_km2(m.observed_m2)} km² · "
             f"{format_percent(m.observed_coverage_percent)} % de la zone d'étude"]
    if estimate.breakdown_status == BREAKDOWN_UNAVAILABLE:
        lines.append(f"Non exploitable : {_km2(m.zone_m2 - m.observed_m2)} km² (ventilation non disponible : "
                     "mosaïque de plusieurs tuiles)")
    else:
        lines.append(f"Observée mais exclue : {_km2(m.excluded_observed_m2)} km² · non observée (nuages, ombres) : "
                     f"{_km2(m.not_observed_m2)} km² · sans donnée : {_km2(m.no_data_m2)} km²")
    lines.append(f"Acquisition {_when(estimate)} · Copernicus Sentinel-2 L2A · seuil NDVI ≥ "
                 f"{fr(VEGETATION_PROFILE.threshold)}")
    st.caption("  \n".join(lines))


def _unpublished(indicator, estimate):
    with st.container(border=True):
        st.caption(indicator.label)
        st.markdown("<div style='font-size:1.7rem;font-weight:600;line-height:1.2'>Non disponible</div>",
                    unsafe_allow_html=True)
        st.caption(" ".join(estimate.assessment.reasons))
        st.markdown(":violet-badge[Estimation] :red-badge[Expérimental] :gray-badge[Non publiable]")
        _context_lines(estimate)
    with st.expander("Données de diagnostic (non publiables)"):
        m = estimate.measure
        st.caption("Mesures techniques conservées pour comprendre l'acquisition. Elles ne constituent pas une "
                   "estimation de la végétation de la zone d'étude.")
        share = "—" if m.vegetated_share_percent is None else f"{format_percent(m.vegetated_share_percent)} %"
        st.table(pd.DataFrame({
            "Mesure technique": ["Surface observée exploitable", f"Pixels observés à NDVI ≥ seuil",
                                 "Part de la petite surface observée"],
            "Valeur": [f"{_km2(m.observed_m2)} km² ({format_percent(m.observed_coverage_percent)} % de la zone)",
                       f"{_km2(m.vegetated_m2)} km²", share]}))
        st.caption("Sensibilité au seuil sur cette petite surface observée (diagnostic, non publiable) :")
        _sensitivity(estimate)


def _breakdown(indicator, estimate):
    rows = [{"Catégorie": item.label, "Surface (km²)": format_surface(item.value),
             "Surface (ha)": format_hectares_from_km2(item.value),
             "Part de la zone d'étude (%)": format_percent(item.share_percent)} for item in indicator.breakdown]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("Ces catégories partagent la zone d'étude sans recouvrement. Les causes indiquées sont celles de "
               "la classification SCL de l'acquisition ; aucune n'est déduite de la géographie.")
    for item in indicator.breakdown:                 # Below the table: readable on a phone without scrolling.
        st.caption(f"**{item.label}** — {DEFINITIONS.get(item.key, '')}")


def _sensitivity(estimate):
    m = estimate.measure
    rows = [{"Seuil": f"NDVI ≥ {fr(t)}",
             "Surface (km²)": _km2(area), "Surface (ha)": format_hectares_from_km2(area / 1e6),
             "Part de la surface observée (%)": "—" if m.observed_m2 <= 0
             else format_percent(100.0 * area / m.observed_m2)} for t, area in m.sensitivity_m2]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("Même surface observée pour les trois seuils : l'écart mesure la dépendance au seuil, pas une "
               "incertitude statistique.")


def _provenance(indicator, estimate, inventory):
    credit = attribution(indicator.source, estimate.acquired_at_utc.year)
    lines = [f"**Nature** : estimation ; statut scientifique **{indicator.scientific_status}** (méthode non validée).",
             f"**Produits** : {', '.join(estimate.product_ids)}",
             f"**Dates** : acquisition {_when(estimate)} ; ingestion GEONAF "
             f"{indicator.dates.ingested_at_utc:%d/%m/%Y %H:%M} UTC" if indicator.dates.ingested_at_utc else
             f"**Dates** : acquisition {_when(estimate)} ; ingestion inconnue",
             f"**Profil scientifique** : {inventory.scientific_profile_id} {inventory.scientific_profile_version} "
             f"(empreinte {inventory.scientific_profile_fingerprint[:16]}…), politique SCL « {inventory.scl_policy} »",
             f"**Profil expérimental** : {VEGETATION_PROFILE.profile_id} {VEGETATION_PROFILE.version} (empreinte "
             f"{VEGETATION_PROFILE.fingerprint[:16]}…), seuil NDVI ≥ {fr(VEGETATION_PROFILE.threshold)}, couverture "
             f"minimale {fr(VEGETATION_PROFILE.min_observed_coverage_percent)} %",
             f"**Ventilation** : {estimate.breakdown_status}",
             f"**Fiabilité : {indicator.reliability}** — " + " ".join(indicator.reliability_reasons),
             f"**Méthode** : {indicator.method}"]
    st.markdown("  \n".join(lines))
    if credit is not None:
        if credit.verified:
            st.caption(f"Attribution vérifiée : {credit.text} ({credit.licence})")
        else:
            st.caption(f"Attribution non revérifiée : « {credit.text} » — mention reprise de la note de publication "
                       "du paquet de démonstration v3, non contrôlée sur l'avis juridique Copernicus.")
    st.caption("Empreintes des manifestes vérifiés : " + ", ".join(
        f"{p['product_id']} {p['manifest_sha256'][:12]}…" for p in estimate.provenance))
    st.markdown("**Limites scientifiques**")
    for limit in indicator.limits:
        st.caption(f"- {limit}")


def _start_button(aoi_id, flag):
    st.caption("Surface à NDVI ≥ 0,4 sur la seule surface réellement observée par Sentinel-2 en 2025. Le calcul "
               "lit les acquisitions locales vérifiées (quelques secondes) ; il n'est pas lancé automatiquement.")
    # The callback sets the flag before the rerun: the button disappears as soon as it is clicked.
    st.button("Calculer l'estimation Sentinel-2", key=f"s2_vegetation_start_{aoi_id}",
              on_click=st.session_state.__setitem__, args=(flag, True))


def show_s2_vegetation(data_dir, aoi_id: str) -> None:
    st.subheader("Végétation observée par Sentinel-2")
    st.markdown(":red-badge[Estimation expérimentale] :gray-badge[Non validée scientifiquement]")
    st.warning(NOT_COMPARABLE)
    flag = f"s2_vegetation_on_{aoi_id}"
    if not st.session_state.get(flag):
        _start_button(aoi_id, flag)
        return
    data_dir = Path(data_dir)
    if not show_assessment(assess_aoi_file(data_dir / "aoi" / f"{aoi_id}.geojson"),
                           operation="Estimation Sentinel-2"):
        return
    try:
        key = s2_inventory_key(data_dir, aoi_id, year=YEAR, profile=VEGETATION_PROFILE)
        with st.spinner("Lecture des acquisitions Sentinel-2 vérifiées…"):
            inventory = cached_inventory(key, str(data_dir), aoi_id)
    except Exception:
        st.warning("Estimation Sentinel-2 indisponible (acquisitions locales illisibles ou profil refusé).")
        return
    for note in inventory.notes:
        st.caption(note)
    for label, reason in inventory.failures:
        st.caption(f"Acquisition écartée — {label} : {reason}")
    if not inventory.estimates:
        st.info("Aucune estimation Sentinel-2 : aucune acquisition exploitable pour cette zone d'étude.")
        return
    estimates = list(reversed(inventory.estimates))                # Most recent first.
    default = inventory.default_estimate()
    index = estimates.index(default) if default is not None else 0
    selected = st.selectbox("Acquisition", range(len(estimates)), index=index,
                            format_func=lambda i: _option(estimates[i]), key=f"s2_vegetation_date_{aoi_id}")
    estimate = estimates[selected]
    if default is None:
        st.caption("Aucune acquisition n'atteint la couverture minimale : aucune estimation n'est publiée.")
    indicator = build_s2_vegetation_indicator(estimate, inventory)
    (_published if estimate.assessment.available else _unpublished)(indicator, estimate)
    with st.expander("Ventilation de la zone d'étude et définitions"):
        _breakdown(indicator, estimate)
    if estimate.assessment.available:     # Otherwise shown with the diagnostic data, never as an estimate.
        with st.expander("Sensibilité au seuil NDVI (0,3 · 0,4 · 0,5)"):
            _sensitivity(estimate)
    with st.expander("Provenance, profils, méthode et limites"):
        _provenance(indicator, estimate, inventory)
