import pytest
import requests
import hashlib
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


def test_ingest_writes_manifest_only_when_explicit_roles_are_supplied(
    monkeypatch, tmp_path
):
    item = {
        "id": "S2_PRODUCT",
        "collection": "sentinel-2-l2a",
        "properties": {"datetime": "2025-01-25T09:31:59Z"},
        "assets": {
            name: {
                "href": f"s3://eodata/S2_PRODUCT/{name}.bin",
                "file:checksum": _multihash(0x16, b"synthetic", "sha3_256"),
            }
            for name in ("red-asset", "nir-asset", "scl-asset", "xml-asset")
        },
    }
    roles = {
        "b04": "red-asset",
        "b08": "nir-asset",
        "scl": "scl-asset",
        "calibration_xml": "xml-asset",
    }
    calls = {}

    monkeypatch.setattr(ingestion, "search_sentinel2", lambda **_kwargs: [item])

    def fake_download(*, asset, destination_dir, **_kwargs):
        path = destination_dir / asset["href"].rsplit("/", maxsplit=1)[-1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
        return path

    def fake_build(**kwargs):
        calls["build"] = kwargs
        return {"product_id": item["id"]}

    def fake_write(manifest, destination):
        calls["write"] = (manifest, destination)
        return destination

    monkeypatch.setattr(ingestion, "download_asset", fake_download)
    monkeypatch.setattr(ingestion, "build_acquisition_manifest", fake_build)
    monkeypatch.setattr(ingestion, "write_acquisition_manifest", fake_write)

    result = ingestion.ingest_sentinel2_product(
        s3_client=object(),
        bbox=[9.3, 0.3, 9.6, 0.6],
        start_date="2025-01-01",
        end_date="2025-01-31",
        destination_dir=tmp_path,
        asset_names=list(roles.values()),
        manifest_asset_roles=roles,
    )

    assert result["manifest"] == {"product_id": "S2_PRODUCT"}
    assert result["manifest_path"].name == "acquisition_manifest.json"
    assert calls["build"]["product"] is item
    assert calls["build"]["asset_ids"] == roles
    assert set(calls["build"]["asset_paths"]) == set(roles)
    assert calls["build"]["stac_source"] == ingestion.STAC_API_URL
    assert calls["write"] == (
        {"product_id": "S2_PRODUCT"},
        result["manifest_path"],
    )


def test_ingest_manifest_requires_all_role_assets_to_be_requested(
    monkeypatch, tmp_path
):
    item = {
        "id": "S2_PRODUCT",
        "assets": {
            name: {"href": f"s3://eodata/S2_PRODUCT/{name}.jp2"}
            for name in ("red", "nir", "scl", "metadata")
        },
    }
    monkeypatch.setattr(ingestion, "search_sentinel2", lambda **_kwargs: [item])
    download = Mock()
    monkeypatch.setattr(ingestion, "download_asset", download)

    with pytest.raises(ValueError, match="inclus dans asset_names"):
        ingestion.ingest_sentinel2_product(
            s3_client=object(),
            bbox=[9.3, 0.3, 9.6, 0.6],
            start_date="2025-01-01",
            end_date="2025-01-31",
            destination_dir=tmp_path,
            asset_names=["red"],
            manifest_asset_roles={
                "b04": "red",
                "b08": "nir",
                "scl": "scl",
                "calibration_xml": "metadata",
            },
        )
    download.assert_not_called()


@pytest.mark.parametrize(
    ("checksum", "message"),
    [
        (None, "Checksum source absent ou invalide"),
        ("sha256:" + "0" * 64, "ne correspond pas au checksum"),
    ],
)
def test_ingest_refuses_unverified_cached_asset_when_writing_manifest(
    monkeypatch, tmp_path, checksum, message
):
    role_names = {
        "b04": "red",
        "b08": "nir",
        "scl": "scl",
        "calibration_xml": "metadata",
    }
    assets = {
        name: {"href": f"s3://eodata/S2_PRODUCT/{name}.bin"}
        for name in role_names.values()
    }
    if checksum is not None:
        assets["red"]["file:checksum"] = checksum
    item = {
        "id": "S2_PRODUCT",
        "assets": assets,
    }
    monkeypatch.setattr(ingestion, "search_sentinel2", lambda **_kwargs: [item])
    download = Mock()
    monkeypatch.setattr(ingestion, "download_asset", download)
    product_dir = tmp_path / "S2_PRODUCT"
    product_dir.mkdir()
    (product_dir / "red.bin").write_bytes(b"stale cached content")

    with pytest.raises(ValueError, match=message):
        ingestion.ingest_sentinel2_product(
            s3_client=object(),
            bbox=[9.3, 0.3, 9.6, 0.6],
            start_date="2025-01-01",
            end_date="2025-01-31",
            destination_dir=tmp_path,
            asset_names=list(role_names.values()),
            manifest_asset_roles=role_names,
        )
    download.assert_not_called()


def _encode_varint(value):
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _multihash(algorithm_code, content, algorithm):
    digest = hashlib.new(algorithm, content).digest()
    return (_encode_varint(algorithm_code) + _encode_varint(len(digest)) + digest).hex()


@pytest.mark.parametrize(
    ("checksum", "expected"),
    [
        ("sha256:" + hashlib.sha256(b"content").hexdigest(), ("sha256", hashlib.sha256(b"content").hexdigest())),
        (hashlib.sha256(b"content").hexdigest(), ("sha256", hashlib.sha256(b"content").hexdigest())),
        ("1220" + hashlib.sha256(b"content").hexdigest(), ("sha256", hashlib.sha256(b"content").hexdigest())),
        (_multihash(0x16, b"content", "sha3_256"), ("sha3_256", hashlib.sha3_256(b"content").hexdigest())),
        (_multihash(0xD5, b"content", "md5"), ("md5", hashlib.md5(b"content").hexdigest())),
    ],
)
def test_stac_checksum_accepts_confirmed_multihash_and_legacy_sha256(checksum, expected):
    assert ingestion._stac_checksum({"file:checksum": checksum}) == expected


@pytest.mark.parametrize(
    "checksum",
    [
        "f00120" + "00" * 32,  # unknown multicodec
        "d520" + "00" * 32,  # MD5 code with an impossible 32-byte digest
        "1221" + "00" * 32,  # SHA2-256 code with an invalid declared size
        "not-a-checksum",
    ],
)
def test_stac_checksum_rejects_unknown_or_malformed_formats(checksum):
    with pytest.raises(ValueError, match="checksum STAC non pris en charge"):
        ingestion._stac_checksum({"file:checksum": checksum})


@pytest.mark.parametrize("alter_cached_content", [False, True])
def test_manifest_cache_verifies_sha3_multihash(monkeypatch, tmp_path, alter_cached_content):
    roles = {"b04": "red", "b08": "nir", "scl": "scl", "calibration_xml": "metadata"}
    contents = {name: f"synthetic-{name}".encode() for name in roles.values()}
    assets = {
        name: {
            "href": f"s3://eodata/S2_PRODUCT/{name}.bin",
            "file:checksum": _multihash(0x16, contents[name], "sha3_256"),
        }
        for name in roles.values()
    }
    item = {"id": "S2_PRODUCT", "assets": assets}
    monkeypatch.setattr(ingestion, "search_sentinel2", lambda **_kwargs: [item])
    product_dir = tmp_path / "S2_PRODUCT"
    product_dir.mkdir()
    cache_paths = {}
    for name in roles.values():
        path = product_dir / f"{name}.bin"
        path.write_bytes(contents[name])
        cache_paths[name] = path
    if alter_cached_content:
        cache_paths["red"].write_bytes(b"altered cached content")

    monkeypatch.setattr(
        ingestion,
        "download_asset",
        lambda *, asset, **_kwargs: cache_paths[asset["href"].rsplit("/", 1)[-1].removesuffix(".bin")],
    )
    monkeypatch.setattr(ingestion, "build_acquisition_manifest", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(ingestion, "write_acquisition_manifest", lambda manifest, destination: destination)

    call = lambda: ingestion.ingest_sentinel2_product(
        s3_client=object(),
        bbox=[9, 0, 10, 1],
        start_date="2025-01-01",
        end_date="2025-01-31",
        destination_dir=tmp_path,
        asset_names=list(roles.values()),
        manifest_asset_roles=roles,
    )
    if alter_cached_content:
        with pytest.raises(ValueError, match="ne correspond pas au checksum"):
            call()
    else:
        assert call()["manifest"] == {"ok": True}


def _safe_manifest_fixture(xml_bytes):
    import hashlib

    digest = hashlib.sha3_256(xml_bytes).hexdigest()
    return (
        "<xfdu><dataObjectSection><dataObject><byteStream>"
        "<fileLocation href='S2_PRODUCT.SAFE/MTD_MSIL2A.xml'/>"
        f"<checksum checksumName='SHA3-256'>{digest}</checksum>"
        "</byteStream></dataObject></dataObjectSection></xfdu>"
    ).encode()


def _fallback_integrity_item(xml_bytes, safe_bytes):
    roles = {"b04": "red", "b08": "nir", "scl": "scl", "calibration_xml": "metadata"}
    assets = {
        name: {
            "href": f"s3://eodata/S2_PRODUCT/{name}.bin",
            "file:checksum": _multihash(0x16, b"synthetic", "sha3_256"),
        }
        for name in ("red", "nir", "scl")
    }
    assets["metadata"] = {
        "href": "s3://eodata/S2_PRODUCT/MTD_MSIL2A.xml",
        "file:local_path": "S2_PRODUCT.SAFE/MTD_MSIL2A.xml",
        # The declared multihash uses the MD5 code with an invalid 32-byte digest.
        "file:checksum": "d520" + "00" * 32,
    }
    assets["safe_manifest"] = {
        "href": "s3://eodata/S2_PRODUCT/manifest.safe",
        "file:size": len(safe_bytes),
        "file:checksum": _multihash(0xD5, safe_bytes, "md5"),
    }
    return {"id": "S2_PRODUCT", "assets": assets}, roles


@pytest.mark.parametrize("alter_xml_cache", [False, True])
def test_invalid_xml_checksum_uses_verified_safe_manifest_for_cache(
    monkeypatch, tmp_path, alter_xml_cache
):
    xml_bytes = b"<product>synthetic calibration</product>"
    safe_bytes = _safe_manifest_fixture(xml_bytes)
    item, roles = _fallback_integrity_item(xml_bytes, safe_bytes)
    monkeypatch.setattr(ingestion, "search_sentinel2", lambda **_kwargs: [item])
    product_dir = tmp_path / item["id"]
    product_dir.mkdir()
    (product_dir / "MTD_MSIL2A.xml").write_bytes(
        b"altered" if alter_xml_cache else xml_bytes
    )
    downloaded = []

    def fake_download(*, asset, destination_dir, **_kwargs):
        downloaded.append(asset["href"].rsplit("/", 1)[-1])
        content = safe_bytes if asset is item["assets"]["safe_manifest"] else xml_bytes if asset is item["assets"]["metadata"] else b"synthetic"
        path = destination_dir / asset["href"].rsplit("/", 1)[-1]
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(content)
        return path

    monkeypatch.setattr(ingestion, "download_asset", fake_download)
    def fake_small_download(*, destination, **_kwargs):
        downloaded.append("manifest.safe")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(safe_bytes)
        return destination

    monkeypatch.setattr(ingestion, "_download_small_stac_asset", fake_small_download)
    captured = {}
    monkeypatch.setattr(
        ingestion,
        "build_acquisition_manifest",
        lambda **kwargs: captured.update(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(ingestion, "write_acquisition_manifest", lambda manifest, destination: destination)
    call = lambda: ingestion.ingest_sentinel2_product(
        s3_client=object(),
        bbox=[9, 0, 10, 1],
        start_date="2025-01-01",
        end_date="2025-01-31",
        destination_dir=tmp_path,
        asset_names=list(roles.values()),
        manifest_asset_roles=roles,
    )

    if alter_xml_cache:
        with pytest.raises(ValueError, match="cache XML ne correspond pas"):
            call()
    else:
        result = call()
        assert "manifest.safe" in downloaded
        assert result["manifest"] == {"ok": True}
        integrity = captured["integrity"]
        xml_record = integrity["source_assets"]["calibration_xml"]
        assert integrity["mode"] == "strict"
        assert xml_record["status"] == "verified"
        assert xml_record["method"] == "safe_manifest_component_checksum"
        assert xml_record["declared_checksum_status"] == "invalid"
        assert integrity["evidence"]["safe_manifest"]["asset_id"] == "safe_manifest"


def test_strict_rejects_invalid_xml_checksum_without_alternative(monkeypatch, tmp_path):
    xml_bytes = b"<product>synthetic calibration</product>"
    item, roles = _fallback_integrity_item(xml_bytes, b"unused")
    item["assets"].pop("safe_manifest")
    monkeypatch.setattr(ingestion, "search_sentinel2", lambda **_kwargs: [item])
    download = Mock()
    monkeypatch.setattr(ingestion, "download_asset", download)

    with pytest.raises(ValueError, match="aucune preuve source alternative"):
        ingestion.ingest_sentinel2_product(
            s3_client=object(),
            bbox=[9, 0, 10, 1],
            start_date="2025-01-01",
            end_date="2025-01-31",
            destination_dir=tmp_path,
            asset_names=list(roles.values()),
            manifest_asset_roles=roles,
        )
    download.assert_not_called()


def test_strict_rejects_invalid_safe_manifest_checksum(monkeypatch, tmp_path):
    xml_bytes = b"<product>synthetic calibration</product>"
    safe_bytes = _safe_manifest_fixture(xml_bytes)
    item, roles = _fallback_integrity_item(xml_bytes, safe_bytes)
    item["assets"]["safe_manifest"]["file:checksum"] = "d520" + "00" * 32
    monkeypatch.setattr(ingestion, "search_sentinel2", lambda **_kwargs: [item])
    download = Mock()
    monkeypatch.setattr(ingestion, "download_asset", download)
    monkeypatch.setattr(
        ingestion,
        "_download_small_stac_asset",
        lambda **_kwargs: pytest.fail("Un checksum SAFE invalide ne doit pas déclencher le téléchargement."),
    )

    with pytest.raises(ValueError, match="aucune preuve source alternative"):
        ingestion.ingest_sentinel2_product(
            s3_client=object(),
            bbox=[9, 0, 10, 1],
            start_date="2025-01-01",
            end_date="2025-01-31",
            destination_dir=tmp_path,
            asset_names=list(roles.values()),
            manifest_asset_roles=roles,
        )
    download.assert_not_called()


def test_exploratory_mode_records_unconfirmed_source_and_refuses_cache(
    monkeypatch, tmp_path
):
    xml_bytes = b"<product>synthetic calibration</product>"
    item, roles = _fallback_integrity_item(xml_bytes, b"unused")
    item["assets"].pop("safe_manifest")
    monkeypatch.setattr(ingestion, "search_sentinel2", lambda **_kwargs: [item])
    product_dir = tmp_path / item["id"]
    product_dir.mkdir()
    cache_xml = product_dir / "MTD_MSIL2A.xml"
    cache_xml.write_bytes(xml_bytes)
    download = Mock()

    with pytest.raises(ValueError, match="Cache XML sans checksum source vérifiable"):
        ingestion.ingest_sentinel2_product(
            s3_client=object(),
            bbox=[9, 0, 10, 1],
            start_date="2025-01-01",
            end_date="2025-01-31",
            destination_dir=tmp_path,
            asset_names=list(roles.values()),
            manifest_asset_roles=roles,
            source_integrity_mode="exploratory",
        )
    download.assert_not_called()

    cache_xml.unlink()
    for role, name in roles.items():
        item["assets"][name]["file:checksum"] = _multihash(0x16, b"synthetic", "sha3_256") if role != "calibration_xml" else "d520" + "00" * 32
    download_calls = []

    def fake_download(*, asset, destination_dir, **_kwargs):
        name = asset["href"].rsplit("/", 1)[-1]
        download_calls.append(name)
        path = destination_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
        return path

    monkeypatch.setattr(ingestion, "download_asset", fake_download)
    captured = {}
    monkeypatch.setattr(ingestion, "build_acquisition_manifest", lambda **kwargs: captured.update(kwargs) or {"ok": True})
    monkeypatch.setattr(ingestion, "write_acquisition_manifest", lambda manifest, destination: destination)
    result = ingestion.ingest_sentinel2_product(
        s3_client=object(),
        bbox=[9, 0, 10, 1],
        start_date="2025-01-01",
        end_date="2025-01-31",
        destination_dir=tmp_path,
        asset_names=list(roles.values()),
        manifest_asset_roles=roles,
        source_integrity_mode="exploratory",
    )
    record = captured["integrity"]["source_assets"]["calibration_xml"]
    assert result["manifest"] == {"ok": True}
    assert record["status"] == "unconfirmed"
    assert record["method"] == "none"
    assert captured["integrity"]["mode"] == "exploratory"
