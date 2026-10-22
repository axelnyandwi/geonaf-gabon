"""Offline queue, transfer budgets and atomic publication contracts."""

from threading import Event
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import time

import pytest

from src.data.download_control import DownloadBudget, TransferControl, TransferAborted
from src.data.catalogue import CatalogueRecord, CatalogueResult, CatalogueQuery
from src.data.download_store import LocalDownloadStore
from src.services.acquisition_catalogue import load_catalogue_aoi
from src.services.download_plan import make_plan
from src.services.download_queue import DownloadQueue
from tests.test_local_preview import local_data


def test_transfer_enforces_cancellation_and_network_budget():
    cancelled = Event()
    control = TransferControl(DownloadBudget(network_bytes=10), cancelled)
    control.consume(6)
    with pytest.raises(TransferAborted, match="réseau"):
        control.consume(5)
    cancelled.set()
    with pytest.raises(TransferAborted, match="Annulation"):
        control.check()


@pytest.mark.parametrize("kwargs", [{"workers": 0}, {"workers": 3},
                                    {"memory_bytes": 1}, {"storage_bytes": -1}])
def test_invalid_budgets_are_rejected(kwargs):
    with pytest.raises(ValueError):
        DownloadBudget(**kwargs)


def _scenario(local_data):
    items, payloads, records = {}, {}, []
    for day in (15, 25):
        source = local_data / f"raw/sentinel2/product-{day}"
        manifest = json.loads((source / "acquisition_manifest.json").read_text())
        identifier = manifest["product_id"]
        assets = {}
        for role, asset in manifest["assets"].items():
            name = asset["path"]
            data = (source / name).read_bytes()
            key = f"Sentinel-2/{identifier}/{name}"
            payloads[key] = data
            asset_id = {"b04": "B04_10m", "b08": "B08_10m", "scl": "SCL_20m",
                        "calibration_xml": "product_metadata"}[role]
            assets[asset_id] = {"href": "s3://eodata/" + key, "file:size": len(data),
                                "file:checksum": "sha256:" + hashlib.sha256(data).hexdigest()}
        acquired = f"2025-01-{day}T09:30:00Z"
        items[identifier] = {"id": identifier, "collection": "sentinel-2-l2a",
                             "properties": {"datetime": acquired, "processing:level": "L2"}, "assets": assets}
        records.append(CatalogueRecord("sentinel-2-l2a", identifier,
                                       datetime.fromisoformat(acquired.replace("Z", "+00:00")),
                                       20.0, True, True, True))
    query = CatalogueQuery(load_catalogue_aoi(local_data, "akanda"), date(2025, 1, 1), date(2025, 1, 31))
    selection = CatalogueResult(query, tuple(records), True, 2, "https://stac.dataspace.copernicus.eu/v1",
                                datetime.now(timezone.utc))
    class Client:
        def __init__(self):
            self.calls = []
            self.fail = None
            self.hook = None

        def download_file(self, bucket, key, filename, Callback=None, **kwargs):
            assert bucket == "eodata" and kwargs["Config"].use_threads is False
            self.calls.append(key)
            if self.fail:
                raise self.fail
            payload = payloads[key]
            with open(filename, "wb") as stream:
                middle = len(payload) // 2
                for chunk in (payload[:middle], payload[middle:]):
                    stream.write(chunk)
                    if self.hook:
                        self.hook()
                    Callback(len(chunk))

        def close(self):
            pass
    return selection, items, Client()


def _wait(queue):
    deadline = time.monotonic() + 15
    while queue.busy and time.monotonic() < deadline:
        time.sleep(.01)
    assert not queue.busy
    return queue.snapshot()


def test_multiple_downloads_publish_valid_manifests_and_preserve_provenance(local_data):
    selection, items, client = _scenario(local_data)
    root = local_data / "managed"
    queue = DownloadQueue(root, resolver=lambda record: items[record.stac_id], client_factory=lambda: client)
    try:
        queue.prepare(selection, DownloadBudget(workers=2))
        assert _wait(queue)["phase"] == "ready" and not client.calls
        queue.start()
        snapshot = _wait(queue)
        assert [row["state"] for row in snapshot["rows"]] == ["completed", "completed"]
        assert len(client.calls) == 8
        for record in selection.records:
            folder = root / "raw/sentinel2" / record.stac_id
            manifest = json.loads((folder / "acquisition_manifest.json").read_text())
            assert manifest["integrity"]["mode"] == "strict"
            provenance = json.loads((folder / "download_provenance.json").read_text())
            assert provenance["query_fingerprint"] == selection.query.fingerprint
            assert provenance["item"]["properties"]["processing:level"] == "L2"
        queue.prepare(selection, DownloadBudget())
        assert all(row["state"] == "skipped" for row in _wait(queue)["rows"])
        assert len(client.calls) == 8
    finally:
        queue.close()


