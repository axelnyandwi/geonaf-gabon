"""« Comparaison temporelle sur surface commune » sub-section; calculations live in src.indicators.s2_pairs.

Nothing is computed at the initial load: the reader starts the comparison
explicitly, once per session and zone. The aggregated selection (a few numbers
per pair, no raster) is cached per process on ``s2_pairs_key`` (zone digest,
file identities and manifest digests of the acquisitions, scientific,
vegetation and transition profiles, evaluation limit, requested pair), so a
second session or a change of pair already computed is a cache hit. A pair
that the default evaluation skipped is shown without any figure until the
reader asks for it. Resource guards run before every computation.
"""

from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st

from src.components.errors import show_error
from src.components.analysis_settings import TERRITORY_LABELS
from src.components.formatting import (
    format_hectares_from_km2, format_ndvi, format_percent, format_surface, french_table,
)
from src.components.resource_panel import assess_aoi_file, show_assessment
from src.components.s2_vegetation import VEGETATION_PROFILE, YEAR, cached_inventory
from src.indicators.assembly import build_s2_transition_indicators
from src.indicators.presentation import attribution, fr
from src.indicators.provenance import parse_utc
from src.indicators.profiles import S2_NDVI_TRANSITION_V0
from src.indicators.s2_inventory import s2_inventory_key
from src.indicators.s2_pairs import (
    NOT_EVALUATED, PUBLISHABLE, REJECTED, pair_provenance, s2_pair_selection, s2_pairs_key,
)

TRANSITION_PROFILE = S2_NDVI_TRANSITION_V0
TITLE = "Comparaison temporelle sur surface commune"
NO_CAUSE = ("Une transition de classe spectrale ne démontre ni une déforestation, ni un reboisement, ni une "
            "modification réelle de l'occupation du sol : elle indique seulement qu'un pixel observé aux deux dates "
            "est passé d'un côté à l'autre d'un seuil NDVI.")
TABLE_DECIMALS = {"Surface (km²)": 2, "Surface (ha)": 1, "Part de la surface commune (%)": 2,
                  "Borne supérieure de couverture commune (%)": 2, "Vers NDVI < seuil (km²)": 2,
                  "Vers NDVI ≥ seuil (km²)": 2, "Resté ≥ seuil (km²)": 2, "Resté < seuil (km²)": 2,
                  "Solde (km²)": 2}
STATUS_LABELS = {PUBLISHABLE: "publiable", "non publiable": "non publiable", REJECTED: "écartée avant calcul",
                 NOT_EVALUATED: "non calculée"}


@st.cache_resource(max_entries=8, show_spinner=False)
def cached_selection(key, data_dir, aoi_id, only, _inventory):
    # Guards already checked by the caller (with the host's or container's free memory); the key identifies
    # the inventory (``_inventory`` is not hashed) through ``s2_inventory_key`` inside ``s2_pairs_key``.
    return s2_pair_selection(Path(data_dir), aoi_id, inventory=_inventory, year=YEAR, profile=TRANSITION_PROFILE,
                             only=only, available_bytes=None)


def _when(instant):
    return f"{instant:%d/%m/%Y %H:%M} UTC"


def _of(label):
    return f"d'{label}" if label[:1].lower() in "aeiouyh" else f"de {label}"


def _option(pair):
    # Short enough for a 390 px wide phone; the reason and the bound are shown once the pair is selected.
    return f"{pair.label} · {'écartée' if pair.status == REJECTED else STATUS_LABELS.get(pair.status, pair.status)}"


def _thresholds(aoi_id):
    from src.geo.scientific_profile import REFERENCE_PROFILES

    variation = REFERENCE_PROFILES[aoi_id].change_threshold
    st.caption(
        f"**Seuil de classe NDVI {fr(TRANSITION_PROFILE.threshold)}** — à chaque date, un pixel observé est classé "
        f"NDVI ≥ {fr(TRANSITION_PROFILE.threshold)} ou NDVI < {fr(TRANSITION_PROFILE.threshold)} ; une transition "
        f"est un changement de classe entre les deux dates. Hypothèse de travail non calibrée.  \n"
        f"**Seuil de variation ΔNDVI {fr(variation, 2)}** — utilisé par les analyses scientifiques historiques "
        f"(section « Analyses scientifiques ») pour qualifier une variation du NDVI entre deux dates ; il "
        "n'intervient pas dans cette comparaison.")


