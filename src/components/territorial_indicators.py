"""« Chiffres clés du territoire » section; calculations live in src.services.territorial_indicators.

Each card shows the essentials (value in km² then hectares, shares with their
denominator, nature of the result, reliability, date); provenance, licence,
method and limits sit in a collapsed section so that phones stay readable.
"""

from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st

from src.components.errors import show_internal_error
from src.components.formatting import (
    format_hectares_from_km2, format_percent, format_pixels, format_surface, french_table,
)
from src.indicators.presentation import (
    RESULT_KIND_TEXT, ROAD_GLOSSARY, attribution, highway_label, road_category_label, share_text,
)
from src.services.compute_slots import heavy_computation
from src.services.territorial_indicators import (
    AVAILABLE, LOW, NOT_ASSESSED, compute_territorial_indicators, indicator_inputs,
)

UNAVAILABLE_TEXT = "Non disponible"
RELIABILITY_COLORS = {"élevée": "green", "moyenne": "blue", LOW: "orange", NOT_ASSESSED: "gray"}
KIND_COLORS = {"mesure géométrique": "gray", "observation d'une base cartographique": "orange",
               "estimation": "violet", "prédiction": "red"}
CARDS = (  # (indicator, secondary indicator shown under the value)
    ("aoi.area_km2", None), ("landcover.land_km2", None), ("landcover.vegetation_km2", None),
    ("landcover.builtup_km2", None), ("osm.roads_km", None), ("osm.buildings_count", None),
    ("osm.buildings_density_land", "osm.buildings_density_aoi"), ("osm.buildings_footprint_km2", None),
)
# Ratios whose denominator comes from another source: both dates are shown, never one alone.
DENOMINATOR_OF = {"osm.buildings_density_land": "landcover.land_km2"}


@st.cache_resource(max_entries=4, show_spinner=False)
def cached_indicators(key, _inputs, aoi_label):
    # One entry per (AOI digest, layer fingerprints, indicator profile): see indicator_inputs.
    with heavy_computation():  # Cache miss only: shared limit of simultaneous heavy computations.
        return compute_territorial_indicators(_inputs, aoi_label=aoi_label)


def _value(indicator):
    if indicator.value is None:
        return UNAVAILABLE_TEXT
    if indicator.unit == "bâtiments":
        return format_pixels(indicator.value)
    if indicator.unit == "km":
        return f"{indicator.value:,.0f}".replace(",", " ")
    if indicator.unit == "bâtiments/km²":
        return f"{indicator.value:,.0f}".replace(",", " ")
    return format_surface(indicator.value)


def _year(indicator):
    dates = indicator.dates
    return None if dates is None or dates.observation_start_utc is None else dates.observation_start_utc.year


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


def _short_date(indicator):
    """Visible date: the observed period when known, else the state of the reference database."""
    dates = indicator.dates
    if dates is None:
        return ""
    if dates.observation_start_utc is None:
        return f"Référentiel : {dates.reference_label}"
    return _date_line(indicator).split(" · ")[0]


def _kind_badge(indicator):
    label = RESULT_KIND_TEXT.get(indicator.result_kind, ("Nature non qualifiée", ""))[0]
    return f":{KIND_COLORS.get(indicator.result_kind, 'gray')}-badge[{label}]"


def _credit_line(source, year, prefix="Attribution"):
    credit = attribution(source, year)
    if credit is None:
        return None
    licence = f"[{credit.licence}]({credit.licence_url})" if credit.licence_url else credit.licence
    text = f"**{prefix}** : {credit.text} · Licence {licence}"
    if credit.citation:
        text += f" · Citation : {credit.citation}"
    if not credit.verified:
        text += " · mention issue du descripteur, non revérifiée"
    return text


def _ratio_lines(indicator, denominator):
    """Numerator and denominator sources with their own dates, read from the metadata."""
    return [f"Numérateur : bâtiments OpenStreetMap — {indicator.dates.reference_label}",
            f"Dénominateur : surface terrestre {denominator.source.product} ({denominator.source.version}) — "
            f"carte {_year(denominator)}",
            "Sources de dates différentes : ce rapport ne décrit pas une même année d'observation."]


