"""Territory selection, boundary comparison and GeoJSON import page; logic lives in services."""

import hashlib
from pathlib import Path

import pandas as pd
import streamlit as st

from src.components.formatting import format_percent, format_surface
from src.components.resource_panel import assess_geometry, show_assessment
from src.services import territory_catalog as catalog

KINDS = ("Zone d'étude GEONAF (zone d'acquisition)", "Territoire administratif (référentiel)",
         "Zone personnalisée importée")
SESSION_KEY = "geonaf_territory"


@st.cache_resource(show_spinner=False)
def _renderer():
    from streamlit.components.v2 import component

    folder = Path(__file__).parent / "maplibre"
    vendor = (folder / "vendor" / "maplibre-gl.js").read_text(encoding="utf-8")
    module = (folder / "territory.js").read_text(encoding="utf-8")
    css = (folder / "vendor" / "maplibre-gl.css").read_text(encoding="utf-8") + "\n" + \
        (folder / "territory.css").read_text(encoding="utf-8")
    adapter = """
export default function(component) {
  const {parentElement, data, setStateValue} = component;
  let view;
  try {
    view = mountTerritoryMap(parentElement, data, maplibregl, () => setStateValue('error', true));
  } catch {
    setStateValue('error', true);
  }
  return () => view?.dispose();
}
"""
    return component("geonaf_territory_map", js=vendor + "\n" + module + "\n" + adapter, css=css)


def show_map(payload, key) -> None:
    try:
        result = _renderer()(data=payload, key=f"geonaf_territory_{key}", height="content",
                             default={"error": False}, on_error_change=lambda: None)
        if result.error:
            st.warning("Carte interactive indisponible dans ce navigateur ; les chiffres restent valides.")
    except Exception:
        st.warning("Carte interactive indisponible ; les chiffres restent valides.")


@st.cache_data(max_entries=64, show_spinner=False)
def _payload(kind, key, digest, _options):
    if kind == "selection":
        return catalog.selection_payload(_options[0])
    return catalog.comparison_payload(*_options)


def _digest(option) -> str:
    if option.record is not None:
        return option.record.fingerprint
    return option.territory_id + ":" + hashlib.sha256(option.geometry_path.read_bytes()).hexdigest()


def _provenance(option) -> dict:
    record = option.record
    if record is None:
        return {"Nom": option.label, "Type": "Zone d'acquisition GEONAF", "Surface (km²)": format_surface(option.area_km2),
                "Source": "GEONAF (carré de travail 12 × 12 km)", "Millésime": "—", "Licence": "—",
                "Statut": option.status_text}
    source = record.source
    return {
        "Nom": record.label, "Type": "Unité administrative" if record.kind == "admin_unit" else "Zone importée",
        "Niveau (type local déclaré)": (f"{record.admin.level_code} ({record.admin.local_type})"
                                        if record.admin else "—"),
        "Surface (km²)": format_surface(record.area_km2), "Source": f"{source.product} — {source.attribution}",
        "Version / production": source.version, "Millésime des limites": source.reference_year or "non précisé",
        "Licence": source.license_spdx + (" (partage à l'identique)" if source.share_alike else ""),
        "Récupération": source.retrieved_at_utc or "—", "Statut": option.status_text,
        "Identifiant / version": f"{record.territory_id} v{record.version}",
    }


def _provenance_table(option):
    st.table(pd.DataFrame({"Attribut": list(_provenance(option)), "Valeur": [str(v) for v in
                                                                            _provenance(option).values()]}))
    if option.record is not None:
        for limit in option.record.limits:
            st.caption(f"- {limit}")


def _load_button(data_dir, release, level, key):
    if st.button(f"Charger {catalog.REFERENTIALS[release]} — {catalog.LEVELS[level]} (réseau)", key=key):
        try:
            with st.spinner("Récupération bornée geoBoundaries (métadonnées, puis géométries si absentes)…"):
                report = catalog.load_referential(Path(data_dir), release, level)
        except Exception as error:
            message = str(error) if type(error).__name__ == "GeoBoundariesError" else type(error).__name__
            st.error(f"Chargement impossible : {message}. Aucune donnée partielle n'a été enregistrée.")
            return
        st.success(f"{len(report.registered)} unité(s) enregistrée(s), {len(report.rejected)} rejetée(s) ; "
                   + ("instantané local réutilisé." if report.snapshot.reused else
                      f"{report.snapshot.network_bytes / 1024:,.0f} Ko téléchargés.".replace(",", " ")))
        for name, reason in report.rejected:
            st.caption(f"Unité rejetée (non réparée) : {name} — {reason}")
        st.rerun()


