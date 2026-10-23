"""« Chiffres clés du territoire » section; calculations live in src.services.territorial_indicators."""

from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st

from src.components.formatting import format_percent, format_pixels, format_surface
from src.services.territorial_indicators import (
    AVAILABLE, LOW, NOT_ASSESSED, compute_territorial_indicators, indicator_inputs,
)

RELIABILITY_COLORS = {"élevée": "green", "moyenne": "blue", LOW: "orange", NOT_ASSESSED: "gray"}
NATURE_COLORS = {"Classification externe": "violet", "Base collaborative": "orange",
                 "Géométrie de référence": "gray"}
CARDS = (  # (indicator, secondary indicator shown under the value)
    ("aoi.area_km2", None), ("landcover.land_km2", None), ("landcover.vegetation_km2", None),
    ("landcover.builtup_km2", None), ("osm.roads_km", None), ("osm.buildings_count", None),
    ("osm.buildings_density_land", "osm.buildings_density_aoi"), ("osm.buildings_footprint_km2", None),
)


@st.cache_resource(max_entries=4, show_spinner=False)
def cached_indicators(key, _inputs, aoi_label):
    # One entry per (AOI digest, layer fingerprints, indicator profile): see indicator_inputs.
    return compute_territorial_indicators(_inputs, aoi_label=aoi_label)


def _value(indicator):
    if indicator.value is None:
        return "—"
    if indicator.unit == "bâtiments":
        return format_pixels(indicator.value)
    if indicator.unit == "km":
        return f"{indicator.value:,.0f}".replace(",", " ")
    if indicator.unit == "bâtiments/km²":
        return f"{indicator.value:,.0f}".replace(",", " ")
    return format_surface(indicator.value)


def _date_line(indicator):
    dates = indicator.dates
    if dates is None:
        return ""
    if dates.observation_start_utc is not None:
        observed = (f"{dates.observation_start_utc:%Y}" if dates.observation_start_utc.year
                    == dates.observation_end_utc.year else
                    f"{dates.observation_start_utc:%d/%m/%Y} → {dates.observation_end_utc:%d/%m/%Y}")
    else:
        observed = "inconnue"
    ingested = "inconnue" if dates.ingested_at_utc is None else f"{dates.ingested_at_utc:%d/%m/%Y}"
    return f"Observation : {observed} · Référentiel : {dates.reference_label} · Ingestion GEONAF : {ingested}"


def _card(indicator, secondary=None):
    with st.container(border=True):
        st.caption(indicator.label)
        st.markdown(f"<div style='font-size:1.7rem;font-weight:600;line-height:1.2'>{escape(_value(indicator))}"
                    f" <span style='font-size:1rem;font-weight:400'>{escape(indicator.unit)}</span></div>",
                    unsafe_allow_html=True)
        if indicator.status != AVAILABLE:
            st.caption(indicator.method)
            return
        details = [f"{format_percent(p)} % de la {label}" for label, p in indicator.shares]
        if secondary is not None and secondary.value is not None:
            details.append(f"{_value(secondary)} {secondary.unit} sur toute la zone d'étude")
        if details:
            st.caption(" · ".join(details))
        badges = f":{NATURE_COLORS.get(indicator.source.nature, 'gray')}-badge[{indicator.source.nature}] "
        badges += f":{RELIABILITY_COLORS[indicator.reliability]}-badge[Fiabilité : {indicator.reliability}]"
        st.markdown(badges)
        if indicator.dates is not None:
            st.caption(_date_line(indicator))


def _breakdown_frame(indicator, unit_label):
    rows = []
    for item in indicator.breakdown:
        rows.append({"Regroupement": item.label, unit_label: round(item.value, 2),
                     "Part de la zone d'étude (%)": None if item.share_percent is None
                     else round(item.share_percent, 2),
                     "Détail": " ; ".join(f"{key} {value:.2f}" for key, value in item.detail)})
    return pd.DataFrame(rows)


