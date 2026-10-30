"""Shared, byte-bounded results and heavy-computation slots through the real pages (C4b); offline."""

from dataclasses import fields, is_dataclass
from pathlib import Path
import threading
from unittest.mock import Mock

import numpy as np
import pytest
from streamlit.testing.v1 import AppTest

from src.components.preview_cache import peek_pair_analysis
from src.services import bounded_cache, compute_slots
from src.services.bounded_cache import MIB, ByteBudgetCache
from src.services.compute_slots import ComputeSlots, SlotSettings
from tests.test_local_preview import local_data  # noqa: F401  (fixture)
from tests.test_multisource_demo import layers  # noqa: F401  (fixture)

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def _arrays(value, path="result"):
    if isinstance(value, np.ndarray):
        yield path, value
    elif is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            yield from _arrays(getattr(value, field.name), f"{path}.{field.name}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            yield from _arrays(item, f"{path}[{index}]")


def _assert_same_arrays(left, right):
    pairs = dict(_arrays(right))
    for path, array in _arrays(left):
        other = pairs.pop(path)
        assert array.dtype == other.dtype and array.shape == other.shape, path
        assert np.array_equal(array, other, equal_nan=array.dtype.kind in "fc"), path
    assert not pairs


@pytest.fixture
def home(local_data, monkeypatch):
    import src.config
    import src.services.local_preview as preview

    monkeypatch.setattr(src.config, "DATA_DIR", local_data)
    monkeypatch.setattr("src.components.resource_panel.available_memory_bytes", lambda: None)
    analyze = Mock(wraps=preview.analyze_local_akanda)
    monkeypatch.setattr(preview, "analyze_local_akanda", analyze)

    def open_session(**state):
        app = AppTest.from_file(APP, default_timeout=60)
        for name, value in state.items():
            app.session_state[name] = value
        return app.run()
    open_session.analyze = analyze
    return open_session


def test_sessions_of_the_same_pair_share_one_read_only_result(home, local_data):
    from src.services.local_preview import analyze_local_akanda, list_local_observations
    from src.geo.scientific_profile import reference_profile

    first, second = home(), home()
    assert not first.exception and not first.error and not second.exception and not second.error
    assert home.analyze.call_count == 1                                     # Computed once for both sessions.
    assert first.session_state["pair"] == second.session_state["pair"]
    assert "result" not in first.session_state                              # Sessions keep only the key.
    shared = peek_pair_analysis(first.session_state["pair"])
    for path, array in _arrays(shared):
        assert not array.flags.writeable, path
        with pytest.raises(ValueError):
            array[...] = 0                                                  # No in-place write.
        view = array[..., :1]
        assert not view.flags.writeable, path                               # Views are read-only too...
        with pytest.raises(ValueError):
            view.setflags(write=True)                                       # ...and cannot be unlocked.
    direct = analyze_local_akanda(*list_local_observations(local_data), local_data,
                                  scientific_profile=reference_profile("akanda"))
    _assert_same_arrays(shared, direct)                                     # Same science as uncached.
    assert [m.value for m in first.metric] == [m.value for m in second.metric]


def test_different_pairs_or_profiles_get_their_own_entries(home):
    standard = home()
    advanced = home(analysis_mode="Analyse avancée", analysis_threshold=0.2)
    assert not advanced.exception and not advanced.error
    assert standard.session_state["pair"] != advanced.session_state["pair"]
    assert home.analyze.call_count == 2
    left, right = (peek_pair_analysis(app.session_state["pair"]) for app in (standard, advanced))
    assert left is not right and (left.threshold, right.threshold) == (standard_threshold(left), 0.2)


def standard_threshold(result):
    from src.geo.scientific_profile import reference_profile

    assert result.threshold == reference_profile("akanda").change_threshold
    return result.threshold


def test_evicted_result_is_recomputed_identically(home, monkeypatch):
    monkeypatch.setattr(bounded_cache, "_shared", ByteBudgetCache(1))      # Too small: nothing retained.
    app = home()
    first_metrics = [m.value for m in app.metric]
    app.run()
    assert not app.exception and not app.error and home.analyze.call_count == 2
    assert [m.value for m in app.metric] == first_metrics
    assert bounded_cache.shared_cache().retained_bytes() == 0


def test_saturated_slots_show_a_safe_message_then_recover(home, monkeypatch):
    slots = ComputeSlots(SlotSettings(slots=1, wait_seconds=0.2))
    monkeypatch.setattr(compute_slots, "_shared", slots)
    inside, release = threading.Event(), threading.Event()

    def other_session():
        with slots.heavy():
            inside.set()
            release.wait(10)

    holder = threading.Thread(target=other_session)
    holder.start()
    assert inside.wait(5)
    try:
        app = home()
        assert not app.exception and not app.metric
        assert any(e.value.startswith("Serveur occupé") and "Traceback" not in e.value for e in app.error)
    finally:
        release.set()
        holder.join(10)
    app.run()
    assert not app.exception and not app.error and len(app.metric) == 4
    assert slots.active == 0 and slots.refused == 1


# --- Multisource page ---------------------------------------------------------------------------

def _multisource(data_dir, monkeypatch):
    import src.config
    import src.components.multisource as ui
    from src.services import external_retrieval

    monkeypatch.setattr(src.config, "DATA_DIR", data_dir)
    monkeypatch.setattr("src.components.resource_panel.available_memory_bytes", lambda: None)
    for name in ("retrieve_worldcover", "retrieve_osm", "retrieve_sentinel1"):
        monkeypatch.setattr(external_retrieval, name, Mock(side_effect=AssertionError("réseau")))
    ndvi = Mock(wraps=ui.analyze_local_group)
    monkeypatch.setattr(ui, "analyze_local_group", ndvi)
    app = AppTest.from_file(APP, default_timeout=60).run()
    app.switch_page("pages/3_Multisource.py").run()
    assert not app.exception and not app.error
    return app, ndvi


def _coverage_warning(app):
    return any("inférieure au minimum du profil" in w.value for w in app.warning)


def test_threshold_and_coverage_changes_reuse_the_ndvi_with_the_effective_profile(layers, monkeypatch):
    app, ndvi = _multisource(layers[0], monkeypatch)
    app.radio(key="ms_analysis_mode").set_value("Analyse avancée").run()
    for threshold in (0.11, 0.12, 0.13):
        app.number_input(key="ms_analysis_threshold").set_value(threshold).run()
        assert not app.exception and not app.error
    assert ndvi.call_count == 1                                     # One NDVI for four profiles.
    # The coverage warning reads the profile carried by the observation: it must be the effective one.
    app.number_input(key="ms_analysis_coverage").set_value(100.0).run()
    assert _coverage_warning(app) and ndvi.call_count == 1
    app.number_input(key="ms_analysis_coverage").set_value(1.0).run()
    assert not _coverage_warning(app) and ndvi.call_count == 1
    app.selectbox(key="ms_analysis_scl").set_value("vegetation").run()
    assert not app.exception and ndvi.call_count == 2              # Another SCL policy: another NDVI.


def test_multisource_cache_stays_within_its_byte_budget(layers, monkeypatch):
    monkeypatch.setattr(bounded_cache, "_shared", ByteBudgetCache(2 * MIB))
    app, _ = _multisource(layers[0], monkeypatch)
    app.radio(key="ms_analysis_mode").set_value("Analyse avancée").run()
    for threshold in (0.11, 0.12, 0.13):
        app.number_input(key="ms_analysis_threshold").set_value(threshold).run()
        assert not app.exception and not app.error
        assert bounded_cache.shared_cache().retained_bytes() <= 2 * MIB