def _provenance(indicator, denominator=None):
    """Collapsed details: nature, source, licence, dates, coverage, reliability reasons, method, limits."""
    with st.expander("Provenance et fiabilité"):
        label, explanation = RESULT_KIND_TEXT.get(indicator.result_kind, ("Nature non qualifiée", ""))
        lines = [f"**Nature du résultat** : {label}. {explanation}"]
        if indicator.source is not None:
            lines.append(f"**Source** : {indicator.source.product} ({indicator.source.version}) — "
                         f"{indicator.source.nature}")
        credit = _credit_line(indicator.source, _year(indicator))
        if credit is not None:
            lines.append(credit)
        if indicator.dates is not None:
            lines.append(f"**Dates** : {_date_line(indicator)}")
        if denominator is not None and denominator.source is not None:
            lines.append(f"**Dénominateur** : surface terrestre, {denominator.source.product} "
                         f"({denominator.source.version}) — {denominator.source.nature}")
            lines.append(f"**Dates du dénominateur** : {_date_line(denominator)}")
            credit = _credit_line(denominator.source, _year(denominator), "Attribution du dénominateur")
            if credit is not None:
                lines.append(credit)
        if indicator.coverage_percent is not None:
            lines.append(f"**Couverture de la zone d'étude par la source** : "
                         f"{format_percent(indicator.coverage_percent)} %")
        lines.append(f"**Fiabilité : {indicator.reliability}** — " + " ".join(indicator.reliability_reasons))
        lines.append(f"**Méthode** : {indicator.method}")
        st.markdown("  \n".join(lines))
        for limit in indicator.limits:
            st.caption(f"- {limit}")


def _card(indicator, secondary=None, denominator=None):
    with st.container(border=True):
        st.caption(indicator.label)
        unit = "" if indicator.value is None else f" <span style='font-size:1rem;font-weight:400'>" \
                                                   f"{escape(indicator.unit)}</span>"
        st.markdown(f"<div style='font-size:1.7rem;font-weight:600;line-height:1.2'>{escape(_value(indicator))}"
                    f"{unit}</div>", unsafe_allow_html=True)
        if indicator.status != AVAILABLE or indicator.value is None:
            st.caption(indicator.method)
            st.markdown(_kind_badge(indicator))
            return
        details = []
        if indicator.unit == "km²":
            details.append(f"soit {format_hectares_from_km2(indicator.value)} ha")
        details += [share_text(label, p, format_percent) for label, p in indicator.shares]
        if secondary is not None and secondary.value is not None:
            details.append(f"{_value(secondary)} {secondary.unit} sur toute la zone d'étude (eaux comprises)")
        if details:
            st.caption("  \n".join(details))
        st.markdown(f"{_kind_badge(indicator)} "
                    f":{RELIABILITY_COLORS[indicator.reliability]}-badge[Fiabilité : {indicator.reliability}]")
        if denominator is not None and denominator.status == AVAILABLE and indicator.dates is not None:
            st.caption("  \n".join(_ratio_lines(indicator, denominator)))
        elif indicator.dates is not None:
            st.caption(_short_date(indicator))
        _provenance(indicator, denominator)


def _breakdown_frame(indicator, unit_label, *, hectares=False, label=lambda text: text, detail=lambda key: key):
    rows = []
    for item in indicator.breakdown:
        row = {"Regroupement": label(item.label), unit_label: item.value}      # Full precision; display rounds.
        if hectares:
            row["Surface (ha)"] = item.value * 100.0
        row["Part de la zone d'étude (%)"] = item.share_percent
        row["Détail"] = " ; ".join(f"{detail(key)} {format_surface(value)}" for key, value in item.detail)
        rows.append(row)
    return pd.DataFrame(rows)


