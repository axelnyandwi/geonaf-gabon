"""Streamlit presentation of the multisource demonstrator; engine logic stays in services."""

from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from src.components.errors import show_error, show_internal_error
from src.components.formatting import format_ndvi, format_percent, format_pixels
from src.components.analysis_settings import TERRITORY_LABELS, choose_analysis, choose_territory
from src.geo.aoi_identity import load_aoi_identity
from src.services.external_layers import latest_osm, layers_for_aoi, open_store, stored_layers
from src.services.local_preview import (
    LocalPreviewError, analyze_local_group, analyze_local_observation, group_local_observations,
    list_local_observations, local_group_key, local_observation_key,
)

NATURE_HELP = {
    "Observation mesurée": "Grandeur dérivée d'une mesure satellitaire à une date connue.",
    "Classification externe": "Produit d'un modèle de classification tiers, à une autre date.",
    "Base collaborative": "Objets cartographiés par des contributeurs ; complétude et date de terrain inconnues.",
    "Interprétation": "Hypothèse rédigée à partir des statistiques ; non validée.",
}


@st.cache_resource(max_entries=4, show_spinner=False)
def cached_observation(key, _observation, data_dir, _profile):
    # One entry per (date revision, AOI digest, profile): see local_observation_key.
    return analyze_local_observation(_observation, Path(data_dir), scientific_profile=_profile)


@st.cache_resource(max_entries=6, show_spinner=False)
def cached_group(key, _group, data_dir, _profile):
    # One entry per (acquisition group revisions, AOI digest, effective profile): see local_group_key.
    return analyze_local_group(_group, Path(data_dir), scientific_profile=_profile)


@st.cache_resource(max_entries=3, show_spinner=False)
def cached_osm(storage_key, fingerprint, data_dir, bounds):
    from src.data.openstreetmap import load_osm_snapshot

    store, _ = open_store(Path(data_dir))
    return load_osm_snapshot(store, storage_key, bounds)


@st.cache_resource(max_entries=6, show_spinner=False)
def cached_radar(fingerprints, _layers, aoi_path, aoi_geometry_sha256, landcover_fingerprints, _landcover, scale):
    # The AOI geometry fingerprint is part of the key: a modified zone file never reuses old statistics.
    from src.services.sentinel1_layer import load_radar_mosaic

    classes = dict(_landcover[0].descriptor.classes) if _landcover else None
    return load_radar_mosaic([(layer.path, layer.descriptor) for layer in _layers], aoi_path=aoi_path, scale=scale,
                             landcover_path=[layer.path for layer in _landcover] or None, landcover_classes=classes)


@st.cache_resource(max_entries=4, show_spinner=False)
def cached_multisource(key, _observation, aoi_path, _worldcover, _osm, _radar):
    from src.services.multisource_demo import analyze_multisource

    return analyze_multisource(_observation, aoi_path=aoi_path, worldcover=_worldcover, osm=_osm, radar=_radar)


@st.cache_resource(max_entries=8, show_spinner=False)
def cached_map_payload(key, filters, _result, aoi_path):
    from src.services.multisource_map import prepare_multisource_map

    classes, groups, polarization = filters
    return prepare_multisource_map(_result, aoi_path=aoi_path, worldcover_classes=set(classes),
                                   osm_groups=set(groups), radar_polarization=polarization)


@st.cache_resource(show_spinner=False)
def _renderer():
    from streamlit.components.v2 import component

    folder = Path(__file__).parent / "maplibre"
    vendor = (folder / "vendor" / "maplibre-gl.js").read_text(encoding="utf-8")
    module = (folder / "multisource.js").read_text(encoding="utf-8")
    css = (folder / "vendor" / "maplibre-gl.css").read_text(encoding="utf-8") + "\n" + \
        (folder / "multisource.css").read_text(encoding="utf-8")
    adapter = """
export default function(component) {
  const {parentElement, data, setStateValue} = component;
  let view;
  try {
    view = mountMultisourceMap(parentElement, data, maplibregl, () => setStateValue('error', true));
  } catch {
    setStateValue('error', true);
  }
  return () => view?.dispose();
}
"""
    return component("geonaf_multisource_map", js=vendor + "\n" + module + "\n" + adapter, css=css)


def show_map(payload, key):
    result = _renderer()(data=payload, key=f"geonaf_multisource_{key}", height="content",
                         default={"error": False}, on_error_change=lambda: None)
    return not result.error