def _card(caption, value, unit, lines):
    with st.container(border=True):
        st.caption(caption)
        st.markdown(f"<div style='font-size:1.5rem;font-weight:600;line-height:1.2'>{escape(value)} "
                    f"<span style='font-size:1rem;font-weight:400'>{escape(unit)}</span></div>",
                    unsafe_allow_html=True)
        st.caption("  \n".join(lines))


def _pair_header(pair, aoi_id):
    label = TERRITORY_LABELS.get(aoi_id, aoi_id)
    st.caption(f"Paire : **{_when(pair.earlier.acquired_at_utc)} → {_when(pair.later.acquired_at_utc)}** · "
               f"intervalle {fr(pair.interval_days, 1)} jours  \n"
               f"Zone d'étude {label} (carré d'acquisition de 12 × 12 km) : ce n'est pas la commune administrative "
               f"{_of(label)}.")


def _published(pair, indicators):
    m, t = pair.measure, pair.measure.transitions
    common, delta, _ = indicators
    km2 = lambda m2: m2 / 1e6
    share = lambda m2: format_percent(100.0 * m2 / m.common_m2)
    threshold = fr(TRANSITION_PROFILE.threshold)
    first, second = st.columns(2)
    with first:
        _card("Surface commune comparable", format_surface(common.value), "km²",
              [f"soit {format_hectares_from_km2(common.value)} ha",
               f"{format_percent(m.common_coverage_percent)} % de la zone d'étude"])
    with second:
        _card(f"NDVI ≥ {threshold} → NDVI < {threshold}", format_surface(km2(t.down_m2)), "km²",
              [f"soit {format_hectares_from_km2(km2(t.down_m2))} ha", f"{share(t.down_m2)} % de la surface commune"])
    first, second = st.columns(2)
    with first:
        _card(f"NDVI < {threshold} → NDVI ≥ {threshold}", format_surface(km2(t.up_m2)), "km²",
              [f"soit {format_hectares_from_km2(km2(t.up_m2))} ha", f"{share(t.up_m2)} % de la surface commune"])
    with second:
        _card("Variation moyenne pondérée du NDVI", format_ndvi(delta.value), "ΔNDVI",
              [f"NDVI moyen {format_ndvi(m.mean_1)} → {format_ndvi(m.mean_2)} (date 2 − date 1)",
               "moyenne pondérée par la surface de chaque pixel"])
    st.markdown(f":violet-badge[Estimation] :red-badge[Expérimental] "
                f":orange-badge[Fiabilité : {common.reliability}]")
    st.caption("  \n".join(pair.assessment.reasons + pair.assessment.warnings))


def _unpublished(pair):
    with st.container(border=True):
        st.caption("Comparaison de la paire sélectionnée")
        st.markdown("<div style='font-size:1.5rem;font-weight:600;line-height:1.2'>Non disponible</div>",
                    unsafe_allow_html=True)
        st.caption(" ".join(pair.reasons))
        st.markdown(":violet-badge[Estimation] :red-badge[Expérimental] :gray-badge[Non publiable]")
    st.caption("Aucune estimation territoriale n'est publiée pour cette paire.")