def _details(result):
    roads, vegetation = result.get("osm.roads_km"), result.get("landcover.vegetation_km2")
    with st.expander("Voies cartographiées par catégorie (OSM)"):
        if roads.status == AVAILABLE:
            frame = _breakdown_frame(roads, "Longueur (km)", label=road_category_label, detail=highway_label).rename(
                columns={"Part de la zone d'étude (%)": "Part des voies (%)", "Regroupement": "Catégorie",
                         "Détail": "Valeurs OSM « highway » (km)"})
            st.dataframe(french_table(frame, {"Longueur (km)": 2, "Part des voies (%)": 2}), hide_index=True,
                         width="stretch")
            st.markdown("**Catégories**")
            for item in roads.breakdown:
                shown, explanation = ROAD_GLOSSARY.get(item.label, (item.label, ""))
                st.caption(f"**{shown}** — {explanation}")
            for limit in roads.limits:
                st.caption(f"- {limit}")
        else:
            st.caption(roads.method)
    with st.expander("Végétation par regroupement (WorldCover)"):
        if vegetation.status == AVAILABLE:
            st.dataframe(french_table(_breakdown_frame(vegetation, "Surface (km²)", hectares=True),
                                      {"Surface (km²)": 2, "Surface (ha)": 1, "Part de la zone d'étude (%)": 2}),
                         hide_index=True, width="stretch")
            st.caption(vegetation.method)
        else:
            st.caption(vegetation.method)
    with st.expander("Méthodes, dates, sources et limites"):
        st.dataframe(french_table(pd.DataFrame([{
            "Indicateur": item.label, "Valeur": _value(item), "Unité": item.unit, "Statut": item.status,
            "Nature du résultat": RESULT_KIND_TEXT.get(item.result_kind, ("Nature non qualifiée",))[0],
            "Source": None if item.source is None else f"{item.source.product} ({item.source.version})",
            "Nature de la source": None if item.source is None else item.source.nature,
            "Date d'observation": None if item.dates is None else _date_line(item).split(" · ")[0]
                                                                  .removeprefix("Observation : "),
            "Référentiel": None if item.dates is None else item.dates.reference_label,
            "Ingestion GEONAF (UTC)": None if item.dates is None or item.dates.ingested_at_utc is None
                                      else f"{item.dates.ingested_at_utc:%d/%m/%Y %H:%M}",
            "Couverture (%)": item.coverage_percent,
            "Fiabilité": item.reliability, "Méthode": item.method,
        } for item in result.indicators]), {"Couverture (%)": 2}), hide_index=True, width="stretch")
        st.caption("« — » : sans objet ou non disponible.")
        st.caption("Nature du résultat : « mesure géométrique » = calcul exact sur la géométrie ; « observation "
                   "d'une base cartographique » = contenu d'OpenStreetMap à la date de l'instantané, pas le "
                   "terrain ; « estimation » = sortie d'un modèle ou d'une classification ; « prédiction » = "
                   "projection d'un état futur (aucune dans cette version).")
        st.caption("Fiabilité : « élevée » pour une géométrie exacte ; « moyenne » pour une classification dont "
                   "l'exactitude est déclarée par le producteur sans vérification locale ; « faible » si la "
                   "couverture est incomplète ; « non évaluée » pour OpenStreetMap, dont la complétude est inconnue.")
        st.caption(f"Profil d'indicateurs {result.profile.profile_id} {result.profile.version} · empreinte "
                   f"{result.profile.fingerprint[:16]}… · zone d'étude sha256 {result.aoi_sha256[:16]}….")
    # Visible, not folded: a single WorldCover map or OSM snapshot never yields an evolution.
    st.markdown("**Évolutions temporelles** — aucune évolution n'est affichée sans deux référentiels strictement "
                "comparables.")
    for note in result.temporal_notes:
        st.caption(f"- {note}")


def _credits(result):
    """Visible attribution of every source used (licences require it near the data)."""
    seen, lines = set(), []
    for item in result.indicators:
        credit = attribution(item.source, _year(item)) if item.status == AVAILABLE else None
        if credit is not None and credit.text not in seen:
            seen.add(credit.text)
            licence = f"[{credit.licence}]({credit.licence_url})" if credit.licence_url else credit.licence
            lines.append(f"{credit.text} ({licence})")
    if lines:
        st.caption("Sources : " + " · ".join(lines))


def show_key_figures(data_dir, aoi_id: str, aoi_label: str) -> None:
    """First home section; independent of the analysis mode (no Sentinel-2 mask is used)."""
    st.header("Chiffres clés du territoire")
    st.caption(f"Zone d'étude {aoi_label} : chiffres issus des couches stockées localement, sans téléchargement. "
               "Chaque valeur indique sa nature, sa date et sa fiabilité ; sa provenance complète est dans la "
               "section repliable « Provenance et fiabilité » de chaque valeur.")
    try:
        inputs = indicator_inputs(Path(data_dir), aoi_id)
        with st.spinner("Calcul des chiffres clés…"):
            result = cached_indicators(inputs.key, inputs, f"Zone d'étude {aoi_label}")
    except Exception as error:
        show_internal_error(error, context="chiffres_cles", level="warning",
                            message="Chiffres clés indisponibles (couche locale illisible ou modifiée depuis son "
                                    "enregistrement).")
        return
    for row in (CARDS[:4], CARDS[4:]):
        for column, (indicator_id, secondary_id) in zip(st.columns(4), row):
            with column:
                _card(result.get(indicator_id), result.get(secondary_id) if secondary_id else None,
                      result.get(DENOMINATOR_OF[indicator_id]) if indicator_id in DENOMINATOR_OF else None)
    if result.missing:
        st.caption("Sources absentes localement : " + ", ".join(result.missing) + ". Elles peuvent être récupérées "
                   "depuis le démonstrateur multisource ; aucune valeur n'est estimée en leur absence.")
    for warning in result.warnings:
        st.warning(warning)
    st.caption("Les bâtiments et voies OpenStreetMap sont ceux qui ont été cartographiés, pas le total réel. "
               "WorldCover décrit l'année de sa carte, pas l'état actuel.")
    _credits(result)
    _details(result)