def _details(result):
    roads, vegetation = result.get("osm.roads_km"), result.get("landcover.vegetation_km2")
    with st.expander("Voies cartographiées par catégorie (OSM)"):
        if roads.status == AVAILABLE:
            frame = _breakdown_frame(roads, "Longueur (km)").rename(
                columns={"Part de la zone d'étude (%)": "Part des voies (%)"})
            st.dataframe(frame, hide_index=True, width="stretch")
            for limit in roads.limits:
                st.caption(f"- {limit}")
        else:
            st.caption(roads.method)
    with st.expander("Végétation par regroupement (WorldCover)"):
        if vegetation.status == AVAILABLE:
            st.dataframe(_breakdown_frame(vegetation, "Surface (km²)"), hide_index=True, width="stretch")
            st.caption(vegetation.method)
        else:
            st.caption(vegetation.method)
    with st.expander("Méthodes, dates, sources et limites"):
        st.dataframe(pd.DataFrame([{
            "Indicateur": item.label, "Valeur": _value(item), "Unité": item.unit, "Statut": item.status,
            "Source": None if item.source is None else f"{item.source.product} ({item.source.version})",
            "Nature": None if item.source is None else item.source.nature,
            "Date d'observation": None if item.dates is None else _date_line(item).split(" · ")[0]
                                                                  .removeprefix("Observation : "),
            "Référentiel": None if item.dates is None else item.dates.reference_label,
            "Ingestion GEONAF (UTC)": None if item.dates is None or item.dates.ingested_at_utc is None
                                      else f"{item.dates.ingested_at_utc:%d/%m/%Y %H:%M}",
            "Couverture (%)": None if item.coverage_percent is None else round(item.coverage_percent, 2),
            "Fiabilité": item.reliability, "Méthode": item.method,
        } for item in result.indicators]), hide_index=True, width="stretch")
        st.caption("Fiabilité : « élevée » pour une géométrie exacte ; « moyenne » pour une classification dont "
                   "l'exactitude est déclarée par le producteur sans vérification locale ; « faible » si la "
                   "couverture est incomplète ; « non évaluée » pour OpenStreetMap, dont la complétude est inconnue.")
        for item in result.indicators:
            if item.limits:
                st.markdown(f"**{item.label}**")
                for limit in item.limits:
                    st.caption(f"- {limit}")
        st.caption(f"Profil d'indicateurs {result.profile.profile_id} {result.profile.version} · empreinte "
                   f"{result.profile.fingerprint[:16]}… · zone d'étude sha256 {result.aoi_sha256[:16]}….")
    with st.expander("Évolutions temporelles"):
        st.caption("Aucune évolution n'est affichée sans deux référentiels strictement comparables.")
        for note in result.temporal_notes:
            st.caption(f"- {note}")


def show_key_figures(data_dir, aoi_id: str, aoi_label: str) -> None:
    """First home section; independent of the analysis mode (no Sentinel-2 mask is used)."""
    st.header("Chiffres clés du territoire")
    st.caption(f"Zone d'étude {aoi_label} : chiffres issus des couches stockées localement, sans téléchargement. "
               "Chaque valeur indique sa source, sa date d'observation, son référentiel et sa fiabilité.")
    try:
        inputs = indicator_inputs(Path(data_dir), aoi_id)
        with st.spinner("Calcul des chiffres clés…"):
            result = cached_indicators(inputs.key, inputs, f"Zone d'étude {aoi_label}")
    except Exception:
        st.warning("Chiffres clés indisponibles (couche locale illisible ou modifiée depuis son enregistrement).")
        return
    for row in (CARDS[:4], CARDS[4:]):
        for column, (indicator_id, secondary_id) in zip(st.columns(4), row):
            with column:
                _card(result.get(indicator_id), result.get(secondary_id) if secondary_id else None)
    if result.missing:
        st.caption("Sources absentes localement : " + ", ".join(result.missing) + ". Elles peuvent être récupérées "
                   "depuis le démonstrateur multisource ; aucune valeur n'est estimée en leur absence.")
    for warning in result.warnings:
        st.warning(warning)
    st.caption("Les bâtiments et voies OpenStreetMap sont ceux qui ont été cartographiés, pas le total réel. "
               "WorldCover décrit l'année de sa carte, pas l'état actuel.")
    _details(result)
