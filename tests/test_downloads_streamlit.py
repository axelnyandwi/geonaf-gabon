"""Commands use the actual background queue and synthetic transfer transport."""

from dataclasses import replace
from datetime import date

from tests.test_local_preview import local_data
from tests.test_catalogue_streamlit import _page
from tests.test_downloads import _scenario, _wait
from src.services.download_queue import DownloadQueue


def test_reruns_never_start_downloads_and_completion_is_visible(local_data, monkeypatch):
    import src.components.acquisition_downloads as ui
    selection, items, client = _scenario(local_data)
    queue = DownloadQueue(local_data / "managed", resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    monkeypatch.setattr(ui, "download_queue", lambda root: queue)
    page, search = _page(local_data, monkeypatch)
    search.side_effect = lambda query, data: replace(selection, query=query)
    try:
        page.run(timeout=30)
        page.date_input(key="catalogue_start").set_value(date(2025, 1, 1))
        page.date_input(key="catalogue_end").set_value(date(2025, 1, 31))
        page.button(key="catalogue_search").click().run()
        page.multiselect[0].set_value([selection.records[0].stac_id]).run()
        page.run()
        assert not client.calls
        page.button(key="downloads_estimate").click().run()
        assert _wait(queue)["phase"] == "ready" and not client.calls
        page.run()
        page.run()
        assert not page.exception and not client.calls
        page.button(key="downloads_start").click().run()
        assert _wait(queue)["rows"][0]["state"] == "completed"
        page.run()
        page.run()
        assert not page.exception and not page.error and len(client.calls) == 4
        assert "completed" in page.dataframe[0].value.to_string()
    finally:
        queue.close()


def test_busy_to_idle_transition_reenables_commands_without_loop(local_data, monkeypatch):
    import src.components.acquisition_downloads as ui
    selection, items, client = _scenario(local_data)
    queue = DownloadQueue(local_data / "managed", resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    monkeypatch.setattr(ui, "download_queue", lambda root: queue)
    page, search = _page(local_data, monkeypatch)
    search.side_effect = lambda query, data: replace(selection, query=query)
    try:
        page.run(timeout=30)
        page.date_input(key="catalogue_start").set_value(date(2025, 1, 1))
        page.date_input(key="catalogue_end").set_value(date(2025, 1, 31))
        page.button(key="catalogue_search").click().run()
        page.multiselect[0].set_value([selection.records[0].stac_id]).run()
        page.button(key="downloads_estimate").click().run()
        _wait(queue)
        # The fragment saw a busy queue on its previous tick; the worker has since finished.
        page.session_state["downloads_was_busy"] = True
        page.run()
        assert not page.exception and page.session_state["downloads_was_busy"] is False
        assert not page.button(key="downloads_estimate").disabled and not client.calls
    finally:
        queue.close()