def _transitions_table(pair):
    m, t = pair.measure, pair.measure.transitions
    threshold = fr(TRANSITION_PROFILE.threshold)
    rows = [(f"NDVI ≥ {threshold} puis < {threshold}", t.down_m2,
             "Pixel au-dessus du seuil à la première date, au-dessous à la seconde."),
            (f"NDVI < {threshold} puis ≥ {threshold}", t.up_m2,
             "Pixel au-dessous du seuil à la première date, au-dessus à la seconde."),
            (f"Resté NDVI ≥ {threshold}", t.stayed_above_m2, "Au-dessus du seuil aux deux dates."),
            (f"Resté NDVI < {threshold}", t.stayed_below_m2, "Au-dessous du seuil aux deux dates.")]
    frame = pd.DataFrame([{"Catégorie": label, "Surface (km²)": m2 / 1e6, "Surface (ha)": m2 / 1e4,
                           "Part de la surface commune (%)": 100.0 * m2 / m.common_m2} for label, m2, _ in rows])
    st.dataframe(french_table(frame, TABLE_DECIMALS), hide_index=True, width="stretch")
    st.caption("Les quatre catégories partagent la surface commune sans recouvrement ; une valeur égale au seuil "
               "compte comme ≥ seuil.")
    for label, _, definition in rows:
        st.caption(f"**{label}** — {definition}")
    st.caption(f"Solde des surfaces de transitions (vers ≥ {threshold} moins vers < {threshold}) : "
               f"**{format_surface(t.net_m2 / 1e6)} km²**. Différence de surfaces de transitions : ce n'est ni une "
               "perte ni un gain réel de végétation.")


def _sensitivity_table(pair):
    rows = [{"Seuil": f"NDVI {fr(s.threshold)}", "Vers NDVI < seuil (km²)": s.down_m2 / 1e6,
             "Vers NDVI ≥ seuil (km²)": s.up_m2 / 1e6, "Resté ≥ seuil (km²)": s.stayed_above_m2 / 1e6,
             "Resté < seuil (km²)": s.stayed_below_m2 / 1e6, "Solde (km²)": s.net_m2 / 1e6}
            for s in pair.measure.sensitivity]
    st.dataframe(french_table(pd.DataFrame(rows), TABLE_DECIMALS), hide_index=True, width="stretch")
    st.caption("Même surface commune pour les trois seuils : l'écart mesure la dépendance au seuil, pas une "
               "incertitude statistique.")


def _blocks(pair):
    b = pair.measure.blocks
    size = fr(b.size_m * b.size_m / 1e6)
    st.caption(f"Cellules fixes de {size} km² dans une projection à surfaces équivalentes ({b.crs}, origine 0, 0), "
               "indépendantes de la grille de l'image. Chaque pixel est attribué en entier à la cellule qui "
               "contient son centre : attribution approximative en bordure de cellule. Une cellule est retenue "
               f"lorsque la zone d'étude en couvre au moins {format_percent(100 * b.min_zone_share)} %.")
    if not b.eligible_blocks:
        st.caption("Diagnostic non calculable : zone d'étude plus petite qu'une cellule de référence.")
    else:                                 # Three figures: plain lines stay readable on a phone, a table did not.
        st.markdown(f"- Cellules retenues : **{b.eligible_blocks}**\n"
                    f"- Avec au moins {fr(100 * b.common_min_share, 0)} % de surface commune : "
                    f"**{format_percent(100.0 * b.covered_share)} %** des cellules\n"
                    f"- Sans surface commune : **{format_percent(100.0 * b.empty_share)} %** des cellules")
    st.caption("Information seulement : ce diagnostic montre où la surface commune se répartit, il ne prouve pas "
               "que la surface commune est représentative de la zone d'étude et ne conditionne pas la publication.")