def _nature(label):
    st.caption(f"**Nature : {label}** — {NATURE_HELP[label]}")


def _retrieval_panel(data_dir, identity, month):
    from src.services import external_retrieval as retrieval

    with st.expander("Récupération des couches externes (réseau, sur action explicite)"):
        st.caption("Requêtes bornées à l'AOI, mises en cache et tracées. Aucune donnée existante n'est écrasée ; "
                   f"plafonds : WorldCover {retrieval.WORLDCOVER_MAX_BYTES // 2**20} Mo, OSM "
                   f"{retrieval.OSM_MAX_BYTES // 2**20} Mo, Sentinel-1 {retrieval.SENTINEL1_MAX_BYTES // 2**20} Mo "
                   "par polarisation (estimation préalable par l'index des tuiles COG).")
        aoi_id, bounds, digest = identity.aoi_id, identity.wgs84_bounds, identity.geometry_sha256
        columns = st.columns(3)
        action, report = None, None
        try:
            if columns[0].button("WorldCover (fenêtre AOI)", key="ms_fetch_wc"):
                action, report = "WorldCover", retrieval.retrieve_worldcover(data_dir, aoi_id=aoi_id,
                                                                             wgs84_bounds=bounds,
                                                                             aoi_geometry_sha256=digest)
            force = columns[1].checkbox("Forcer un nouvel instantané OSM", key="ms_osm_force",
                                        help="Sinon, un instantané de moins de 7 jours est réutilisé.")
            if columns[1].button("OpenStreetMap (Overpass)", key="ms_fetch_osm"):
                action, report = "OpenStreetMap", retrieval.retrieve_osm(data_dir, wgs84_bounds=bounds,
                                                                         force_refresh=force)
            if columns[2].button(f"Sentinel-1 VV/VH ({month:%m/%Y})", key="ms_fetch_s1"):
                action, report = "Sentinel-1", retrieval.retrieve_sentinel1(data_dir, aoi_id=aoi_id,
                                                                            wgs84_bounds=bounds, month=month,
                                                                            aoi_geometry_sha256=digest)
        except Exception as error:
            show_error(error, context="multisource.recuperation", prefix="Récupération impossible. ",
                       suffix=" Aucune donnée partielle n'a été enregistrée.")
            return
        if action:
            public = {k: v for k, v in report.items() if k != "storage_key"}
            st.success(f"{action} : {'réutilisé depuis le cache' if report.get('reused') else 'récupération terminée'}.")
            st.json(public, expanded=False)
            for name in ("cached_osm", "cached_radar", "cached_multisource", "cached_map_payload"):
                globals()[name].clear()


def _group_label(group):
    first = group[0]
    tiles = ", ".join(sorted({item.product_id.split("_")[5] for item in group if len(item.product_id.split("_")) > 5}))
    return f"{first.acquired_at:%d/%m/%Y · %H:%M UTC} · {len(group)} tuile(s) {tiles}"


