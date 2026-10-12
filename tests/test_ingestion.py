import pytest
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
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "features": [
                    {"id": "S2_TEST_001"},
                    {"id": "S2_TEST_002"},
                ]
            }

    def fake_post(url, json, timeout):
        assert url == "https://stac.dataspace.copernicus.eu/v1/search"
        assert json["collections"] == ["sentinel-2-l2a"]
        assert json["bbox"] == [9.3, 0.3, 9.6, 0.6]
        assert json["datetime"] == (
            "2025-01-01T00:00:00Z/"
            "2025-01-31T23:59:59Z"
        )
        assert json["query"]["eo:cloud_cover"]["lte"] == 20
        assert timeout == 30

        return FakeResponse()

    monkeypatch.setattr(
        "src.data.ingestion.requests.post",
        fake_post,
    )

    result = search_sentinel2(
        bbox=[9.3, 0.3, 9.6, 0.6],
        start_date="2025-01-01",
        end_date="2025-01-31",
        max_cloud_cover=20,
    )

    assert len(result) == 2
    assert result[0]["id"] == "S2_TEST_001"

def test_search_sentinel2_returns_empty_list(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "features": []
            }

    def fake_post(url, json, timeout):
        return FakeResponse()

    monkeypatch.setattr(
        "src.data.ingestion.requests.post",
        fake_post,
    )

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
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "features": [
                    {"id": "S2_TEST_001"},
                ]
            }

    def fake_post(url, json, timeout):
        assert "query" not in json
        assert json["datetime"] == (
            "2025-01-01T00:00:00Z/"
            "2025-01-31T23:59:59Z"
        )

        return FakeResponse()

    monkeypatch.setattr(
        "src.data.ingestion.requests.post",
        fake_post,
    )

    result = search_sentinel2(
        bbox=[9.3, 0.3, 9.6, 0.6],
        start_date="2025-01-01",
        end_date="2025-01-31",
    )

    assert len(result) == 1