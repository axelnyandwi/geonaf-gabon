"""OSM geometry/caching, WorldCover window retrieval and Sentinel-1 mosaics; fake transports, offline."""

from datetime import date, datetime, timedelta, timezone
import json
import socket

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.data.external_http import ExternalResponse
from src.data.layer_store import FileLayerRepository, LayerFileStore
from src.data.openstreetmap import (
    IncompleteOverpassResponse, OpenStreetMapAdapter, fetch_osm_snapshot, load_osm_snapshot,
)
from src.data.remote_window import ByteBudgetExceeded
from src.data.sentinel1 import (
    describe_window, fetch_sentinel1_window, parse_mosaic_item, search_body, search_mosaics,
)
from src.data.worldcover import fetch_worldcover_window
from tests.test_external_retrieval_core import write_cog

UTC = timezone.utc
AKANDA = (9.388, 0.354, 9.496, 0.463)
NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Appel réseau interdit dans ces tests.")
    monkeypatch.setattr(socket, "create_connection", refuse)


def ring(*points):
    return [{"lat": lat, "lon": lon} for lon, lat in points]


def overpass(*elements, remark=None):
    payload = {"osm3s": {"timestamp_osm_base": "2026-10-20T08:00:00Z"}, "elements": list(elements)}
    if remark:
        payload["remark"] = remark
    return payload


SQUARE_A = ring((9.40, 0.40), (9.42, 0.40), (9.42, 0.42))       # First half of the outer ring…
SQUARE_B = ring((9.42, 0.42), (9.40, 0.42), (9.40, 0.40))       # …closed by a second way.
HOLE = ring((9.405, 0.405), (9.41, 0.405), (9.41, 0.41), (9.405, 0.41), (9.405, 0.405))


def relation(id_, members, tags=None):
    return {"type": "relation", "id": id_, "tags": {"type": "multipolygon", "natural": "wetland", **(tags or {})},
            "members": members}


def test_query_requests_multipolygon_relations_with_server_limits():
    query = OpenStreetMapAdapter().overpass_query(AKANDA)
    assert 'relation["landuse"]["type"="multipolygon"]' in query and "[maxsize:" in query
    assert 'relation["highway"]' not in query  # Linear features only as ways.
    with pytest.raises(ValueError):
        OpenStreetMapAdapter().overpass_query(AKANDA, timeout_s=600)


def test_multipolygon_assembly_and_strict_rejection_of_incomplete_geometries():
    payload = overpass(
        relation(1, [{"type": "way", "role": "outer", "geometry": SQUARE_A},
                     {"type": "way", "role": "outer", "geometry": SQUARE_B},
                     {"type": "way", "role": "inner", "geometry": HOLE},
                     {"type": "node", "role": "label", "lat": 0.41, "lon": 9.41}]),
        relation(2, [{"type": "way", "role": "outer", "geometry": SQUARE_A}]),             # Unclosed ring.
        relation(3, [{"type": "way", "role": "outer", "geometry": SQUARE_A},
                     {"type": "way", "role": "outer"}]),                                     # Member without geometry.
        {"type": "way", "id": 4, "tags": {"building": "yes"},
         "geometry": ring((9.43, 0.43), (9.44, 0.44), (9.44, 0.43), (9.43, 0.44), (9.43, 0.43))},  # Bow-tie.
        {"type": "way", "id": 5, "tags": {"highway": "primary"},
         "geometry": [{"lat": 0.42, "lon": 9.42}, {"lat": None, "lon": 9.43}]},              # Missing vertex.
        {"type": "way", "id": 6, "tags": {"landuse": "forest"}, "geometry": ring((9.45, 0.45), (9.46, 0.46))},
        {"type": "node", "id": 7, "lat": 0.4, "lon": 9.4})
    frame, descriptor = OpenStreetMapAdapter().parse_overpass(payload, retrieved_at=NOW, wgs84_bounds=AKANDA)
    assert list(frame["osm_id"]) == ["relation/1"]
    polygon = frame.geometry.iloc[0]
    assert polygon.geom_type == "Polygon" and len(polygon.interiors) == 1
    assert polygon.area == pytest.approx(0.02 ** 2 - 0.005 ** 2)
    assert frame["group"].iloc[0] == "Zones humides (OSM)"
    p = descriptor.provenance
    assert (p["rejected_incomplete"], p["rejected_invalid"], p["rejected_unsupported"]) == ("4", "1", "1")
    assert p["relations"] == "1" and p["skipped_elements"] == "6"


