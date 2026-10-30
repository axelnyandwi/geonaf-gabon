"""Streamlit presentation of NDVI series; calculations stay in services."""

from datetime import timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from src.components.errors import show_error
from src.components.formatting import format_ndvi, format_percent, format_pixels
from src.components.analysis_settings import choose_analysis, choose_territory
from src.services.local_preview import (
    LocalPreviewError, analyze_local_ndvi_series, list_local_observations, local_series_key,
)
from src.services.ndvi_time_series import MIN_SERIES_DATES, series_csv
from src.services.observation_selection import acquisition_day, catalogue_bounds, observation_label

STATUS_LABELS = {"exploration": "Exploration", "not_assessed": "Comparabilité non évaluée",
                 "not_comparable": "Non comparable", "validated": "Résultat validé"}


@st.cache_resource(max_entries=8, show_spinner=False)
def cached_series(key, _observations, data_dir, _profile, period, gap_warning_days):
    # Immutable result keyed by file revisions, AOI, profile, period and gap threshold.
    return analyze_local_ndvi_series(_observations, Path(data_dir), scientific_profile=_profile,
                                     period=period, gap_warning_days=gap_warning_days)


def _frames(result):
    rows = []
    for item in result.dates:
        # Explicit "Z": browsers otherwise parse ISO datetimes as local time.
        rows.append({"date": f"{item.acquired_at_utc:%Y-%m-%dT%H:%M:%S}Z",
                     "Date (UTC)": f"{item.acquired_at_utc:%d/%m/%Y %H:%M}", "own_mean": item.own.mean, "own_median": item.own.median,
                     "coverage": item.valid_coverage_percent, "included": item.included,
                     "Retenue": "oui" if item.included else "non",
                     "common_mean": item.common.mean if item.common else None,
                     "common_median": item.common.median if item.common else None})
    frame = pd.DataFrame(rows)
    common = frame[frame["included"]].melt(["date", "Date (UTC)"], ["common_mean", "common_median"],
                                           "Statistique", "NDVI")
    own = frame.melt(["date", "Date (UTC)", "Retenue"], ["own_mean", "own_median"], "Statistique", "NDVI").dropna()
    names = {"common_mean": "Moyenne", "common_median": "Médiane", "own_mean": "Moyenne", "own_median": "Médiane"}
    common["Statistique"] = common["Statistique"].map(names)
    own["Statistique"] = own["Statistique"].map(names)
    return frame, common, own


def _charts(result):
    frame, common, own = _frames(result)
    # One time axis for all charts: the requested period, so gaps stay visible.
    days = result.period or (result.dates[0].acquired_at_utc.date(), result.dates[-1].acquired_at_utc.date())
    domain = [f"{days[0] - timedelta(days=1):%Y-%m-%d}T00:00:00Z", f"{days[1] + timedelta(days=1):%Y-%m-%d}T23:59:59Z"]
    x = alt.X("date:T", title="Acquisition (UTC)", scale=alt.Scale(type="utc", domain=domain),
              axis=alt.Axis(format="%d/%m", labelAngle=0))
    legend = alt.Legend(title="Statistique")
    tooltip = [alt.Tooltip("Date (UTC):N"),
               alt.Tooltip("Statistique:N"), alt.Tooltip("NDVI:Q", format=".4f")]
    y = alt.Y("NDVI:Q", scale=alt.Scale(domain=[-1, 1]), title="NDVI")
    st.markdown("**NDVI sur pixels communs** — seule courbe dont les dates portent sur les mêmes pixels.")
    st.altair_chart(alt.Chart(common).mark_line(point=True).encode(
        x=x, y=y, color=alt.Color("Statistique:N", legend=legend),
        strokeDash=alt.StrokeDash("Statistique:N", legend=legend), tooltip=tooltip).interactive(),
        width="stretch")
    st.markdown("**NDVI propre à chaque date** — points non reliés : pixels différents, non comparables entre dates.")
    st.altair_chart(alt.Chart(own).mark_point(filled=True, size=70).encode(
        x=x, y=y, color=alt.Color("Statistique:N", legend=legend), shape=alt.Shape("Retenue:N"),
        tooltip=tooltip + [alt.Tooltip("Retenue:N")]).interactive(), width="stretch")
    minimum = result.scientific_profile.min_comparable_coverage_percent
    bars = alt.Chart(frame).mark_bar(size=8).encode(
        x=x, y=alt.Y("coverage:Q", scale=alt.Scale(domain=[0, 100]), title="Couverture valide AOI (%)"),
        color=alt.Color("Retenue:N"),
        tooltip=[alt.Tooltip("Date (UTC):N"), alt.Tooltip("coverage:Q", title="Couverture (%)", format=".2f")])
    rule = alt.Chart(pd.DataFrame({"minimum": [minimum]})).mark_rule(strokeDash=[4, 4]).encode(y="minimum:Q")
    st.markdown(f"**Couverture valide par date** — ligne : minimum du profil ({minimum:g} %).")
    st.altair_chart((bars + rule).interactive(), width="stretch")


def _missing(result, in_period, chosen):
    lines = []
    for item in result.dates:
        if not item.included:
            lines.append(f"{item.acquired_at_utc:%d/%m/%Y %H:%M} UTC · exclue : {item.exclusion_reason}")
    kinds = {"period_start": "Début de période → 1re date retenue",
             "between_dates": "Entre deux dates retenues", "period_end": "Dernière date retenue → fin de période"}
    for gap in result.gaps:
        if gap.exceeds_warning:
            lines.append(f"{kinds[gap.kind]} : {format_percent(gap.days)} jours sans observation retenue.")
    for observation in in_period:
        if observation not in chosen:
            lines.append(f"{observation_label(observation)} · disponible localement, non sélectionnée.")
    return lines


