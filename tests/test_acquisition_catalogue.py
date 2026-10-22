"""Metadata-only catalogue tests; no satellite or network access."""

from datetime import date, datetime, timezone
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from tests.test_local_preview import local_data
from src.data.catalogue import CatalogueQuery, CatalogueRecord, CatalogueResult, CatalogueError
from src.services.acquisition_catalogue import load_catalogue_aoi, search_catalogue, select_acquisitions


def _item(identifier="A", acquired="2025-01-15T09:30:00Z"):
    return {"id": identifier, "collection": "sentinel-2-l2a",
            "properties": {"datetime": acquired, "eo:cloud_cover": 25.0},
            "assets": {key: {"href": "s3://eodata/synthetic/" + key}
                       for key in ("B04_10m", "B08_10m", "SCL_20m")}}


def _stub_pages(monkeypatch, pages, *, late_error=None):
    from src.data import ingestion
    calls = {"pages": 0}

    class Search:
        def pages_as_dicts(self):
            for page in pages:
                calls["pages"] += 1
                yield {"features": page}
            if late_error:
                raise late_error

    class Client:
        def search(self, **kwargs):
            calls.update(kwargs)
            return Search()

    def open_client(url, **kwargs):
        calls["stac_io"] = kwargs["stac_io"]
        calls["close"] = Mock(wraps=kwargs["stac_io"].session.close)
        kwargs["stac_io"].session.close = calls["close"]
        return Client()

    monkeypatch.setattr(ingestion.Client, "open", open_client)
    monkeypatch.setattr(ingestion, "download_asset", Mock(side_effect=AssertionError("No download")))
    return calls


def test_free_period_and_aoi_identity(local_data):
    aoi = load_catalogue_aoi(local_data, "akanda")
    query = CatalogueQuery(aoi, date(2024, 1, 1), date(2026, 10, 22))
    assert query.fingerprint != replace(query, end=date(2026, 10, 21)).fingerprint
    assert aoi.geometry_json and aoi.sha256
    with pytest.raises(ValueError):
        CatalogueQuery(aoi, date(2025, 2, 1), date(2025, 1, 1))
    with pytest.raises(CatalogueError):
        load_catalogue_aoi(local_data, "../outside")


def test_multiple_selection_preserves_search_provenance(local_data):
    query = CatalogueQuery(load_catalogue_aoi(local_data, "akanda"), date(2025, 1, 1), date(2025, 2, 28))
    records = tuple(CatalogueRecord("sentinel-2-l2a", name, datetime(2025, 1, day, tzinfo=timezone.utc),
                                    40.0, True, True, True) for name, day in (("A", 15), ("B", 25)))
    result = CatalogueResult(query, records, True, 2, "https://stac.dataspace.copernicus.eu/v1",
                             datetime.now(timezone.utc))
    selection = select_acquisitions(result, ["B", "A"])
    assert [item.stac_id for item in selection.records] == ["B", "A"]
    assert selection.query == query and selection.source == result.source
    with pytest.raises(CatalogueError):
        select_acquisitions(result, ["unknown"])


def test_complete_pagination_deduplicates_and_reuses_secure_transport(local_data, monkeypatch):
    from src.data.network import SecureStacIO
    calls = _stub_pages(monkeypatch, [[_item("B")], [_item("A"), _item("B")],
                                      [_item("C", "2025-02-03T09:30:00Z")]])
    query = CatalogueQuery(load_catalogue_aoi(local_data, "akanda"), date(2025, 1, 1), date(2025, 2, 28))
    result = search_catalogue(query, local_data)
    assert result.complete and result.raw_count == 4
    assert [row.stac_id for row in result.records] == ["A", "B", "C"]
    assert calls["pages"] == 3 and calls["max_items"] is None
    assert calls["intersects"] == json.loads(query.aoi.geometry_json) and "bbox" not in calls
    assert isinstance(calls["stac_io"], SecureStacIO)
    calls["close"].assert_called_once()


def test_complete_mode_removes_legacy_cap_without_changing_default(monkeypatch):
    from src.data.ingestion import search_sentinel2
    calls = _stub_pages(monkeypatch, [[_item("A")], [_item("B")], [_item("C")]])
    result = search_sentinel2([0, 0, 1, 1], "2025-01-01", "2025-01-31", max_items=1, complete=True)
    assert len(result) == 3 and result.raw_count == 3 and not result.truncated
    assert calls["max_items"] is None
    assert calls["datetime"] == "2025-01-01T00:00:00Z/2025-01-31T23:59:59.999999Z"


def test_late_network_failure_never_returns_partial_results_or_private_details(local_data, monkeypatch):
    import requests
    calls = _stub_pages(monkeypatch, [[_item()]], late_error=requests.Timeout("private-url-and-token"))
    query = CatalogueQuery(load_catalogue_aoi(local_data, "akanda"), date(2025, 1, 1), date(2025, 1, 31))
    with pytest.raises(CatalogueError) as error:
        search_catalogue(query, local_data)
    assert "private" not in str(error.value) and error.value.__suppress_context__
    calls["close"].assert_called_once()