def test_runtime_remark_marks_response_as_partial():
    with pytest.raises(IncompleteOverpassResponse):
        OpenStreetMapAdapter().parse_overpass(overpass(remark="runtime error: Query timed out in \"query\""),
                                              retrieved_at=NOW, wgs84_bounds=AKANDA)


class FakeOverpass:
    def __init__(self, *payloads):
        self.payloads, self.calls = list(payloads), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return ExternalResponse(200, {}, json.dumps(self.payloads.pop(0)).encode())


def building(id_, lon):
    return {"type": "way", "id": id_, "tags": {"building": "yes"},
            "geometry": ring((lon, 0.40), (lon + 0.001, 0.40), (lon + 0.001, 0.401), (lon, 0.40))}


def test_osm_snapshot_cache_refresh_and_partial_responses_are_never_stored(tmp_path):
    store = LayerFileStore(tmp_path / "external")
    repository = FileLayerRepository(store)
    client = FakeOverpass(overpass(building(1, 9.40)), overpass(remark="runtime error: out of memory"),
                          overpass(building(1, 9.40), building(2, 9.41)))
    frame, descriptor, report = fetch_osm_snapshot(client, store, repository, wgs84_bounds=AKANDA, now=NOW)
    assert len(frame) == 1 and report["requests"] == 1 and not report["reused"]
    method, url, kwargs = client.calls[0]
    assert method == "POST" and url == "https://overpass-api.de/api/interpreter" and "out geom" in kwargs["data"]["data"]
    _, again, report = fetch_osm_snapshot(client, store, repository, wgs84_bounds=AKANDA, now=NOW + timedelta(days=1))
    assert report["reused"] and len(client.calls) == 1 and again.fingerprint == descriptor.fingerprint
    with pytest.raises(IncompleteOverpassResponse):
        fetch_osm_snapshot(client, store, repository, wgs84_bounds=AKANDA, now=NOW + timedelta(days=2),
                           force_refresh=True)
    assert len(store.keys("openstreetmap")) == 1
    frame, _, report = fetch_osm_snapshot(client, store, repository, wgs84_bounds=AKANDA,
                                          now=NOW + timedelta(days=8))  # Cache older than 7 days.
    assert len(frame) == 2 and not report["reused"] and len(store.keys("openstreetmap")) == 2
    assert descriptor.quality.checksum_sha256 and descriptor.temporal.kind == "snapshot"
    from src.services.external_layers import stored_layers

    assert len(stored_layers(repository, "openstreetmap", AKANDA)) >= 2
    key = report["storage_key"]
    raw = store.path(key).read_bytes()
    store.path(key).write_bytes(raw.replace(b'"id":2', b'"id":3'))
    with pytest.raises(ValueError, match="modifié"):
        stored_layers(repository, "openstreetmap", AKANDA)


class FakeRangeHttp:
    """Serves a local COG as if it were the WorldCover tile."""

    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def policy(self, url):
        assert url.startswith("https://esa-worldcover.s3.eu-central-1.amazonaws.com/")
        return url, None

    def request(self, method, url, *, headers=None, **kwargs):
        self.calls.append(method)
        if method == "HEAD":
            return ExternalResponse(200, {"Content-Length": str(len(self.payload))}, b"")
        start, end = (int(v) for v in headers["Range"].removeprefix("bytes=").split("-"))
        return ExternalResponse(206, {}, self.payload[start:end + 1])


def worldcover_tile(tmp_path):
    size = 1080  # 0.09° at 1/12 000°: large enough that the AOI window is a partial read.
    data = np.random.default_rng(1).choice(np.array([10, 50, 80, 95], np.uint8), (size, size))
    return write_cog(tmp_path / "tile.tif", data, crs="EPSG:4326", transform=from_origin(9.39, 0.46, 1 / 12000, 1 / 12000),
                     nodata=0, block=256), data