def show_multisource(data_dir):
    st.title("Démonstrateur multisource")
    st.caption("Sentinel-2 × ESA WorldCover × OpenStreetMap, et Sentinel-1 présenté séparément. "
               "Croisements descriptifs et exploratoires sur une AOI commune.")
    st.warning("Aucun changement environnemental n'est validé ici : les sources ont des dates, résolutions et "
               "natures différentes. Une validation indépendante (terrain, imagerie très haute résolution) reste "
               "nécessaire.")
    columns = st.columns([1, 3])
    with columns[0]:
        aoi_id = choose_territory(data_dir, "ms_territory")
    if aoi_id is None:
        return
    with columns[1]:
        configuration = choose_analysis(aoi_id, "ms_analysis")
    if configuration is None:
        return
    profile = configuration.effective
    try:
        groups = group_local_observations(list_local_observations(data_dir))
    except LocalPreviewError as error:
        show_error(error, context="multisource.acquisitions")
        return
    if not groups:
        st.error("Aucune acquisition Sentinel-2 locale vérifiée : utiliser le catalogue des acquisitions.")
        return
    labels = {_group_label(group): group for group in groups}
    group = labels[st.selectbox("Acquisition Sentinel-2 (prise de vue, date d'acquisition UTC)", list(labels),
                                index=len(labels) - 1, key="ms_date")]
    aoi_path = (Path(data_dir) / "aoi" / f"{aoi_id}.geojson").resolve()
    try:
        identity = load_aoi_identity(aoi_path, aoi_id)
    except ValueError as error:
        show_internal_error(error, context="multisource.zone",
                            message="Zone d'étude refusée : géométrie illisible, vide ou non surfacique.")
        return
    bounds = identity.wgs84_bounds
    month = date(group[0].acquired_at.year, group[0].acquired_at.month, 1)
    _retrieval_panel(Path(data_dir), identity, month)

    from src.components.resource_panel import assess_aoi_file, show_assessment

    osm_stored = latest_osm(open_store(Path(data_dir))[1], bounds)
    osm_bytes = None
    if osm_stored is not None and str(osm_stored.descriptor.provenance.get("response_bytes", "")).isdigit():
        osm_bytes = int(osm_stored.descriptor.provenance["response_bytes"])   # Volume of the stored snapshot.
    if not show_assessment(assess_aoi_file(aoi_path, osm_wanted=True, osm_estimated_bytes=osm_bytes),
                           operation="Analyse multisource"):
        return
    try:
        key = local_group_key(group, data_dir, profile)
        with st.spinner("Vérification des empreintes et calcul du NDVI de la prise de vue…"):
            observation = cached_group(key, group, str(data_dir), profile)
        store, repository = open_store(Path(data_dir))
        worldcover = list(layers_for_aoi(stored_layers(repository, "esa-worldcover", bounds,
                                                       observation.acquired_at_utc), identity))
        if worldcover:  # One product version only (closest validity first).
            version = worldcover[0].descriptor.version
            worldcover = [layer for layer in worldcover if layer.descriptor.version == version]
        osm_layer = latest_osm(repository, bounds)
        monthly = [layer for layer in stored_layers(repository, "sentinel-1-mosaic", bounds,
                                                    observation.acquired_at_utc)
                   if layer.descriptor.temporal.start_utc.strftime("%Y-%m") == f"{month:%Y-%m}"]
        radar_layers = [layer for polarization in ("VV", "VH") for layer in layers_for_aoi(
            [l for l in monthly if l.descriptor.provenance.get("polarization") == polarization], identity)]
    except LocalPreviewError as error:
        show_error(error, context="multisource.prise_de_vue")
        return
    except ValueError as error:
        show_internal_error(error, context="multisource.couches",
                            message="Couche externe locale invalide (empreinte modifiée) : affichage interrompu.")
        return
    with st.spinner("Croisement des sources…"):
        osm = cached_osm(osm_layer.storage_key, osm_layer.descriptor.fingerprint, str(data_dir), bounds) \
            if osm_layer else None
        wc_fingerprints = tuple(layer.descriptor.fingerprint for layer in worldcover)
        radar = []
        for polarization in ("VV", "VH"):
            layers = [layer for layer in radar_layers if layer.descriptor.provenance.get("polarization") == polarization]
            if layers:
                radar.append(cached_radar(tuple(layer.descriptor.fingerprint for layer in layers), layers,
                                          str(aoi_path), identity.geometry_sha256, wc_fingerprints, worldcover,
                                          "intensity"))
        result_key = (key, wc_fingerprints, osm_layer.descriptor.fingerprint if osm_layer else None,
                      tuple(layer.descriptor.fingerprint for layer in radar_layers))
        result = cached_multisource(result_key, observation, str(aoi_path),
                                    [(layer.path, layer.descriptor) for layer in worldcover] or None, osm,
                                    tuple(radar))
    if result.missing:
        st.info(f"Sources absentes localement pour {TERRITORY_LABELS[aoi_id]} : " + ", ".join(result.missing)
                + ". Elles peuvent être récupérées (volume estimé et plafonné) dans le panneau ci-dessus ; "
                "l'analyse se poursuit avec les sources disponibles.")
    with st.expander("Provenance de la configuration d'analyse"):
        st.json(configuration.provenance, expanded=False)

    _indicators(result)
    for warning in result.warnings:
        st.warning(warning)

    st.subheader("Carte interactive")
    filters = st.columns(3)
    classes = {z.code: z.label for z in result.worldcover.zones if z.pixel_count} if result.worldcover else {}
    chosen_classes = filters[0].multiselect("Classes WorldCover affichées", list(classes), default=list(classes),
                                            format_func=classes.get, key="ms_classes")
    groups = sorted(set(result.osm_frame["group"])) if result.osm_frame is not None else []
    chosen_groups = filters[1].multiselect("Groupes OSM affichés", groups, default=groups, key="ms_groups")
    polarizations = [layer.polarization for layer in result.radar]
    polarization = filters[2].radio("Couche radar", ["Aucune"] + polarizations, key="ms_pol", horizontal=True)
    filter_key = (tuple(sorted(chosen_classes)), tuple(sorted(chosen_groups)),
                  "none" if polarization == "Aucune" else polarization)  # "none" matches no radar layer.
    try:
        payload = cached_map_payload(result_key, filter_key, result, str(aoi_path))
        if not show_map(payload, abs(hash((result_key, filter_key))) % 10**8):
            st.warning("Carte interactive indisponible dans ce navigateur ; les tableaux restent valides.")
    except Exception as error:
        show_internal_error(error, context="multisource.carte", level="warning",
                            message="Carte interactive indisponible ; les tableaux restent valides.")
    st.caption("Filtres d'affichage uniquement : les statistiques ci-dessous portent sur toutes les classes. "
               "Activer les couches dans la barre au-dessus de la carte.")

    tabs = st.tabs(["Occupation du sol × NDVI", "Infrastructures OSM", "Radar Sentinel-1",
                    "Comparabilité et provenance", "Interprétations"])
    with tabs[0]:
        _landcover_tab(result, set(chosen_classes))
    with tabs[1]:
        _infrastructure_tab(result, set(chosen_groups))
    with tabs[2]:
        _radar_tab(result, set(chosen_classes))
    with tabs[3]:
        _provenance_tab(result)
    with tabs[4]:
        _nature("Interprétation")
        for sentence in result.interpretations or ("Aucune interprétation générée.",):
            st.markdown(f"- *Hypothèse :* {sentence}")
        st.caption(result.scope)