def _guard(option):
    geometry = catalog.geometry_wgs84(option)
    assessment = assess_geometry(geometry, _digest(option), osm_wanted=True)
    show_assessment(assessment, operation="Analyse multisource de ce territoire", prominent_warnings=True)


def _selection(data_dir):
    kind = st.radio("Type de zone", KINDS, key="territory_kind")
    option = None
    if kind == KINDS[0]:
        zones = catalog.historical_zones(data_dir)
        if not zones:
            st.info("Aucune zone d'acquisition locale.")
            return
        option = st.selectbox("Zone d'étude", zones, format_func=lambda o: o.label, key="territory_zone")
        st.info("Zone d'acquisition GEONAF : carré de 12 × 12 km utilisé pour les acquisitions Sentinel-2. "
                "Ce n'est ni une commune ni une autre unité administrative.")
    elif kind == KINDS[1]:
        columns = st.columns(2)
        release = columns[0].radio("Référentiel", list(catalog.REFERENTIALS), format_func=catalog.REFERENTIALS.get,
                                   key="territory_release")
        level = columns[1].selectbox("Niveau", list(catalog.LEVELS), index=2, format_func=catalog.LEVELS.get,
                                     key="territory_level")
        options = catalog.admin_territories(data_dir, release, level)
        if not options:
            st.info("Référentiel non chargé localement. Le chargement est une action explicite : environ 50 à 450 Ko, "
                    "mis en cache ; aucun téléchargement n'a lieu au simple changement d'option.")
            _load_button(data_dir, release, level, f"load_{release}_{level}")
            return
        option = st.selectbox(f"Territoire ({len(options)} unités)", options, format_func=lambda o: o.label,
                              key=f"territory_unit_{release}_{level}")
        st.warning(option.status_text + " Aucune commune n'est publiée par geoBoundaries pour le Gabon : "
                   "Akanda et Owendo n'y existent pas comme unités.")
    else:
        options = catalog.custom_zones(data_dir)
        if not options:
            st.info("Aucune zone importée : utiliser l'onglet « Import GeoJSON ».")
            return
        option = st.selectbox("Zone importée", options, format_func=lambda o: o.label, key="territory_custom")
        st.warning(option.status_text)
    st.session_state[SESSION_KEY] = option.key
    left, right = st.columns([3, 2])
    with left:
        show_map(_payload("selection", option.key, _digest(option), (option,)), option.key)
    with right:
        _provenance_table(option)
    _guard(option)