def test_duplicates_are_downloaded_once(local_data):
    selection, items, client = _scenario(local_data)
    selection = replace(selection, records=(selection.records[0],) * 2)
    queue = DownloadQueue(local_data / "managed", resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    try:
        queue.prepare(selection, DownloadBudget())
        _wait(queue)
        queue.start()
        assert len(_wait(queue)["rows"]) == 1 and len(client.calls) == 4
    finally:
        queue.close()


def test_corrupt_existing_product_is_not_overwritten(local_data):
    selection, items, client = _scenario(local_data)
    root = local_data / "managed"
    target = root / "raw/sentinel2" / selection.records[0].stac_id
    shutil.copytree(local_data / "raw/sentinel2/product-15", target)
    (target / "b04.tif").write_bytes(b"corrupt")
    queue = DownloadQueue(root, resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    try:
        queue.prepare(replace(selection, records=selection.records[:1]), DownloadBudget())
        assert _wait(queue)["rows"][0]["state"] == "failed"
        assert (target / "b04.tif").read_bytes() == b"corrupt" and not client.calls
    finally:
        queue.close()


def test_cancel_mid_asset_and_resume_keeps_partial_bytes_out_of_catalogue(local_data):
    selection, items, client = _scenario(local_data)
    root = local_data / "managed"
    queue = DownloadQueue(root, resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    one = replace(selection, records=selection.records[:1])
    try:
        queue.prepare(one, DownloadBudget())
        _wait(queue)
        client.hook = queue.cancel
        queue.start()
        assert _wait(queue)["rows"][0]["state"] == "cancelled"
        assert not list((root / "raw/sentinel2").glob("*/acquisition_manifest.json"))
        assert list((root / "downloads").rglob("*.part"))
        client.hook = None
        queue.prepare(one, DownloadBudget())
        assert _wait(queue)["phase"] == "ready"
        queue.start()
        assert _wait(queue)["rows"][0]["state"] == "completed"
        assert list((root / "downloads").rglob("quarantine/*"))
    finally:
        queue.close()


@pytest.mark.parametrize("mode", ["tls", "429"])
def test_transport_reuses_tls_errors_and_bounded_429_retries(local_data, monkeypatch, mode):
    import requests
    from botocore.exceptions import ClientError
    from src.data import ingestion
    from src.network_config import NetworkSettings
    selection, items, client = _scenario(local_data)
    original = client.download_file
    calls = []
    def unreliable(*args, **kwargs):
        calls.append(1)
        if mode == "tls":
            raise requests.exceptions.SSLError("private certificate path")
        if len(calls) == 1:
            raise ClientError({"Error": {"Code": "SlowDown"}, "ResponseMetadata": {
                "HTTPStatusCode": 429, "HTTPHeaders": {"retry-after": "0"}}}, "GetObject")
        return original(*args, **kwargs)
    client.download_file = unreliable
    monkeypatch.setattr(ingestion.NetworkSettings, "from_environment", lambda: NetworkSettings(backoff_seconds=0))
    queue = DownloadQueue(local_data / "managed", resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    try:
        queue.prepare(replace(selection, records=selection.records[:1]), DownloadBudget())
        _wait(queue)
        queue.start()
        row = _wait(queue)["rows"][0]
        if mode == "tls":
            assert row["state"] == "failed" and "tls" in row["error"]
            assert "private" not in row["error"] and len(calls) == 1
        else:
            assert row["state"] == "completed" and len(calls) == 5
    finally:
        queue.close()


def test_disk_space_budget_blocks_before_transfer(local_data, monkeypatch):
    selection, items, client = _scenario(local_data)
    queue = DownloadQueue(local_data / "managed", resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    monkeypatch.setattr("src.data.download_store.shutil.disk_usage", lambda root: shutil._ntuple_diskusage(10, 9, 1))
    try:
        queue.prepare(selection, DownloadBudget())
        assert _wait(queue)["rows"][0]["state"] == "failed" and not client.calls
    finally:
        queue.close()


def test_product_lock_prevents_competing_writers(local_data):
    store = LocalDownloadStore(local_data / "managed")
    with store.lock("PRODUCT"):
        with pytest.raises(TransferAborted, match="verrouillée"):
            with store.lock("PRODUCT"):
                pytest.fail("Competing writer entered")
    with store.lock("PRODUCT"):
        pass  # OS lock released, no stale lock-file deletion required.


@pytest.mark.parametrize("identifier", ["../outside", "C:secret", "NUL", "bad."])
def test_unsafe_paths_are_rejected(local_data, identifier):
    store = LocalDownloadStore(local_data / "managed")
    with pytest.raises(TransferAborted):
        store.stage(identifier)


def test_changed_stac_identity_and_missing_size_are_rejected(local_data):
    selection, items, client = _scenario(local_data)
    record = selection.records[0]
    item = items[record.stac_id]
    with pytest.raises(TransferAborted):
        make_plan(record, {**item, "id": "OTHER"}, selection.query)
    item["assets"]["B04_10m"].pop("file:size")
    with pytest.raises(TransferAborted, match="Taille"):
        make_plan(record, item, selection.query)


def test_corrupt_transfer_never_publishes_and_resume_reuses_valid_assets(local_data):
    selection, items, client = _scenario(local_data)
    original = client.download_file
    def corrupt_second(bucket, key, filename, **kwargs):
        original(bucket, key, filename, **kwargs)
        if key.endswith("b08.tif"):
            with open(filename, "r+b") as output:
                output.write(b"broken")
    client.download_file = corrupt_second
    root = local_data / "managed"
    queue = DownloadQueue(root, resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    one = replace(selection, records=selection.records[:1])
    try:
        queue.prepare(one, DownloadBudget())
        _wait(queue)
        queue.start()
        assert _wait(queue)["rows"][0]["state"] == "failed"
        assert not (root / "raw/sentinel2" / one.records[0].stac_id).exists()
        assert len(client.calls) == 2
        client.download_file = original
        queue.prepare(one, DownloadBudget())
        _wait(queue)
        queue.start()
        assert _wait(queue)["rows"][0]["state"] == "completed"
        assert sum(key.endswith("b04.tif") for key in client.calls) == 1
        assert sum(key.endswith("b08.tif") for key in client.calls) == 2
    finally:
        queue.close()


def test_new_queue_never_restarts_transfers_automatically(local_data):
    selection, items, client = _scenario(local_data)
    root = local_data / "managed"
    queue = DownloadQueue(root, resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    queue.prepare(selection, DownloadBudget())
    assert _wait(queue)["phase"] == "ready"
    queue.close()
    resumed = DownloadQueue(root, resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    try:
        assert not resumed.busy and resumed.snapshot()["phase"] == "idle"
        assert "Historique" in resumed.snapshot()["error"] and not client.calls
        with pytest.raises(TransferAborted):
            resumed.start()
    finally:
        resumed.close()


def test_prepare_returns_immediately_and_cannot_be_duplicated(local_data):
    selection, items, client = _scenario(local_data)
    entered, release = Event(), Event()
    def slow(record):
        entered.set()
        assert release.wait(5)
        return items[record.stac_id]
    queue = DownloadQueue(local_data / "managed", resolver=slow, client_factory=lambda: client)
    try:
        queue.prepare(selection, DownloadBudget())
        assert entered.wait(2) and queue.busy
        with pytest.raises(TransferAborted):
            queue.prepare(selection, DownloadBudget())
        queue.cancel()
        release.set()
        assert all(row["state"] == "cancelled" for row in _wait(queue)["rows"])
        assert not client.calls
    finally:
        release.set()
        queue.close()


def test_asset_names_can_differ_and_collisions_are_rejected(local_data):
    selection, items, client = _scenario(local_data)
    record = selection.records[0]
    item = items[record.stac_id]
    item["assets"]["red-different"] = item["assets"].pop("B04_10m")
    item["assets"]["red-different"].update({"eo:bands": [{"name": "B04"}], "gsd": 10})
    assert make_plan(record, item, selection.query).roles["b04"] == "red-different"
    item["assets"]["B08_10m"]["href"] = item["assets"]["red-different"]["href"]
    with pytest.raises(TransferAborted, match="Collision"):
        make_plan(record, item, selection.query)


def test_concurrency_is_bounded_and_cancel_covers_pending_acquisitions(local_data):
    from threading import Barrier, Lock
    selection, items, client = _scenario(local_data)
    barrier, lock = Barrier(2), Lock()
    seen = set()
    original = client.download_file
    def synchronized(bucket, key, filename, **kwargs):
        identifier = key.split("/")[1]
        with lock:
            first = identifier not in seen
            seen.add(identifier)
        if first:
            barrier.wait(5)  # Both acquisitions must really run concurrently.
        return original(bucket, key, filename, **kwargs)
    client.download_file = synchronized
    queue = DownloadQueue(local_data / "managed", resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    try:
        queue.prepare(selection, DownloadBudget(workers=2))
        _wait(queue)
        queue.start()
        assert all(row["state"] == "completed" for row in _wait(queue)["rows"])
        assert len(seen) == 2
    finally:
        queue.close()


def test_too_long_local_paths_are_rejected_at_estimation_without_transfer(local_data):
    selection, items, client = _scenario(local_data)
    queue = DownloadQueue(local_data / "managed", resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    queue.store.max_path = 60  # Simulates a deep OneDrive root under Windows MAX_PATH.
    try:
        queue.prepare(replace(selection, records=selection.records[:1]), DownloadBudget())
        row = _wait(queue)["rows"][0]
        assert row["state"] == "failed" and "Chemin local trop long" in row["error"]
        assert not client.calls
    finally:
        queue.close()


def test_staging_temporary_names_fit_windows_max_path_for_real_identifier(local_data):
    store = LocalDownloadStore(local_data / "managed")
    identifier = "S2A_MSIL2A_20250120T093321_N0511_R136_T32NNF_20250120T125148"
    folder = store.stage(identifier)
    assert folder.name == identifier and identifier not in folder.parent.name
    relative = folder.relative_to(store.root)
    # Former layout repeated the 60-character identifier; keep the gain explicit.
    assert len(str(relative)) < len(str(Path("downloads/staging") / identifier / identifier)) - 40


def test_local_write_failure_is_not_reported_as_network_error(local_data):
    selection, items, client = _scenario(local_data)
    def unwritable(bucket, key, filename, **kwargs):
        client.calls.append(key)
        raise FileNotFoundError(2, "path too long")
    client.download_file = unwritable
    queue = DownloadQueue(local_data / "managed", resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    try:
        queue.prepare(replace(selection, records=selection.records[:1]), DownloadBudget())
        _wait(queue)
        queue.start()
        row = _wait(queue)["rows"][0]
        assert row["state"] == "failed" and "Écriture locale" in row["error"] and "Réseau" not in row["error"]
        assert len(client.calls) == 1  # Not retried as a transient transport error.
        assert not list((local_data / "managed/raw/sentinel2").iterdir())
    finally:
        queue.close()


def test_final_queue_journal_is_not_busy(local_data):
    selection, items, client = _scenario(local_data)
    root = local_data / "managed"
    queue = DownloadQueue(root, resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    try:
        queue.prepare(replace(selection, records=selection.records[:1]), DownloadBudget())
        _wait(queue)
        assert json.loads((root / "downloads/queue.json").read_text())["busy"] is False
        queue.start()
        _wait(queue)
        journal = json.loads((root / "downloads/queue.json").read_text())
        assert journal["busy"] is False and journal["phase"] == "finished"
        assert journal["rows"][0]["state"] == "completed"
    finally:
        queue.close()


def test_xml_without_stac_checksum_requires_safe_evidence(local_data):
    selection, items, client = _scenario(local_data)
    root = local_data / "managed"
    one = replace(selection, records=selection.records[:1])
    queue = DownloadQueue(root, resolver=lambda r: items[r.stac_id], client_factory=lambda: client)
    try:
        queue.prepare(one, DownloadBudget())
        _wait(queue)
        queue.start()
        assert _wait(queue)["rows"][0]["state"] == "completed"
    finally:
        queue.close()
    record = one.records[0]
    item = json.loads(json.dumps(items[record.stac_id]))
    item["assets"]["product_metadata"].pop("file:checksum")
    safe = b"<xfdu/>"
    item["assets"]["safe_manifest"] = {"href": f"s3://eodata/Sentinel-2/{record.stac_id}/manifest.safe",
                                       "file:size": len(safe),
                                       "file:checksum": "sha256:" + hashlib.sha256(safe).hexdigest()}
    plan = make_plan(record, item, one.query)
    folder = root / "raw/sentinel2" / record.stac_id
    # The published manifest holds no SAFE evidence: its XML is no longer source-bound.
    with pytest.raises(TransferAborted, match="preuve SAFE"):
        queue.store.validate(folder, plan)
