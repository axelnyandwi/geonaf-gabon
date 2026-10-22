"""Series page through the real multipage registry; synthetic verified products."""

from pathlib import Path
from unittest.mock import Mock

from tests.test_ndvi_time_series import series_data


def _page(series_data, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import src.config
    import src.components.ndvi_series as ui

    monkeypatch.setattr(src.config, "DATA_DIR", series_data)
    ui.cached_series.clear()
    analyze = Mock(wraps=ui.analyze_local_ndvi_series)
    monkeypatch.setattr(ui, "analyze_local_ndvi_series", analyze)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
    app.run(timeout=30)
    assert any(link.label == "Série temporelle NDVI" for link in app.get("page_link"))
    app.switch_page("pages/2_Series_NDVI.py").run(timeout=30)
    return app, analyze


def test_series_page_computes_only_on_click_and_exports(series_data, monkeypatch):
    app, analyze = _page(series_data, monkeypatch)
    assert not app.exception and not app.error
    assert app.title[0].value == "Série temporelle NDVI"
    assert len(app.multiselect[0].value) == 4
    app.run()
    assert analyze.call_count == 0 and not app.metric
    app.button(key="series_compute").click().run(timeout=30)
    assert not app.exception and not app.error and analyze.call_count == 1
    assert app.metric[0].value == "3 / 4"
    assert "Non comparable" not in app.warning[0].value  # 3 retained dates, no calendar.
    assert "Comparabilité non évaluée" in app.warning[0].value
    assert len(app.dataframe[0].value) == 4
    app.run()
    app.run()
    assert analyze.call_count == 1 and len(app.metric) == 4
    assert app.get("download_button")
    texts = [element.value for kind in (app.caption, app.warning, app.info, app.markdown) for element in kind]
    assert all(str(series_data) not in text for text in texts)
    assert any("exclue" in text for text in texts)


def test_series_page_requires_three_dates_and_explicit_recompute(series_data, monkeypatch):
    app, analyze = _page(series_data, monkeypatch)
    labels = app.multiselect[0].value
    app.multiselect[0].set_value(labels[:2]).run()
    assert not app.exception and any("au moins 3" in info.value for info in app.info)
    app.multiselect[0].set_value(labels[:3]).run()
    assert any("Calculer la série" in info.value for info in app.info) and analyze.call_count == 0
    app.button(key="series_compute").click().run(timeout=30)
    assert not app.exception and analyze.call_count == 1
    assert app.metric[0].value == "2 / 3"
    assert "Non comparable" in app.warning[0].value