def test_worldcover_window_is_fetched_once_within_cap_then_reused(tmp_path):
    tile, data = worldcover_tile(tmp_path)
    bounds = (9.40, 0.40, 9.42, 0.42)
    store = LayerFileStore(tmp_path / "external")
    repository = FileLayerRepository(store)
    client = FakeRangeHttp(tile.read_bytes())
    with pytest.raises(ByteBudgetExceeded):
        fetch_worldcover_window(client, store, repository, aoi_id="test", wgs84_bounds=bounds, max_bytes=70_000)
    assert not store.keys("worldcover")
    path, descriptor, report = fetch_worldcover_window(client, store, repository, aoi_id="test", wgs84_bounds=bounds,
                                                       now=NOW)
    assert not report["reused"] and 0 < report["network_bytes"] < len(tile.read_bytes())
    with rasterio.open(path) as window:
        assert window.crs.to_epsg() == 4326 and window.width == 240
        np.testing.assert_array_equal(window.read(1), data[480:720, 120:360])
    assert descriptor.provenance["aoi_id"] == "test" and descriptor.provenance["retrieved_at_basis"] == "requête HTTP Range"
    calls = len(client.calls)
    _, again, report = fetch_worldcover_window(None, store, repository, aoi_id="test", wgs84_bounds=bounds)
    assert report["reused"] and len(client.calls) == calls and again == descriptor
    with pytest.raises(ValueError, match="Identifiant"):
        fetch_worldcover_window(client, store, repository, aoi_id="../x", wgs84_bounds=bounds)


def mosaic_item(**overrides):
    asset = lambda pol: {"href": f"s3://eodata/Global-Mosaics/Sentinel-1/S1SAR_L3_IW_MCM/2025/01/01/M/{pol}.tif",
                         "type": "image/tiff; application=geotiff; profile=cloud-optimized", "file:size": 1000,
                         "proj:code": "EPSG:32632", "nodata": -32768}
    item = {"id": "Sentinel-1_IW_mosaic_2025_M01_32NNF_0_0", "collection": "sentinel-1-global-mosaics",
            "bbox": [9.0, 0.0, 9.9, 0.9],
            "properties": {"product:type": "S1SAR_L3_IW_MCM", "sar:instrument_mode": "IW",
                           "sar:polarizations": ["VH", "VV"], "gsd": 20, "grid:code": "MGRS-32NNF",
                           "start_datetime": "2025-01-01T00:00:00.000Z", "end_datetime": "2025-01-31T23:59:59.000Z",
                           "processing:facility": "Sinergise Solutions", "published": "2025-06-19T22:42:23Z"},
            "assets": {"VV": asset("VV"), "VH": asset("VH")}}
    for key, value in overrides.items():
        target, name = (item["properties"], key[5:]) if key.startswith("prop_") else (item, key)
        target[name] = value
    return item


@pytest.mark.parametrize("change", [
    {"collection": "sentinel-1-grd"}, {"prop_product:type": "IW_GRDH_1S"}, {"prop_sar:polarizations": ["VV"]},
    {"assets": {"VV": {"href": "s3://eodata/other/VV.tif"}}}, {"prop_gsd": None}])
def test_only_validated_iw_mosaics_are_accepted(change):
    mosaic = parse_mosaic_item(mosaic_item())
    assert mosaic.month == "2025-01" and mosaic.crs == "EPSG:32632" and mosaic.gsd_m == 20
    assert mosaic.assets["VH"].s3_key.startswith("Global-Mosaics/Sentinel-1/")
    with pytest.raises((ValueError, KeyError)):
        parse_mosaic_item(mosaic_item(**change))


def test_search_is_bounded_and_reports_rejections():
    with pytest.raises(ValueError):
        search_body((0, 0, 3, 3), date(2025, 1, 1), date(2025, 1, 31))
    with pytest.raises(ValueError):
        search_body(AKANDA, date(2023, 1, 1), date(2025, 2, 1))

    class FakeStac:
        settings = type("S", (), {"stac_endpoint": "https://stac.dataspace.copernicus.eu/v1"})()

        def request(self, href, method, parameters=None):
            assert href.endswith("/search") and method == "POST" and parameters["collections"] == [
                "sentinel-1-global-mosaics"]
            return json.dumps({"features": [mosaic_item(), mosaic_item(collection="x")], "links": []})

    mosaics, rejected, truncated = search_mosaics(FakeStac(), AKANDA, date(2025, 1, 1), date(2025, 1, 31))
    assert len(mosaics) == 1 and rejected == 1 and not truncated


