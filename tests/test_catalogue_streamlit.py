"""Metadata catalogue UI with a fake repository; no network/downloads."""

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from src.data.catalogue import CatalogueRecord, CatalogueResult, CatalogueError
from tests.test_local_preview import local_data


def _page(local_data, monkeypatch):
    import src.config
    import src.components.acquisition_catalogue as ui
    monkeypatch.setattr(src.config, "DATA_DIR", local_data)
    def result(query, data_dir):
        return CatalogueResult(query, tuple(
            CatalogueRecord("sentinel-2-l2a", name, datetime(2025, 1, day, tzinfo=timezone.utc),
                            25.0, True, True, True)
            for name, day in (("A", 15), ("B", 25))),
            True, 2, "https://stac.dataspace.copernicus.eu/v1", datetime.now(timezone.utc))
    search = Mock(side_effect=result)
    monkeypatch.setattr(ui, "search_catalogue", search)
    # Register pages from the actual entrypoint, including the return link.
    # Empty local observations keep this catalogue-only fixture metadata-only.
    import src.services.local_preview as preview
    monkeypatch.setattr(preview, "list_local_observations", lambda data_dir: ())
    page = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
    page.run(timeout=30)
    page.switch_page("pages/1_Acquisitions_Sentinel2.py")
    return page, search


def test_no_search_on_open_and_multiple_selection_does_not_repeat_network(local_data, monkeypatch):
    page, search = _page(local_data, monkeypatch)
    page.run(timeout=30)
    assert not page.exception and not page.error and not search.called
    page.date_input(key="catalogue_start").set_value(date(2025, 1, 1))
    page.date_input(key="catalogue_end").set_value(date(2025, 2, 28))
    page.button(key="catalogue_search").click().run(timeout=30)
    assert not page.exception and not page.error and len(page.dataframe) == 1
    assert search.call_count == 1
    assert search.call_args.args[0].start == date(2025, 1, 1)
    assert search.call_args.args[0].profile_identity is not None
    assert not page.session_state["catalogue_selection"].records
    page.multiselect[0].set_value(["A", "B"]).run()
    assert search.call_count == 1
    assert len(page.session_state["catalogue_selection"].records) == 2
    assert "result" not in page.session_state  # No scientific computation.
    assert "pair" not in page.session_state


def test_changed_period_hides_stale_results_and_selection(local_data, monkeypatch):
    page, search = _page(local_data, monkeypatch)
    page.run(timeout=30)
    page.button(key="catalogue_search").click().run()
    page.multiselect[0].set_value(["A"]).run()
    page.date_input(key="catalogue_start").set_value(date(2024, 1, 1)).run()
    assert not page.dataframe and not page.multiselect and search.call_count == 1
    assert "catalogue_selection" not in page.session_state


def test_failed_search_clears_old_result_and_does_not_leak_paths(local_data, monkeypatch):
    page, search = _page(local_data, monkeypatch)
    page.run(timeout=30)
    page.button(key="catalogue_search").click().run()
    search.side_effect = CatalogueError("Recherche Copernicus indisponible.")
    page.button(key="catalogue_search").click().run()
    assert not page.exception and page.error and not page.dataframe
    assert "catalogue_result" not in page.session_state
    assert "catalogue_selection" not in page.session_state
    assert all(str(local_data) not in message.value for message in page.error)


def test_invalid_period_never_calls_network(local_data, monkeypatch):
    page, search = _page(local_data, monkeypatch)
    page.run(timeout=30)
    page.date_input(key="catalogue_start").set_value(date(2025, 2, 1))
    page.date_input(key="catalogue_end").set_value(date(2025, 1, 1))
    page.button(key="catalogue_search").click().run()
    assert page.error and not search.called


def test_aoi_selection_invalidates_search_without_reusing_akanda_profile(local_data, monkeypatch):
    source = local_data / "aoi/akanda.geojson"
    (source.parent / "libreville.geojson").write_bytes(source.read_bytes())
    page, search = _page(local_data, monkeypatch)
    page.run(timeout=30)
    page.button(key="catalogue_search").click().run()
    page.selectbox(key="catalogue_aoi").set_value("libreville").run()
    assert not page.dataframe and search.call_count == 1
    assert "catalogue_selection" not in page.session_state
    page.button(key="catalogue_search").click().run()
    assert not page.exception and not page.error and search.call_count == 2
    query = search.call_args.args[0]
    # Libreville now has its own reference profile; Akanda's is never reused.
    from src.geo.scientific_profile import GABON_EQUATORIAL_LIBREVILLE as LIBREVILLE
    assert query.aoi.aoi_id == "libreville"
    assert query.profile_identity == (LIBREVILLE.profile_id, LIBREVILLE.version, LIBREVILLE.fingerprint)


def test_empty_and_partial_catalogues_are_explicit(local_data, monkeypatch):
    from dataclasses import replace
    page, search = _page(local_data, monkeypatch)
    page.run(timeout=30)
    normal = search.side_effect
    search.side_effect = lambda query, data: replace(normal(query, data), records=(), raw_count=0)
    page.button(key="catalogue_search").click().run()
    assert not page.multiselect and any("Aucune acquisition trouvée" in item.value for item in page.info)
    search.side_effect = lambda query, data: replace(normal(query, data), complete=False)
    page.button(key="catalogue_search").click().run()
    assert page.dataframe and any("partielle" in item.value for item in page.warning)


def test_local_refresh_does_not_repeat_copernicus_search(local_data, monkeypatch):
    page, search = _page(local_data, monkeypatch)
    page.run(timeout=30)
    page.button(key="catalogue_search").click().run()
    page.button(key="catalogue_refresh_local").click().run()
    assert not page.exception and not page.error and search.call_count == 1


def test_local_refresh_failure_clears_selection_without_private_details(local_data, monkeypatch):
    from src.services import acquisition_catalogue
    page, search = _page(local_data, monkeypatch)
    page.run(timeout=30)
    page.button(key="catalogue_search").click().run()
    def denied(data_dir):
        raise PermissionError("private-path")
    monkeypatch.setattr(acquisition_catalogue, "_local_states", denied)
    page.button(key="catalogue_refresh_local").click().run()
    assert not page.exception and page.error and search.call_count == 1
    assert "private-path" not in page.error[0].value
    assert "catalogue_selection" not in page.session_state