def _provenance(pair, selection, indicator):
    facts = pair_provenance(pair, selection)
    profiles = facts["profiles"]
    lines = [f"**Nature** : estimation ; statut scientifique **{facts['scientific_status']}** (méthode non validée)."]
    for name, key in (("Date antérieure", "earlier"), ("Date postérieure", "later")):
        acquisition = facts[key]
        estimate = getattr(pair, key)
        lines.append(f"**{name}** : acquisition {_when(estimate.acquired_at_utc)} · ingestion GEONAF "
                     f"{_when(parse_utc(acquisition['ingested_at_utc'])) if acquisition['ingested_at_utc'] else 'inconnue'}"
                     f" · produits {', '.join(acquisition['product_ids'])}"
                     f" · {', '.join(acquisition['satellites']) or 'satellite inconnu'} · orbite relative "
                     f"{', '.join(acquisition['relative_orbits']) or 'inconnue'} (orbite absolue non enregistrée) · "
                     f"couverture individuelle {format_percent(acquisition['individual_coverage_percent'])} %")
    lines += [
        f"**Profil scientifique** : {profiles['scientific']['id']} {profiles['scientific']['version']} (empreinte "
        f"{profiles['scientific']['fingerprint'][:16]}…), politique SCL « {profiles['scientific']['scl_policy']} », "
        f"seuil de variation ΔNDVI {fr(profiles['scientific']['ndvi_variation_threshold'], 2)} (non utilisé ici)",
        f"**Profil de végétation** : {profiles['vegetation']['id']} {profiles['vegetation']['version']} (empreinte "
        f"{profiles['vegetation']['fingerprint'][:16]}…), seuil de classe NDVI {fr(facts['ndvi_class_threshold'])}",
        f"**Profil de transition** : {profiles['transition']['id']} {profiles['transition']['version']} (empreinte "
        f"{profiles['transition']['fingerprint'][:16]}…), couverture commune minimale "
        f"{fr(TRANSITION_PROFILE.min_common_coverage_percent)} %",
        f"**Couverture commune** : {format_percent(facts['common_coverage_percent'])} % réellement mesurés ; borne "
        f"supérieure de présélection {format_percent(facts['coverage_upper_bound_percent'])} % (min des couvertures "
        "individuelles, jamais une preuve de comparabilité)",
        f"**Comparabilité saisonnière** : {facts['seasonal_comparability'] or 'non évaluée'}",
        f"**Fiabilité : {indicator.reliability}** — " + " ".join(indicator.reliability_reasons),
        f"**Méthode** : {indicator.method}"]
    st.markdown("  \n".join(lines))
    credit = attribution(indicator.source, pair.later.acquired_at_utc.year)
    if credit is not None and not credit.verified:
        st.caption(f"Attribution non revérifiée : « {credit.text} » — mention reprise de la note de publication du "
                   "paquet de démonstration v3, non contrôlée sur l'avis juridique Copernicus.")
    st.caption("Empreintes des manifestes vérifiés : " + ", ".join(
        f"{tile['product_id']} {tile['manifest_sha256'][:12]}…"
        for key in ("earlier", "later") for tile in facts[key]["tiles"]))
    st.markdown("**Limites scientifiques**")
    for limit in indicator.limits:
        st.caption(f"- {limit}")


def _pairs_table(pairs):
    frame = pd.DataFrame([{"Paire": p.label, "Statut": STATUS_LABELS.get(p.status, p.status),
                           "Borne supérieure de couverture commune (%)": p.coverage_upper_bound_percent,
                           "Motif": " ".join(p.reasons) or "—"} for p in pairs])
    st.dataframe(french_table(frame, TABLE_DECIMALS), hide_index=True, width="stretch")
    st.caption("La borne supérieure (minimum des couvertures individuelles) sert seulement à écarter les paires "
               "sans calcul ; seule la couverture commune réellement mesurée décide de la publication.")


def _compute(data_dir, aoi_id, only):
    """Cached selection (default evaluation, or the one pair ``only``) after the resource guard."""
    inventory = cached_inventory(s2_inventory_key(data_dir, aoi_id, year=YEAR, profile=VEGETATION_PROFILE),
                                 str(data_dir), aoi_id)
    key = s2_pairs_key(data_dir, aoi_id, year=YEAR, profile=TRANSITION_PROFILE, only=only)
    return cached_selection(key, str(data_dir), aoi_id, only, inventory)