class FakeS3:
    def __init__(self, payload):
        self.payload, self.gets = payload, 0

    def head_object(self, Bucket, Key):
        assert Bucket == "eodata" and Key.startswith("Global-Mosaics/")
        return {"ContentLength": len(self.payload)}

    def get_object(self, Bucket, Key, Range):
        import io

        self.gets += 1
        start, end = (int(v) for v in Range.removeprefix("bytes=").split("-"))
        return {"Body": io.BytesIO(self.payload[start:end + 1])}


def radar_cog(tmp_path, values=None):
    data = values if values is not None else np.random.default_rng(2).uniform(0.001, 0.5, (600, 600)).astype("float32")
    return write_cog(tmp_path / "vv.tif", data, transform=from_origin(560000, 52000, 20, 20), nodata=-32768,
                     block=256), data


def test_sentinel1_window_respects_cap_and_records_provenance(tmp_path, monkeypatch):
    from src.network_config import NetworkSettings

    monkeypatch.setattr(NetworkSettings, "from_environment", classmethod(lambda cls, **kw: cls(max_attempts=1)))
    tile, data = radar_cog(tmp_path)
    mosaic = parse_mosaic_item(mosaic_item())
    store = LayerFileStore(tmp_path / "external")
    repository = FileLayerRepository(store)
    s3 = FakeS3(tile.read_bytes())
    bounds = (9.5435, 0.4250, 9.5615, 0.4430)  # ≈2 km box inside the synthetic tile.
    with pytest.raises(ByteBudgetExceeded):
        fetch_sentinel1_window(s3, store, repository, mosaic, "VV", aoi_id="t", wgs84_bounds=bounds,
                               max_bytes=65_536)
    assert not store.keys("sentinel1")
    path, descriptor, report = fetch_sentinel1_window(s3, store, repository, mosaic, "VV", aoi_id="t",
                                                      wgs84_bounds=bounds, now=NOW)
    assert not report["reused"] and report["network_bytes"] < len(tile.read_bytes())
    gets = s3.gets
    _, again, report = fetch_sentinel1_window(s3, store, repository, mosaic, "VV", aoi_id="t", wgs84_bounds=bounds)
    assert report["reused"] and s3.gets == gets and again == descriptor
    with rasterio.open(path) as window:
        assert window.nodata == -32768 and window.dtypes[0] == "float32" and window.res == (20, 20)
    assert descriptor.kind == "continuous_raster" and descriptor.resolution_m == 20
    assert descriptor.temporal.kind == "interval" and descriptor.temporal.start_utc == datetime(2025, 1, 1, tzinfo=UTC)
    assert descriptor.license.spdx_id.startswith("LicenseRef-Copernicus") and "[2025]" in descriptor.license.attribution
    assert any("γ⁰ aplani" in note and "10·log10" in note for note in descriptor.quality.notes)
    assert descriptor.provenance["polarization"] == "VV" and "s3_key" in descriptor.provenance


