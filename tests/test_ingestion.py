import pytest
import requests
from src.data import ingestion
from src.data.ingestion import get_asset, get_s3_key
from src.data.ingestion import get_s3_key
from unittest.mock import Mock
from src.data.ingestion import (
    download_asset,
    get_asset,
    get_s3_key,
    search_sentinel2,
    select_best_product,
)


class FakeItemSearch:
    def __init__(self, pages, max_items, error=None):
        self.pages = pages
        self.max_items = max_items
        self.error = error

    def pages_as_dicts(self):
        remaining = self.max_items
        for page_index, page in enumerate(self.pages):
            if self.error is not None and page_index == len(self.pages) - 1:
                raise self.error

            features = page.get("features")
            if not isinstance(features, list):
                yield page
                continue

            selected = features[:remaining]
            yield {**page, "features": selected}
            remaining -= len(selected)
            if remaining <= 0:
                return


def _stub_stac_client(monkeypatch, pages, *, error=None):
    calls = {}

    class FakeCatalog:
        def search(self, **kwargs):
            calls.update(kwargs)
            return FakeItemSearch(pages, kwargs["max_items"], error=error)

    def fake_open(url, **kwargs):
        calls["url"] = url
        calls["open_kwargs"] = kwargs
        return FakeCatalog()

    monkeypatch.setattr(ingestion.Client, "open", fake_open)
    return calls


def test_get_s3_key():
    asset = {
        "href": "s3://eodata/Sentinel-2/test/B04_10m.jp2"
    }

    result = get_s3_key(asset)

    assert result == "Sentinel-2/test/B04_10m.jp2"

def test_get_s3_key_rejects_wrong_bucket():
    asset = {
        "href": "s3://wrong-bucket/Sentinel-2/test/B04_10m.jp2"
    }

    with pytest.raises(ValueError):
        get_s3_key(asset)

def test_get_asset():
    item = {
        "assets": {
            "B04_10m": {
                "href": "s3://eodata/Sentinel-2/test/B04_10m.jp2"
            }
        }
    }

    result = get_asset(item, "B04_10m")

    assert result["href"] == "s3://eodata/Sentinel-2/test/B04_10m.jp2"


def test_get_asset_rejects_missing_asset():
    item = {
        "assets": {}
    }

    with pytest.raises(ValueError):
        get_asset(item, "B08_10m")

def test_download_asset(tmp_path):
    s3_client = Mock()

    asset = {
        "href": "s3://eodata/Sentinel-2/test/B04_10m.jp2"
    }

    result = download_asset(
        s3_client=s3_client,
        asset=asset,
        destination_dir=tmp_path,
    )

    expected_path = tmp_path / "B04_10m.jp2"

    assert result == expected_path

    s3_client.download_file.assert_called_once_with(
        "eodata",
        "Sentinel-2/test/B04_10m.jp2",
        str(expected_path),
    )

def test_download_asset_uses_cache(tmp_path):
    s3_client = Mock()

    asset = {
        "href": "s3://eodata/Sentinel-2/test/B04_10m.jp2"
    }

    cached_file = tmp_path / "B04_10m.jp2"
    cached_file.write_bytes(b"simulated satellite data")

    result = download_asset(
        s3_client=s3_client,
        asset=asset,
        destination_dir=tmp_path,
    )

    assert result == cached_file
    s3_client.download_file.assert_not_called()

def test_search_sentinel2(monkeypatch):
    calls = _stub_stac_client(
        monkeypatch,
        [
            {
                "features": [
                    {"id": "S2_TEST_001", "properties": {"eo:cloud_cover": 35}}
                ]
            },
            {
                "features": [
                    {"id": "S2_TEST_002", "properties": {"eo:cloud_cover": 5}}
                ]
            },
        ],
    )

    result = search_sentinel2(
        bbox=[9.3, 0.3, 9.6, 0.6],
        start_date="2025-01-01",
        end_date="2025-01-31",
        max_cloud_cover=20,
    )

    assert len(result) == 2
    assert result[0]["id"] == "S2_TEST_001"
    assert select_best_product(result)["id"] == "S2_TEST_002"
    assert calls["url"] == ingestion.STAC_API_URL
    assert calls["open_kwargs"]["timeout"] == ingestion.STAC_TIMEOUT_SECONDS
    assert calls["collections"] == ["sentinel-2-l2a"]
    assert calls["bbox"] == [9.3, 0.3, 9.6, 0.6]
    assert calls["datetime"] == (
        "2025-01-01T00:00:00Z/"
        "2025-01-31T23:59:59Z"
    )
    assert calls["query"]["eo:cloud_cover"]["lte"] == 20
    assert calls["limit"] == ingestion.STAC_PAGE_SIZE
    assert calls["max_items"] == ingestion.DEFAULT_STAC_MAX_ITEMS + 1
    assert isinstance(result, ingestion.STACSearchResults)
    assert not result.truncated
    assert result.raw_count == 2

def test_search_sentinel2_returns_empty_list(monkeypatch):
    _stub_stac_client(monkeypatch, [])

    result = search_sentinel2(
        bbox=[9.3, 0.3, 9.6, 0.6],
        start_date="2025-01-01",
        end_date="2025-01-31",
    )

    assert result == []