def _start(aoi_id, flag):
    st.caption("Compare deux acquisitions Sentinel-2 sur la seule surface observée aux deux dates. Le calcul lit "
               "les acquisitions locales vérifiées (quelques secondes) ; il n'est pas lancé automatiquement.")
    st.button("Comparer deux acquisitions", key=f"s2_pairs_start_{aoi_id}",
              on_click=st.session_state.__setitem__, args=(flag, True))


def _request(requested_key, pair_id):
    st.session_state[requested_key] = st.session_state.get(requested_key, frozenset()) | {pair_id}


def show_s2_transitions(data_dir, aoi_id: str) -> None:
    st.markdown(f"#### {TITLE}")
    st.markdown(":violet-badge[Estimation] :red-badge[Expérimental] :gray-badge[Non validée scientifiquement]")
    st.warning(NO_CAUSE)
    _thresholds(aoi_id)
    flag, requested_key = f"s2_pairs_on_{aoi_id}", f"s2_pairs_requested_{aoi_id}"
    if not st.session_state.get(flag):
        _start(aoi_id, flag)
        return
    data_dir = Path(data_dir)
    if not show_assessment(assess_aoi_file(data_dir / "aoi" / f"{aoi_id}.geojson"),
                           operation="Comparaison temporelle Sentinel-2"):
        return
    try:
        with st.spinner("Calcul de la comparaison : vérification des acquisitions et surface commune…"):
            selection = _compute(data_dir, aoi_id, None)
    except Exception as error:  # Functional message, else generic message + diagnostic identifier.
        show_error(error, context="s2_transitions.selection",
                   prefix="Erreur de traitement : comparaison temporelle impossible. ")
        return
    for note in selection.notes:
        st.caption(note)
    if not selection.pairs:
        st.info("Comparaison non disponible : moins de deux acquisitions Sentinel-2 vérifiées pour cette zone.")
        return
    pairs = list(selection.pairs)
    default = selection.default_pair()
    index = pairs.index(default) if default is not None else 0
    chosen = st.selectbox("Paire d'acquisitions", range(len(pairs)), index=index,
                          format_func=lambda i: _option(pairs[i]), key=f"s2_pairs_pair_{aoi_id}")
    pair = pairs[chosen]
    if default is None:
        st.caption("Aucune paire calculée n'atteint les conditions de publication : aucune estimation n'est publiée.")
    with st.expander("Toutes les paires et leur statut"):
        _pairs_table(pairs)
    if pair.status == NOT_EVALUATED and not pair.reasons[0].startswith("Calcul de paire refusé"):
        _pair_header(pair, aoi_id)
        if pair.pair_id not in st.session_state.get(requested_key, frozenset()):
            # No figure from any previous pair: nothing is shown until this pair is computed.
            st.info(f"Paire non calculée ({pair.reasons[0]}) Aucun chiffre n'est affiché tant qu'elle n'est pas "
                    "calculée.")
            st.button("Calculer cette paire", key=f"s2_pairs_compute_{aoi_id}", on_click=_request,
                      args=(requested_key, pair.pair_id))
            return
        try:
            with st.spinner("Calcul de la paire sélectionnée…"):
                (pair,) = _compute(data_dir, aoi_id, pair.pair_id).pairs
        except Exception as error:
            show_error(error, context="s2_transitions.paire",
                       prefix="Erreur de traitement : paire impossible à calculer. ")
            return
    else:
        _pair_header(pair, aoi_id)
    indicators = build_s2_transition_indicators(pair, selection)
    if pair.status != PUBLISHABLE or pair.measure is None:
        _unpublished(pair)
        return
    _published(pair, indicators)
    with st.expander("Les quatre catégories de transitions"):
        _transitions_table(pair)
    with st.expander("Sensibilité aux seuils NDVI 0,3 · 0,4 · 0,5"):
        _sensitivity_table(pair)
    with st.expander("Diagnostic spatial par blocs de 1 km²"):
        _blocks(pair)
    with st.expander("Provenance et limites scientifiques"):
        _provenance(pair, selection, indicators[0])