def _comparison(data_dir):
    st.warning("Comparaison de deux représentations des limites d'un même territoire : un écart n'est jamais un "
               "changement temporel réel. Les deux contours restent distincts.")
    level = st.selectbox("Niveau", list(catalog.LEVELS), index=2, format_func=catalog.LEVELS.get,
                         key="compare_level")
    missing = [release for release in catalog.REFERENTIALS if not catalog.admin_territories(data_dir, release, level)]
    for release in missing:
        st.info(f"{catalog.REFERENTIALS[release]} non chargé à ce niveau.")
        _load_button(data_dir, release, level, f"compare_load_{release}_{level}")
    if missing:
        return
    pairs, only_open, only_humanitarian = catalog.referential_pairs(data_dir, level)
    if not pairs:
        st.info("Aucune paire d'unités de même nom normalisé.")
        return
    names = [a.label for a, _ in pairs]
    index = names.index("Libreville") if "Libreville" in names else 0
    a, b = pairs[st.selectbox("Unité (paires par nom normalisé, à vérifier)", range(len(pairs)), index=index,
                                format_func=lambda i: names[i], key="compare_pair")]
    from src.services.territory_comparison import compare_territories
    from src.data.territory_registry import TerritoryRegistry

    result = compare_territories(TerritoryRegistry(Path(data_dir)), a.territory_id, b.territory_id)
    metrics = st.columns(5)
    metrics[0].metric("gbOpen (km²)", format_surface(result.area_a_km2))
    metrics[1].metric("gbHumanitarian (km²)", format_surface(result.area_b_km2))
    metrics[2].metric("Partie commune (km²)", format_surface(result.intersection_km2))
    metrics[3].metric("Différence symétrique (km²)", format_surface(result.symmetric_difference_km2))
    metrics[4].metric("Recouvrement (%)", format_percent(result.overlap_percent),
                      help="Partie commune / union des deux représentations (indice de Jaccard).")
    st.caption(f"gbOpen couvert par gbHumanitarian : {format_percent(result.a_covered_by_b_percent)} % · "
               f"gbHumanitarian couvert par gbOpen : {format_percent(result.b_covered_by_a_percent)} % · "
               f"écart de surface (gbHumanitarian − gbOpen) / gbOpen : "
               f"{format_percent(result.relative_area_difference_percent)} %. {result.method}")
    left, right = st.columns([3, 2])
    with left:
        show_map(_payload("comparison", f"{a.key}|{b.key}", _digest(a) + _digest(b), (a, b)), f"cmp_{a.key}")
    with right:
        st.dataframe(pd.DataFrame([_provenance(a), _provenance(b)]).astype(str).T.rename(
            columns={0: "gbOpen", 1: "gbHumanitarian"}), width="stretch")
    for note in result.notes:
        st.caption(f"- {note}")
    st.caption(result.nature)
    with st.expander(f"Unités sans correspondance par nom : {len(only_open)} gbOpen, {len(only_humanitarian)} "
                     "gbHumanitarian", expanded=bool(only_open or only_humanitarian)):
        st.markdown("**gbOpen seulement :** " + (", ".join(o.label for o in only_open) or "aucune"))
        st.markdown("**gbHumanitarian seulement :** " + (", ".join(o.label for o in only_humanitarian) or "aucune"))
        st.caption("Orthographes différentes ou unités propres à un référentiel : aucun rapprochement automatique.")


def _import(data_dir):
    st.caption("Une zone par opération. Le fichier est vérifié (taille avant lecture, géométrie valide sans "
               "réparation, nombre de sommets et de parties) puis enregistré avec sa provenance. Statut : "
               "« fourni par l'utilisateur » — jamais officiel automatiquement.")
    upload = st.file_uploader("Fichier GeoJSON", type=["geojson", "json"], key="import_file")
    with st.form("import_form"):
        label = st.text_input("Nom de la zone")
        author = st.text_input("Auteur ou organisme source")
        license_spdx = st.text_input("Licence ou droits de réutilisation (ex. CC-BY-4.0)")
        source_date = st.text_input("Date ou version de la source (ex. 2026-10-23)")
        rights = st.checkbox("Je dispose des droits de réutilisation déclarés pour ce fichier.")
        submitted = st.form_submit_button("Valider et enregistrer la zone")
    if not submitted:
        return
    if upload is None:
        st.error("Aucun fichier sélectionné.")
        return
    if not rights:
        st.error("Les droits de réutilisation doivent être confirmés.")
        return
    try:
        record, _ = catalog.import_custom_zone(Path(data_dir), upload.getvalue(), label=label, author=author,
                                               license_spdx=license_spdx, source_date=source_date)
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else "fichier illisible"
        st.error(f"Import refusé : {message}")
        return
    st.success(f"Zone « {record.label} » enregistrée ({format_surface(record.area_km2)} km², version "
               f"{record.version}, statut « {record.legal_status} »).")


def show_territories(data_dir):
    st.title("Territoires et frontières")
    st.caption("Choisir un territoire et comprendre l'origine de ses limites. GEONAF observe toute zone, "
               "qu'une frontière officielle existe ou non ; zone d'acquisition, unité administrative et zone "
               "importée restent distinctes.")
    tabs = st.tabs(["Sélection", "Comparaison des référentiels", "Import GeoJSON"])
    with tabs[0]:
        _selection(data_dir)
    with tabs[1]:
        _comparison(data_dir)
    with tabs[2]:
        _import(data_dir)