def test_select_best_product():
    items = [
        {
            "id": "IMAGE_A",
            "properties": {"eo:cloud_cover": 22.5},
        },
        {
            "id": "IMAGE_B",
            "properties": {"eo:cloud_cover": 4.2},
        },
        {
            "id": "IMAGE_C",
            "properties": {"eo:cloud_cover": 11.8},
        },
    ]

    result = select_best_product(items)

    assert result["id"] == "IMAGE_B"

def test_select_best_product_rejects_empty_list():
    with pytest.raises(ValueError):
        select_best_product([])

def test_search_sentinel2_without_cloud_filter(monkeypatch):
    calls = _stub_stac_client(
        monkeypatch,
        [{"features": [{"id": "S2_TEST_001"}]}],
    )

    result = search_sentinel2(
        bbox=[9.3, 0.3, 9.6, 0.6],
        start_date="2025-01-01",
        end_date="2025-01-31",
    )

    assert len(result) == 1
    assert "query" not in calls
    assert calls["datetime"] == (
        "2025-01-01T00:00:00Z/"
        "2025-01-31T23:59:59Z"
    )


def test_search_sentinel2_warns_when_total_limit_truncates_results(monkeypatch):
    _stub_stac_client(
        monkeypatch,
        [
            {"features": [{"id": "ITEM_1"}, {"id": "ITEM_2"}]},
            {"features": [{"id": "ITEM_3"}, {"id": "ITEM_4"}]},
        ],
    )

    with pytest.warns(
        ingestion.STACSearchTruncatedWarning,
        match="tronquée",
    ):
        result = search_sentinel2(
            bbox=[9.3, 0.3, 9.6, 0.6],
            start_date="2025-01-01",
            end_date="2025-01-31",
            max_items=2,
        )

    assert [item["id"] for item in result] == ["ITEM_1", "ITEM_2"]
    assert result.truncated
    assert result.raw_count == 3


def test_search_sentinel2_deduplicates_items_across_pages(monkeypatch, caplog):
    _stub_stac_client(
        monkeypatch,
        [
            {"features": [{"id": "ITEM_1"}, {"id": "ITEM_2"}]},
            {"features": [{"id": "ITEM_2"}, {"id": "ITEM_3"}]},
        ],
    )

    result = search_sentinel2(
        bbox=[9.3, 0.3, 9.6, 0.6],
        start_date="2025-01-01",
        end_date="2025-01-31",
        max_items=10,
    )

    assert [item["id"] for item in result] == ["ITEM_1", "ITEM_2", "ITEM_3"]
    assert "dupliqué" in caplog.text.lower()


def test_search_sentinel2_network_error_does_not_expose_url_or_token(monkeypatch):
    private_url = "https://stac.example/search?token=dummy-test-value"
    _stub_stac_client(
        monkeypatch,
        [{"features": [{"id": "ITEM_1"}]}, {"features": []}],
        error=requests.Timeout(f"Request failed for {private_url}"),
    )

    with pytest.raises(ingestion.STACSearchError) as caught:
        search_sentinel2(
            bbox=[9.3, 0.3, 9.6, 0.6],
            start_date="2025-01-01",
            end_date="2025-01-31",
        )

    assert "Timeout" in str(caught.value)
    assert private_url not in str(caught.value)
    assert "dummy-test-value" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_search_sentinel2_rejects_incomplete_page(monkeypatch):
    _stub_stac_client(monkeypatch, [{"features": None}])

    with pytest.raises(ingestion.STACSearchError, match="STAC"):
        search_sentinel2(
            bbox=[9.3, 0.3, 9.6, 0.6],
            start_date="2025-01-01",
            end_date="2025-01-31",
        )


@pytest.mark.parametrize("max_items", [0, -1, None, True])
def test_search_sentinel2_requires_a_positive_integer_limit(max_items):
    with pytest.raises(ValueError, match="max_items"):
        search_sentinel2(
            bbox=[9.3, 0.3, 9.6, 0.6],
            start_date="2025-01-01",
            end_date="2025-01-31",
            max_items=max_items,
        )


def test_ingest_sentinel2_product_forwards_search_limit(monkeypatch, tmp_path):
    item = {
        "id": "S2_PRODUCT",
        "assets": {"B04_10m": {"href": "s3://eodata/example/B04.jp2"}},
    }
    search_arguments = {}

    def fake_search(**kwargs):
        search_arguments.update(kwargs)
        return [item]

    def fake_download(*, asset, destination_dir, **_kwargs):
        filename = asset["href"].rsplit("/", maxsplit=1)[-1]
        return destination_dir / filename

    monkeypatch.setattr(ingestion, "search_sentinel2", fake_search)
    monkeypatch.setattr(ingestion, "download_asset", fake_download)

    result = ingestion.ingest_sentinel2_product(
        s3_client=object(),
        bbox=[9.3, 0.3, 9.6, 0.6],
        start_date="2025-01-01",
        end_date="2025-01-31",
        destination_dir=tmp_path,
        asset_names=["B04_10m"],
        product_id="S2_PRODUCT",
        max_items=42,
    )

    assert search_arguments["max_items"] == 42
    assert result["product_id"] == "S2_PRODUCT"
    assert result["assets"]["B04_10m"].name == "B04.jp2"
