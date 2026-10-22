"""Bounded external HTTP client, remote COG windows and write-once layer storage; offline."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import socket

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.data.external_http import ExternalHttpClient, HostPolicy, ResponseTooLarge
from src.data.external_sources import (
    ExternalLayerDescriptor, TemporalValidity, descriptor_from_record, descriptor_to_record,
)
from src.data.layer_store import FileLayerRepository, LayerFileStore, publish_new_file
from src.data.network import NetworkError
from src.data.remote_window import ByteBudget, ByteBudgetExceeded, RangeFile, read_remote_window
from src.data.worldcover import WorldCoverAdapter
from src.network_config import NetworkSettings

UTC = timezone.utc


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Appel réseau interdit dans ces tests.")
    monkeypatch.setattr(socket, "create_connection", refuse)


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code, self.body = status, body
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    @contextmanager
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        yield self.responses.pop(0)


def settings(**overrides):
    values = dict(max_attempts=3, backoff_seconds=0, max_delay_seconds=1, wait_budget_seconds=5)
    values.update(overrides)
    return NetworkSettings(**values)


def client(responses, hosts=None, clock=None, sleeps=None):
    times = iter(clock or [0.0] * 50)
    return ExternalHttpClient(settings(), session=FakeSession(responses), hosts=hosts,
                              clock=lambda: next(times), sleep=(sleeps.append if sleeps is not None else lambda s: None))


@pytest.mark.parametrize("url", [
    "http://overpass-api.de/api/interpreter", "https://evil.example.org/x", "https://user:pw@overpass-api.de/x",
    "https://overpass-api.de:8443/x", "file:///etc/passwd"])
def test_only_allow_listed_https_hosts(url):
    with pytest.raises(ValueError, match="non autorisé"):
        client([]).request("GET", url)


def test_requests_are_throttled_metered_and_never_redirected():
    sleeps = []
    http = client([FakeResponse(body=b"a"), FakeResponse(body=b"bc")], clock=[0.0, 2.0, 2.0], sleeps=sleeps)
    http.request("GET", "https://overpass-api.de/api/status")
    http.request("GET", "https://overpass-api.de/api/status")
    assert sleeps == [pytest.approx(8.0)]  # 10 s minimum interval for Overpass.
    assert http.usage_summary() == {"overpass-api.de": {"requests": 2, "bytes": 3}}
    method, url, kwargs = http.session.calls[0]
    assert kwargs["allow_redirects"] is False and kwargs["verify"] is True and kwargs["stream"] is True
    assert kwargs["headers"]["User-Agent"].startswith("GEONAF/")


def test_size_ceiling_is_enforced_without_retry():
    hosts = {"overpass-api.de": HostPolicy("overpass", 0, 4)}
    http = client([FakeResponse(body=b"12345")], hosts=hosts)
    with pytest.raises(ResponseTooLarge):
        http.request("GET", "https://overpass-api.de/x")
    streamed = FakeResponse(body=b"12345")
    del streamed.headers["Content-Length"]  # Undeclared size: counted while streaming.
    http = client([streamed], hosts=hosts)
    with pytest.raises(ResponseTooLarge):
        http.request("GET", "https://overpass-api.de/x")
    assert len(http.session.calls) == 1


def test_transient_errors_are_retried_but_redirects_are_failures():
    http = client([FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(body=b"ok")])
    assert http.request("GET", "https://overpass-api.de/x").content == b"ok"
    http = client([FakeResponse(302, headers={"Location": "https://evil.example.org"})])
    with pytest.raises(NetworkError) as error:
        http.request("GET", "https://overpass-api.de/x")
    assert error.value.status_code == 302 and len(http.session.calls) == 1


def write_cog(path, data, *, crs="EPSG:32632", transform=None, nodata=None, block=64):
    transform = transform or from_origin(500000, 100000, 10, 10)
    with rasterio.open(path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
                       dtype=data.dtype, crs=crs, transform=transform, tiled=True, blockxsize=block,
                       blockysize=block, compress="deflate", nodata=nodata) as dst:
        dst.write(data, 1)
    return path


class BytesSource:
    def __init__(self, payload):
        self.payload, self.size, self.calls = payload, len(payload), 0

    def fetch(self, start, end):
        self.calls += 1
        return self.payload[start:end + 1]


def test_remote_window_reads_only_needed_tiles_within_budget(tmp_path):
    data = np.random.default_rng(0).integers(0, 65535, (512, 512), dtype=np.uint16)  # Incompressible.
    path = write_cog(tmp_path / "cog.tif", data)
    payload = path.read_bytes()
    bounds = (500000 + 100 * 10, 100000 - 180 * 10, 500000 + 160 * 10, 100000 - 120 * 10)
    remote = RangeFile(BytesSource(payload), ByteBudget(len(payload)), label="remote.tif")
    array, profile, estimate = read_remote_window(remote, bounds, "EPSG:32632")
    np.testing.assert_array_equal(array, data[120:180, 100:160])
    assert profile["transform"].c == 501000 and profile["width"] == 60
    assert estimate.tiles == 4 and 0 < estimate.compressed_bytes < len(payload)
    assert remote.received < len(payload)
    small = RangeFile(BytesSource(payload), ByteBudget(remote.BLOCK * 2), label="remote.tif")
    with pytest.raises(ByteBudgetExceeded):
        read_remote_window(small, bounds, "EPSG:32632")
    with pytest.raises(FileNotFoundError):
        remote.view("other.tif")


def test_store_is_write_once_and_keys_are_confined(tmp_path):
    store = LayerFileStore(tmp_path / "external")
    store.write_bytes("osm/a.json", b"1")
    with pytest.raises(FileExistsError):
        store.write_bytes("osm/a.json", b"2")
    assert store.path("osm/a.json").read_bytes() == b"1"
    assert not list(store.path("osm").glob(".tmp-*"))
    for key in ("../x", "/abs", "a\\b", "C:/x", ""):
        with pytest.raises(ValueError):
            store.path(key)
    temporary = tmp_path / "t"
    temporary.write_bytes(b"x")
    with pytest.raises(FileExistsError):
        publish_new_file(temporary, store.path("osm/a.json"))


def test_repository_round_trip_find_and_tamper_detection(tmp_path):
    store = LayerFileStore(tmp_path / "external")
    repository = FileLayerRepository(store)
    path = write_cog(tmp_path / "wc.tif", np.full((4, 4), 10, np.uint8), crs="EPSG:4326",
                     transform=from_origin(9.4, 0.4, 0.001, 0.001), block=16)
    descriptor = WorldCoverAdapter().describe_local(path, retrieved_at=datetime(2026, 10, 22, tzinfo=UTC))
    older = ExternalLayerDescriptor(**{**descriptor.__dict__, "version": "v100", "temporal": TemporalValidity(
        "interval", datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))})
    assert descriptor_from_record(json.loads(json.dumps(descriptor_to_record(descriptor)))) == descriptor
    key = repository.save(descriptor, "worldcover/a.tif")
    assert repository.save(descriptor, "worldcover/a.tif") == key  # Idempotent, file not rewritten.
    repository.save(older, "worldcover/b.tif")
    found = repository.find_with_storage("esa-worldcover", (9.3, 0.3, 9.5, 0.5), datetime(2025, 1, 25, tzinfo=UTC))
    assert [(d.version, k) for d, k in found] == [("v200", "worldcover/a.tif"), ("v100", "worldcover/b.tif")]
    assert not repository.find("esa-worldcover", (0, 0, 1, 1))
    record = json.loads(store.path(key).read_text(encoding="utf-8"))
    assert str(tmp_path) not in store.path(key).read_text(encoding="utf-8")
    record["descriptor"]["product"] = "falsifié"
    with pytest.raises(ValueError, match="Empreinte"):
        descriptor_from_record(record["descriptor"])
    with pytest.raises(ValueError):
        repository.save(descriptor, "../outside.tif")