@pytest.mark.parametrize("value", [None, -1, 101, True, float("nan")])
def test_missing_or_invalid_global_cloud_is_unknown_not_zero(local_data, monkeypatch, value):
    item = _item()
    item["properties"]["eo:cloud_cover"] = value
    item["assets"].pop("SCL_20m")
    _stub_pages(monkeypatch, [[item]])
    query = CatalogueQuery(load_catalogue_aoi(local_data, "akanda"), date(2025, 1, 1), date(2025, 1, 31))
    record = search_catalogue(query, local_data).records[0]
    assert record.global_cloud_cover is None
    assert record.b04_available and record.b08_available and not record.scl_available


def test_acquisition_is_normalized_to_utc_and_unsupported_date_rejects_search(local_data, monkeypatch):
    query = CatalogueQuery(load_catalogue_aoi(local_data, "akanda"), date(2025, 1, 1), date(2025, 1, 31))
    _stub_pages(monkeypatch, [[_item(acquired="2025-01-16T00:30:00+02:00")]])
    record = search_catalogue(query, local_data).records[0]
    assert record.acquired_at_utc == datetime(2025, 1, 15, 22, 30, tzinfo=timezone.utc)
    _stub_pages(monkeypatch, [[_item(acquired="2025-01-15T09:30:00")]])
    with pytest.raises(CatalogueError, match="UTC"):
        search_catalogue(query, local_data)


@pytest.mark.parametrize("state", ["manifest_present", "partial", "unverified"])
def test_local_state_checks_metadata_without_reading_pixels(local_data, monkeypatch, state):
    import rasterio
    from src.services.acquisition_catalogue import catalogue_rows

    manifest_path = local_data / "raw/sentinel2/product-15/acquisition_manifest.json"
    if state == "partial":
        (manifest_path.parent / "b04.tif").unlink()  # Synthetic pytest fixture only.
    elif state == "unverified":
        manifest = json.loads(manifest_path.read_text())
        manifest["integrity"]["mode"] = "exploratory"
        manifest_path.write_text(json.dumps(manifest))
    _stub_pages(monkeypatch, [[_item("S2B_TEST_15")]])
    query = CatalogueQuery(load_catalogue_aoi(local_data, "akanda"), date(2025, 1, 1), date(2025, 1, 31))
    monkeypatch.setattr(rasterio, "open", Mock(side_effect=AssertionError("No pixels")))
    result = search_catalogue(query, local_data)
    assert result.records[0].local_state == state
    public = json.dumps(catalogue_rows(result))
    assert str(local_data) not in public and "s3://" not in public


def test_repository_can_be_replaced_without_streamlit_or_storage_dependency(local_data):
    query = CatalogueQuery(load_catalogue_aoi(local_data, "akanda"), date(2025, 1, 1), date(2025, 1, 31))
    class Repository:
        def search(self, request):
            assert request == query
            return CatalogueResult(request, (), True, 0, "synthetic-repository", datetime.now(timezone.utc))
    assert search_catalogue(query, local_data, repository=Repository()).records == ()


def test_query_revision_changes_with_aoi_contents_profile_and_cloud_filter(local_data):
    aoi = load_catalogue_aoi(local_data, "akanda")
    query = CatalogueQuery(aoi, date(2025, 1, 1), date(2025, 1, 31))
    assert query.fingerprint != replace(query, profile_identity=("profile", "1", "digest")).fingerprint
    assert query.fingerprint != replace(query, max_cloud_cover=30).fingerprint
    path = local_data / "aoi/akanda.geojson"
    path.write_bytes(path.read_bytes() + b"\n")
    assert query.fingerprint != replace(query, aoi=load_catalogue_aoi(local_data, "akanda")).fingerprint


@pytest.mark.parametrize("cloud", [-1, 101, True, float("nan")])
def test_query_rejects_invalid_cloud_filter_before_network(local_data, cloud):
    with pytest.raises(ValueError):
        CatalogueQuery(load_catalogue_aoi(local_data, "akanda"), date(2025, 1, 1), date(2025, 1, 31), cloud)


def test_duplicate_local_identity_is_ambiguous(local_data, monkeypatch):
    import shutil
    source = local_data / "raw/sentinel2/product-15"
    shutil.copytree(source, source.with_name("duplicate"))
    _stub_pages(monkeypatch, [[_item("S2B_TEST_15")]])
    query = CatalogueQuery(load_catalogue_aoi(local_data, "akanda"), date(2025, 1, 1), date(2025, 1, 31))
    assert search_catalogue(query, local_data).records[0].local_state == "ambiguous"


def test_manifest_traversal_is_unverified_not_opened_as_asset(local_data, monkeypatch):
    manifest_path = local_data / "raw/sentinel2/product-15/acquisition_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["assets"]["b04"]["path"] = "../outside.tif"
    manifest_path.write_text(json.dumps(manifest))
    _stub_pages(monkeypatch, [[_item("S2B_TEST_15")]])
    query = CatalogueQuery(load_catalogue_aoi(local_data, "akanda"), date(2025, 1, 1), date(2025, 1, 31))
    assert search_catalogue(query, local_data).records[0].local_state == "unverified"
