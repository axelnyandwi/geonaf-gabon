"""« Comparaison temporelle sur surface commune » on the home page with AppTest; offline."""

import json
from unittest.mock import Mock

import numpy as np
import pytest
from streamlit.testing.v1 import AppTest

from src.services.local_preview import LocalPreviewError
from tests.test_local_preview import local_data  # noqa: F401  (fixture)
from tests.test_s2_pairs import FULL, NORTH, SOUTH, dates
from tests.test_s2_vegetation_inventory import PACK
from tests.test_territorial_indicators_streamlit import APP

START, PAIR, COMPUTE = "s2_pairs_start_akanda", "s2_pairs_pair_akanda", "s2_pairs_compute_akanda"
TITLE = "#### Comparaison temporelle sur surface commune"
NBSP = "\u202f"


@pytest.fixture
def home(monkeypatch):
    """Runs the home page on ``data_dir`` with cold caches; ``compute`` counts the pair selections."""
    import src.config
    import src.components.s2_transitions as section
    import src.components.s2_vegetation as vegetation
    import src.components.territorial_indicators as figures

    state = {}

    def run(data_dir, *, clear=True, selection=None):
        monkeypatch.setattr(src.config, "DATA_DIR", data_dir)
        monkeypatch.setattr("src.components.resource_panel.available_memory_bytes", lambda: None)
        if clear:
            figures.cached_indicators.clear()
            vegetation.cached_inventory.clear()
            section.cached_selection.clear()
            state["compute"] = Mock(wraps=selection or section.s2_pair_selection)
            state["inventory"] = Mock(wraps=vegetation.s2_vegetation_inventory)
            monkeypatch.setattr(section, "s2_pair_selection", state["compute"])
            monkeypatch.setattr(vegetation, "s2_vegetation_inventory", state["inventory"])
        return AppTest.from_file(APP).run(timeout=180), state["compute"]
    run.state = state
    return run


def part(app):
    """Texts of the sub-section (from its title to the next header), in document order."""
    collected, inside = [], False
    for node in app.main:
        try:
            value = node.value
        except Exception:
            continue
        if not isinstance(value, str):
            continue
        if value == TITLE:
            inside = True
        elif value == "Analyses scientifiques":
            inside = False
        elif inside:
            collected.append(value)
    return " ".join(collected)


def expander(app, prefix):
    return next(e for e in app.expander if e.label.startswith(prefix))


def started(home, data_dir):
    app, compute = home(data_dir)
    app.button(key=START).click().run(timeout=180)
    assert not app.exception, [e.value for e in app.exception]
    return app, compute


def need_pack():
    if not (PACK / "PACK_MANIFEST.json").is_file():
        pytest.skip("Paquet de démonstration absent.")


# --- States ---------------------------------------------------------------------------------------------------


def test_nothing_is_computed_at_the_initial_load(local_data, home):
    app, compute = home(local_data)
    assert not app.exception and not app.error
    assert TITLE in app.markdown.values and compute.call_count == 0
    assert home.state["inventory"].call_count == 0                       # Neither the L3 inventory.
    assert app.button(key=START).label == "Comparer deux acquisitions"
    assert PAIR not in [s.key for s in app.selectbox]
    text = part(app)
    assert "ne démontre ni une déforestation, ni un reboisement, ni une modification réelle" in text
    assert "Seuil de classe NDVI 0,4" in text and "Seuil de variation ΔNDVI 0,10" in text
    assert "n'intervient pas dans cette comparaison" in text and "km²" not in text
    assert [h.value for h in app.header] == ["Chiffres clés du territoire", "Analyses scientifiques"]


def test_explicit_start_gives_the_result_and_the_button_disappears(local_data, home):
    app, compute = started(home, local_data)
    assert compute.call_count == 1 and START not in [b.key for b in app.button]
    text = part(app)
    assert "15/01/2025 09:30 UTC → 25/01/2025 09:30 UTC" in text and "Non disponible" not in text
    assert "ce n'est pas la commune administrative d'Akanda" in text


def test_unavailable_result_shows_no_estimate(tmp_path, home):
    app, _ = started(home, dates(tmp_path, {10: NORTH, 20: SOUTH}))     # Upper bound 50 %, nothing in common.
    text = part(app)
    assert "Non disponible" in text and "Aucune surface observée commune" in text
    assert "badge[Non publiable]" in text and "soit " not in text and "Surface commune comparable" not in text
    assert "Aucune paire calculée n'atteint les conditions de publication" in text
    assert not any(e.label.startswith(("Les quatre", "Sensibilité", "Diagnostic")) for e in app.expander)