def _indicators(result):
    observation = result.observation
    metrics = st.columns(5)
    metrics[0].metric("NDVI médian (pixels valides)", format_ndvi(observation.summary.median))
    metrics[1].metric("Couverture NDVI valide / AOI", f"{format_percent(observation.valid_coverage_percent)} %")
    buildings = next((i for i in result.infrastructure if i.group == "Bâtiments"), None)
    roads = next((i for i in result.infrastructure if i.group == "Routes et voies"), None)
    metrics[2].metric("Bâtiments OSM (AOI)", format_pixels(buildings.features) if buildings else "—")
    metrics[3].metric("Voies OSM (km, AOI)", f"{roads.length_km:,.0f}".replace(",", " ") if roads else "—")
    vv = next((layer for layer in result.radar if layer.polarization == "VV"), None)
    metrics[4].metric("VV médian (dB)", f"{vv.median_db:.1f}" if vv else "—")


def _landcover_tab(result, chosen):
    if result.worldcover is None:
        st.info("WorldCover absent localement.")
        return
    _nature("Observation mesurée")
    _nature("Classification externe")
    rows = [{"Classe WorldCover": z.label, "Code": z.code, "Pixels NDVI valides": z.pixel_count,
             "Surface (ha)": round((z.area_m2 or 0) / 1e4, 1), "NDVI moyen": z.mean, "NDVI médian": z.median}
            for z in result.worldcover.zones if z.pixel_count and z.code in chosen]
    frame = pd.DataFrame(rows)
    if not frame.empty:
        st.altair_chart(alt.Chart(frame).mark_bar().encode(
            x=alt.X("NDVI médian:Q", scale=alt.Scale(domain=[-0.2, 1])),
            y=alt.Y("Classe WorldCover:N", sort="-x"),
            tooltip=list(frame.columns)), width="stretch")
        st.dataframe(frame.style.format({"NDVI moyen": "{:.4f}", "NDVI médian": "{:.4f}"}), hide_index=True,
                     width="stretch")
    join = result.worldcover
    st.caption(f"{format_pixels(join.layer_coverage_pixels)} pixels NDVI valides classés ; "
               f"{format_pixels(join.unmatched_pixels)} sans classe. {join.temporal.message} {join.scope} "
               "Surfaces : intersection exacte cellule/AOI. Classes peu représentées : statistiques instables.")