def show_ndvi_series(data_dir):
    st.title("Série temporelle NDVI")
    st.caption("Acquisitions locales vérifiées uniquement ; aucun téléchargement. Même AOI, même grille, "
               "calibration BOA de chaque produit et masque SCL du profil.")
    try:
        observations = list_local_observations(data_dir)
        first, last = catalogue_bounds(observations)
    except LocalPreviewError as error:
        show_error(error, context="series.acquisitions")
        return
    aoi_id = choose_territory(data_dir, "series_territory")
    if aoi_id is None:
        return
    configuration = choose_analysis(aoi_id, "series_analysis")
    if configuration is None:
        return
    profile = configuration.effective
    columns = st.columns(3)
    start = columns[0].date_input("Début de période (UTC)", value=first, key="series_start")
    end = columns[1].date_input("Fin de période (UTC)", value=last, key="series_end")
    gap = columns[2].number_input("Seuil de lacune (jours)", min_value=1, max_value=365, value=15,
                                  key="series_gap", help="Intervalle signalé comme lacune entre dates retenues.")
    if not isinstance(start, type(first)) or not isinstance(end, type(first)) or start > end:
        st.error("Période invalide.")
        return
    in_period = [item for item in observations if start <= acquisition_day(item) <= end]
    labels = {observation_label(item): item for item in in_period}
    chosen_labels = st.multiselect(f"Acquisitions de la série ({MIN_SERIES_DATES} minimum)", list(labels),
                                   default=list(labels), key="series_selection")
    chosen = [labels[label] for label in chosen_labels]
    st.caption(f"{len(in_period)} acquisition(s) locale(s) vérifiée(s) dans la période. "
               "Pour en ajouter, utiliser le catalogue des acquisitions.")
    if len(chosen) < MIN_SERIES_DATES:
        st.info(f"Sélectionner au moins {MIN_SERIES_DATES} acquisitions.")
        return
    period = (start, end)
    try:
        key = local_series_key(chosen, data_dir, profile, period, gap)
    except LocalPreviewError as error:
        show_error(error, context="series.cle")
        return
    from src.components.resource_panel import assess_aoi_file, show_assessment

    if not show_assessment(assess_aoi_file(Path(data_dir) / "aoi" / f"{aoi_id}.geojson"),
                           operation="Calcul de la série"):
        return
    if st.button("Calculer la série", key="series_compute"):
        st.session_state["series_key"] = key
    if st.session_state.get("series_key") != key:
        st.info("Paramètres modifiés : cliquer sur « Calculer la série » pour lancer le calcul.")
        return
    try:
        with st.spinner("Vérification des empreintes et calcul du NDVI de chaque date…"):
            result = cached_series(key, tuple(chosen), str(data_dir), profile, period, float(gap))
    except LocalPreviewError as error:
        show_error(error, context="series.calcul")
        return

    status = result.interpretation.status.value
    st.warning(f"Statut scientifique : {STATUS_LABELS[status]}. {result.interpretation_scope}")
    for restriction in result.interpretation.restrictions:
        st.caption(restriction)
    retained = result.included_dates
    between = [gap.days for gap in result.gaps if gap.kind == "between_dates"]
    metrics = st.columns(4)
    metrics[0].metric("Dates retenues", f"{len(retained)} / {len(result.dates)}")
    metrics[1].metric("Couverture pixels communs / AOI", f"{format_percent(result.common_coverage_percent)} %")
    metrics[2].metric("Pixels communs", format_pixels(result.common_pixel_count))
    metrics[3].metric("Intervalle max. entre dates retenues", f"{format_percent(max(between))} j" if between else "—")
    _charts(result)
    st.dataframe([{
        "Acquisition (UTC)": f"{item.acquired_at_utc:%d/%m/%Y %H:%M:%S}", "Produit": item.product_id,
        "Saison": item.season_id or "non évaluée", "Retenue": "oui" if item.included else "non",
        "Couverture valide (%)": format_percent(item.valid_coverage_percent),
        "NDVI moyen (date)": format_ndvi(item.own.mean), "NDVI médian (date)": format_ndvi(item.own.median),
        "Pixels (date)": format_pixels(item.own.count),
        "NDVI moyen (communs)": format_ndvi(item.common.mean if item.common else None),
        "NDVI médian (communs)": format_ndvi(item.common.median if item.common else None),
        "Motif d’exclusion": item.exclusion_reason or "",
    } for item in result.dates], hide_index=True, width="stretch")
    st.subheader("Dates manquantes, exclues ou lacunes")
    lines = _missing(result, in_period, chosen)
    for line in lines or ["Aucune date exclue ni lacune au-delà du seuil."]:
        st.caption(line)
    st.download_button("Exporter la série (CSV avec provenance)", series_csv(result),
                       file_name=f"geonaf_ndvi_series_{profile.aoi_id}_{start:%Y%m%d}_{end:%Y%m%d}.csv",
                       mime="text/csv", key="series_csv")
    st.caption(f"Profil {profile.profile_id} · {profile.version} · empreinte {profile.fingerprint[:16]}… · "
               f"SCL {result.scl_policy} · grille {result.grid.crs.to_string()} "
               f"{result.grid.resolution[0]:g} m, {result.grid.width}×{result.grid.height} · "
               f"AOI {profile.aoi_id} sha256 {result.aoi_sha256[:16]}…. Dates d’acquisition, pas d’ingestion. "
               "Statistiques par pixel touché par l’AOI ; couvertures pondérées par l’intersection exacte.")