def _geonaf_value_error(*args, **kwargs):
    from src.geo.pair_selection import PairSelectionConfig

    PairSelectionConfig(min_interval_days=-1)  # Generic ValueError, even from GEONAF code.


@pytest.mark.parametrize("error, shown, internal", [
    (lambda: LocalPreviewError("Politique SCL « observation » refusée."), "refusée", False),
    (lambda: _geonaf_value_error, "Identifiant de diagnostic : GN-", True),  # Generic ValueError: internal.
    (lambda: RuntimeError("C:/secret/path token=abc123456"), "Identifiant de diagnostic : GN-", True),
    (lambda: ValueError("C:/secret/path from a library"), "Identifiant de diagnostic : GN-", True),
])
def test_processing_error_is_public(local_data, home, caplog, error, shown, internal):
    """Functional errors keep their message; internal ones show a generic message and an identifier."""
    app, _ = home(local_data, selection=Mock(side_effect=error()))
    with caplog.at_level("ERROR", logger="geonaf.ui"):
        app.button(key=START).click().run(timeout=180)
    assert not app.exception
    (message,) = [e.value for e in app.error]
    assert message.startswith("Erreur de traitement") and shown in message
    assert "secret" not in message and "abc123456" not in message and "Traceback" not in message
    records = [json.loads(r.getMessage().partition(" ")[2]) for r in caplog.records if r.name == "geonaf.ui"]
    records = [r for r in records if r["context"].startswith("s2_transitions.")]
    if internal:
        incident = message.split("Identifiant de diagnostic : ")[1].rstrip(".")
        (record,) = records
        assert record["incident"] == incident and record["context"] == "s2_transitions.selection"
        assert record["frames"] and "abc123456" not in json.dumps(record)  # Server-side diagnosis, masked.
    else:
        assert records == [] and "Identifiant" not in message


def test_resource_guard_refuses_before_computation(local_data, home, monkeypatch):
    monkeypatch.setenv("GEONAF_LIMIT_WARN_CELLS", "1")
    monkeypatch.setenv("GEONAF_LIMIT_MAX_CELLS", "2")
    app, compute = home(local_data)
    app.button(key=START).click().run(timeout=180)
    assert compute.call_count == 0
    assert any("Comparaison temporelle Sentinel-2 bloquée avant tout calcul" in e.value for e in app.error)


# --- Selection --------------------------------------------------------------------------------------------------


def test_skipped_pair_shows_no_previous_figure_until_computed(tmp_path, home):
    app, compute = started(home, dates(tmp_path, {10: FULL, 20: FULL, 30: FULL}))
    selector = app.selectbox(key=PAIR)
    labels = [selector.format_func(i) for i in range(len(selector.options))]
    assert labels[selector.value] == "20/01/2025 → 30/01/2025 · publiable" and compute.call_count == 1
    assert "Surface commune comparable" in part(app)
    skipped = labels.index("10/01/2025 → 30/01/2025 · non calculée")
    app.selectbox(key=PAIR).set_value(skipped).run(timeout=180)
    text = part(app)
    assert compute.call_count == 1 and "Paire non calculée" in text and "Aucun chiffre" in text
    for figure in ("Surface commune comparable", "soit ", "% de la surface commune", "badge[Fiabilité"):
        assert figure not in text                                         # Nothing from the previous pair.
    assert "10/01/2025 09:30 UTC → 30/01/2025 09:30 UTC" in text
    app.button(key=COMPUTE).click().run(timeout=180)
    assert not app.exception and compute.call_count == 2                 # The requested pair only.
    assert compute.call_args.kwargs["only"] == (_ids(10), _ids(30))
    assert "Surface commune comparable" in part(app) and COMPUTE not in [b.key for b in app.button]
    app.selectbox(key=PAIR).set_value(labels.index("20/01/2025 → 30/01/2025 · publiable")).run(timeout=180)
    app.selectbox(key=PAIR).set_value(skipped).run(timeout=180)
    assert compute.call_count == 2 and "Surface commune comparable" in part(app)   # Both cached now.


def _ids(day):
    stamp = f"202501{day:02d}T093000"
    return (f"S2B_MSIL2A_{stamp}_N0511_R136_T32NNF_{stamp}",)


def test_other_session_of_the_same_process_reuses_the_cache(local_data, home):
    _, compute = started(home, local_data)
    other, _ = home(local_data, clear=False)                             # New session, same process caches.
    assert START in [b.key for b in other.button]                        # Its own explicit start.
    other.button(key=START).click().run(timeout=180)
    assert not other.exception and compute.call_count == 1               # Cache hit.
    assert "Surface commune comparable" in part(other)