def test_radar_layer_units_masks_and_per_class_statistics(tmp_path):
    import geopandas as gpd
    from shapely.geometry import box
    from src.data.worldcover import WorldCoverAdapter
    from src.services.sentinel1_layer import load_radar_layer

    values = np.full((4, 4), 0.1, np.float32)
    values[0, 0], values[0, 1], values[1, 0] = -32768, np.nan, 0.0
    tile, _ = radar_cog(tmp_path, values)
    descriptor = describe_window(tile, parse_mosaic_item(mosaic_item()), "VV", retrieved_at=NOW)
    aoi = tmp_path / "aoi.geojson"
    gpd.GeoDataFrame(geometry=[box(560000, 51920, 560080, 52000)], crs="EPSG:32632").to_file(aoi)
    classes = np.full((8, 8), 10, np.uint8)
    classes[4:, :] = 80
    landcover = write_cog(tmp_path / "wc.tif", classes, transform=from_origin(560000, 52000, 10, 10), nodata=0, block=16)
    wc = WorldCoverAdapter().describe_local(landcover, retrieved_at=NOW)
    layer = load_radar_layer(tile, descriptor, aoi_path=aoi, landcover_path=landcover,
                             landcover_classes=dict(wc.classes))
    assert layer.invalid_counts == {"nodata": 1, "non_fini": 1, "non_positif": 1}
    assert layer.median_db == pytest.approx(-10.0) and layer.coverage_percent == pytest.approx(13 / 16 * 100)
    amplitude = load_radar_layer(tile, descriptor, aoi_path=aoi, scale="amplitude")
    assert amplitude.median_db == pytest.approx(-20.0) and not amplitude.documented
    assert "non documentée" in amplitude.unit and layer.documented and "10·log10" in layer.unit
    zones = {zone.code: zone for zone in layer.per_class}
    assert zones[10].pixel_count == 5 and zones[80].pixel_count == 8  # Mode 10 m → 20 m.
    with pytest.raises(ValueError):
        load_radar_layer(tile, descriptor, aoi_path=aoi, scale="decibel")


def test_radiometry_decision_is_sourced_and_never_validated():
    from src.data.sentinel1 import RADIOMETRY, SCALE_CONVENTIONS
    from src.services.sentinel1_layer import DEFAULT_SCALE

    assert RADIOMETRY["quantity"] == "gamma0_terrain_flattened" and RADIOMETRY["scale"] == "linear_power"
    assert DEFAULT_SCALE == "intensity" and SCALE_CONVENTIONS[DEFAULT_SCALE] == 10.0
    assert all("https://" in source for source in RADIOMETRY["evidence"]) and len(RADIOMETRY["evidence"]) >= 2
    assert "non vérifiée" in RADIOMETRY["status"] and "Amplitude" in RADIOMETRY["inconsistency"]


@pytest.mark.parametrize("linear, expected_db", [(1.0, 0.0), (0.1, -10.0), (0.01, -20.0), (0.001, -30.0)])
def test_power_to_db_reference_values(tmp_path, linear, expected_db):
    import geopandas as gpd
    from shapely.geometry import box
    from src.services.sentinel1_layer import load_radar_layer

    tile, _ = radar_cog(tmp_path, np.full((2, 2), linear, np.float32))
    descriptor = describe_window(tile, parse_mosaic_item(mosaic_item()), "VH", retrieved_at=NOW)
    aoi = tmp_path / "aoi.geojson"
    gpd.GeoDataFrame(geometry=[box(560000, 51960, 560040, 52000)], crs="EPSG:32632").to_file(aoi)
    layer = load_radar_layer(tile, descriptor, aoi_path=aoi)
    assert layer.median_db == pytest.approx(expected_db, abs=1e-5) and layer.polarization == "VH"


# --- Public anonymous reads: no cloud credential lookup by rasterio (C2c) ---

@pytest.fixture
def credential_probe(monkeypatch):
    """Record any rasterio AWS session, boto3 credential lookup or metadata-service connection."""
    import boto3.session
    import rasterio.session

    events = []
    original_init = rasterio.session.AWSSession.__init__
    original_credentials = boto3.session.Session.get_credentials
    original_connect = socket.socket.connect

    def aws_session(self, *args, **kwargs):
        events.append("AWSSession")
        return original_init(self, *args, **kwargs)

    def get_credentials(self):
        events.append("get_credentials")
        return original_credentials(self)

    def connect(self, address):
        if isinstance(address, tuple) and str(address[0]).startswith("169.254."):
            events.append(f"connect {address[0]}")
            raise OSError("Service de métadonnées cloud interdit dans ces tests.")
        return original_connect(self, address)

    monkeypatch.setattr(rasterio.session.AWSSession, "__init__", aws_session)
    monkeypatch.setattr(boto3.session.Session, "get_credentials", get_credentials)
    monkeypatch.setattr(socket.socket, "connect", connect)
    return events