def _infrastructure_tab(result, chosen):
    if not result.infrastructure:
        st.info("OpenStreetMap absent localement.")
        return
    _nature("Base collaborative")
    st.dataframe(pd.DataFrame([{
        "Groupe": item.group, "Objets (AOI)": item.features,
        "Surface (ha)": None if item.area_ha is None else round(item.area_ha, 1),
        "Longueur (km)": None if item.length_km is None else round(item.length_km, 1),
        "Pixels NDVI valides touchés": item.ndvi.pixel_count, "NDVI médian": item.ndvi.median,
    } for item in result.infrastructure if item.group in chosen]), hide_index=True, width="stretch")
    provenance = result.osm_descriptor.provenance
    st.caption(f"Instantané base OSM : {provenance['osm_base_utc']} ; objets retenus {provenance['features']} "
               f"(chemins OSM « ways » {provenance['ways']}, relations multipolygones {provenance['relations']}) ; rejetés : incomplets "
               f"{provenance['rejected_incomplete']}, invalides {provenance['rejected_invalid']}, non pris en charge "
               f"{provenance['rejected_unsupported']}. Groupes non exclusifs (un pixel peut appartenir à plusieurs). "
               "Surfaces et longueurs découpées à l'AOI en UTM 32N.")


def _radar_tab(result, chosen):
    if not result.radar:
        st.info("Aucune mosaïque Sentinel-1 locale pour le mois de l'acquisition Sentinel-2.")
        return
    _nature("Observation mesurée")
    st.info("La rétrodiffusion radar mesure la rugosité, la structure et l'humidité de surface ; elle n'est pas "
            "comparée au NDVI. Les statistiques sont présentées par classe WorldCover, sur la grille radar native.")
    rows = []
    for layer in result.radar:
        for zone in layer.per_class:
            if zone.pixel_count and zone.code in chosen:
                rows.append({"Polarisation": layer.polarization, "Classe WorldCover (mode 10→20 m)": zone.label,
                             "Pixels 20 m": zone.pixel_count, "Médiane (dB)": round(zone.median, 2),
                             "Moyenne (dB)": round(zone.mean, 2)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    for layer in result.radar:
        st.caption(f"{layer.polarization} · {layer.descriptor.product} · {layer.unit} · médiane AOI "
                   f"{layer.median_db:.2f} dB (Q1 {layer.p25_db:.2f}, Q3 {layer.p75_db:.2f}) · couverture "
                   f"{format_percent(layer.coverage_percent)} % des cellules AOI · invalides {layer.invalid_counts}.")
    from src.data.sentinel1 import RADIOMETRY

    st.caption(f"Radiométrie ({RADIOMETRY['version']}) : γ⁰ aplani du relief en puissance linéaire, "
               f"{RADIOMETRY['db_formula']} — {RADIOMETRY['status']}. {RADIOMETRY['inconsistency']} "
               "Valeurs descriptives : aucune qualification quantitative validée.")
    with st.expander("Sources de la convention radar"):
        for source in RADIOMETRY["evidence"]:
            st.markdown(f"- {source}")
    for layer in result.radar:
        for note in layer.mosaic_notes:
            st.caption(f"{layer.polarization} : {note}")


def _provenance_tab(result):
    st.dataframe(pd.DataFrame([{
        "Source": s.source, "Nature": s.nature, "Produit": s.product, "Date / validité": s.temporal_label,
        "Écart avec S2 (jours)": None if s.offset_days is None else round(s.offset_days, 1),
        "Relation": {"reference": "référence", "within": "dans la validité", "before_layer": "S2 antérieure",
                     "after_layer": "S2 postérieure"}[s.relation],
        "Résolution": s.resolution, "CRS": s.crs,
        "Couverture AOI (%)": None if s.coverage_percent is None else round(s.coverage_percent, 2),
        "Tuiles / produits": ", ".join(s.tiles),
    } for s in result.sources]), hide_index=True, width="stretch")
    for source in result.sources:
        with st.expander(f"Limites et licence · {source.source}"):
            for limit in source.limits:
                st.markdown(f"- {limit}")
            st.caption(f"Attribution : {source.attribution}")
    observation = result.observation
    st.caption(f"Sentinel-2 : {observation.product_id} · grille {observation.grid.crs.to_string()} "
               f"{observation.grid.resolution[0]:g} m, {observation.grid.width}×{observation.grid.height} · SCL "
               f"{observation.scl_policy} · calibration {observation.calibration} · AOI sha256 "
               f"{observation.aoi_sha256[:16]}…. Licences : WorldCover CC BY 4.0 ; OpenStreetMap ODbL 1.0 "
               "(partage à l'identique des bases dérivées) ; Copernicus Sentinel : avis légal Copernicus.")