# --- Pack v3: values, rejected pairs, provenance ----------------------------------------------------------------


def test_pack_v3_defaults_to_15_to_25_january_with_its_reference_values(home):
    need_pack()
    app, compute = started(home, PACK)
    selector = app.selectbox(key=PAIR)
    assert selector.format_func(selector.value) == "15/01/2025 → 25/01/2025 · publiable"
    text = part(app)
    assert "15/01/2025 09:32 UTC → 25/01/2025 09:31 UTC" in text and "intervalle 10,0 jours" in text
    for fragment in ("84,47", f"8{NBSP}447 ha", "58,61 % de la zone d'étude", "6,73", f"673 ha", "2,79", "279 ha",
                     "-0,0329", "0,4976 → 0,4648", "badge[Expérimental]", "badge[Fiabilité : faible]",
                     "Intervalle court (10,0 jours < 30)"):
        assert fragment in text, fragment
    assert "53,19" not in text.split("Les quatre")[0]                    # Stayed surfaces: details only.
    table = expander(app, "Les quatre catégories").dataframe[0].value
    assert str(table["Surface (km²)"].dtype) == "float64"                # Numeric: sortable.
    assert [round(v, 2) for v in table["Surface (km²)"]] == [6.73, 2.79, 53.19, 21.75]
    assert table["Surface (km²)"].sum() == pytest.approx(84.4656628600188, rel=1e-12)
    net = " ".join(c.value for c in expander(app, "Les quatre catégories").caption)
    assert "-3,94 km²" in net and "ni une perte ni un gain réel de végétation" in net
    sensitivity = expander(app, "Sensibilité").dataframe[0].value
    assert sensitivity["Seuil"].tolist() == ["NDVI 0,3", "NDVI 0,4", "NDVI 0,5"]
    blocks = expander(app, "Diagnostic spatial")
    words = " ".join(c.value for c in blocks.caption)
    assert "projection à surfaces équivalentes (EPSG:6933" in words and "centre" in words
    assert "approximative" in words and "ne prouve pas" in words
    figures = " ".join(m.value for m in blocks.markdown)
    assert "Cellules retenues : **140**" in figures and "**77,86 %** des cellules" in figures
    assert "**17,86 %** des cellules" in figures and not blocks.dataframe
    assert compute.call_count == 1


def test_pack_v3_rejected_pairs_show_their_reason_without_figures(home):
    need_pack()
    app, compute = started(home, PACK)
    selector = app.selectbox(key=PAIR)
    labels = [selector.format_func(i) for i in range(len(selector.options))]
    assert labels[1:] == ["20/01/2025 → 25/01/2025 · écartée", "15/01/2025 → 20/01/2025 · écartée"]
    table = expander(app, "Toutes les paires").dataframe[0].value
    assert table["Statut"].tolist() == ["publiable", "écartée avant calcul", "écartée avant calcul"]
    assert str(table["Borne supérieure de couverture commune (%)"].dtype) == "float64"
    for index in (1, 2):
        app.selectbox(key=PAIR).set_value(index).run(timeout=180)
        text = part(app)
        assert "Non disponible" in text and "2,7 % < 30 %" in text and "84,47" not in text
        assert "Surface commune comparable" not in text and "soit " not in text
    assert compute.call_count == 1                                       # Rejected pairs are never computed.


def test_pack_v3_provenance_and_limits(home):
    need_pack()
    from src.geo.scientific_profile import GABON_EQUATORIAL_EXPERIMENTAL as AKANDA
    from src.indicators.profiles import S2_NDVI_TRANSITION_V0, S2_NDVI_VEGETATION_V0

    app, _ = started(home, PACK)
    box = expander(app, "Provenance et limites scientifiques")
    text = " ".join(m.value for m in box.markdown) + " " + " ".join(c.value for c in box.caption)
    for fragment in ("S2B_MSIL2A_20250115T093239_N0511_R136_T32NNF_20250115T120528", "Sentinel-2B",
                     "orbite relative 136", "orbite absolue non enregistrée", "ingestion GEONAF 16/10/2026 17:10 UTC",
                     AKANDA.fingerprint[:16], S2_NDVI_VEGETATION_V0.fingerprint[:16],
                     S2_NDVI_TRANSITION_V0.fingerprint[:16], "seuil de classe NDVI 0,4",
                     "seuil de variation ΔNDVI 0,10 (non utilisé ici)", "jamais une preuve de comparabilité",
                     "statut scientifique **experimental**", "Fiabilité : faible", "Attribution non revérifiée",
                     "Non comparable aux indicateurs WorldCover"):
        assert fragment in text, fragment
    assert str(PACK) not in text and "\\" not in text