_AWS_OPTIONS = ("AWS_NO_SIGN_REQUEST", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def _gdal_aws_options():
    from rasterio._env import get_gdal_config

    return {name: get_gdal_config(name) for name in _AWS_OPTIONS}


class ConfigProbeRangeHttp(FakeRangeHttp):
    """Records the GDAL AWS options seen by the reading thread at each Range GET."""

    def __init__(self, payload):
        super().__init__(payload)
        self.seen = []

    def request(self, method, url, *, headers=None, **kwargs):
        if method == "GET":
            self.seen.append(_gdal_aws_options())
        return super().request(method, url, headers=headers, **kwargs)


def test_worldcover_public_read_never_looks_up_aws_credentials(tmp_path, credential_probe):
    import os

    environment, options = dict(os.environ), _gdal_aws_options()
    tile, data = worldcover_tile(tmp_path)
    store = LayerFileStore(tmp_path / "external")
    client = ConfigProbeRangeHttp(tile.read_bytes())
    bounds = (9.40, 0.40, 9.42, 0.42)

    with pytest.raises(ByteBudgetExceeded):  # Budget still enforced on the anonymous path.
        fetch_worldcover_window(client, store, FileLayerRepository(store), aoi_id="anon", wgs84_bounds=bounds,
                                max_bytes=70_000)
    assert not store.keys("worldcover")
    path, _, report = fetch_worldcover_window(client, store, FileLayerRepository(store), aoi_id="anon",
                                              wgs84_bounds=bounds, now=NOW)

    assert credential_probe == []
    with rasterio.open(path) as window:  # Same window and pixels as the tile itself.
        assert window.crs.to_epsg() == 4326 and window.width == 240
        np.testing.assert_array_equal(window.read(1), data[480:720, 120:360])
    assert 0 < report["network_bytes"] < len(tile.read_bytes())
    # Rasterio context isolation: no AWS option set or changed, even during the read.
    assert client.seen and all(seen == options for seen in client.seen)
    assert _gdal_aws_options() == options and dict(os.environ) == environment


def test_sentinel1_authenticated_read_keeps_its_s3_client(tmp_path, monkeypatch, credential_probe):
    from src.network_config import NetworkSettings

    monkeypatch.setattr(NetworkSettings, "from_environment", classmethod(lambda cls, **kw: cls(max_attempts=1)))
    tile, data = radar_cog(tmp_path)
    options, seen = _gdal_aws_options(), []

    class ProbeS3(FakeS3):
        def get_object(self, Bucket, Key, Range):
            seen.append(_gdal_aws_options())
            return super().get_object(Bucket, Key, Range)

    s3 = ProbeS3(tile.read_bytes())
    store = LayerFileStore(tmp_path / "external")
    path, descriptor, report = fetch_sentinel1_window(
        s3, store, FileLayerRepository(store), parse_mosaic_item(mosaic_item()), "VV", aoi_id="t",
        wgs84_bounds=(9.5435, 0.4250, 9.5615, 0.4430), now=NOW)
    # Bytes come only through GEONAF's (authenticated) S3 client; rasterio adds nothing.
    assert s3.gets > 0 and seen and all(value == options for value in seen)
    assert credential_probe == []
    assert not report["reused"] and report["network_bytes"] < len(tile.read_bytes())
    assert descriptor.provenance["s3_key"].startswith("Global-Mosaics/")
    with rasterio.open(path) as window:
        assert window.dtypes[0] == "float32" and window.res == (20, 20)


def test_anonymous_dataset_name_is_explicit_opt_in(tmp_path):
    from src.data.remote_window import ByteBudget, HttpRangeSource, RangeFile, S3RangeSource

    tile, _ = worldcover_tile(tmp_path)
    url = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/x.tif"
    client = FakeRangeHttp(tile.read_bytes())

    def remote(**kwargs):
        return RangeFile(HttpRangeSource(client, url, **kwargs), ByteBudget(1), label=url)

    assert remote().name == url  # Not inferred from the domain name: default unchanged.
    assert remote(anonymous="yes").name == url  # Only a real True opts in.
    first, second = remote(anonymous=True), remote(anonymous=True)
    assert first.name.startswith("anonymous-range/") and "amazonaws" not in first.name
    assert first.name != second.name  # rasterio allows one opener per dataset name.
    assert first.label == url
    with pytest.raises(FileNotFoundError):
        first.view(url)
    assert S3RangeSource.anonymous is False
