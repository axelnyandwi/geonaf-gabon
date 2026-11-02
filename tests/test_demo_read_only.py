"""Read-only demonstration mode (GEONAF_DEMO_READ_ONLY): no visitor download, no shared write.

Services and store primitives are called directly (as a bypass of the interface would); the
network doubles fail the test if touched. Pages run on a copy of the v3 demo pack, which
must stay byte-identical and verified.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
from pathlib import Path
import shutil
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from src.config import DATA_DIR
from src.demo_mode import ENV_NAME, DemoReadOnlyError, ensure_writable, read_only

ROOT = Path(__file__).resolve().parent.parent
PACK = Path(os.environ.get("GEONAF_DEMO_PACK_DIR") or DATA_DIR / "demo_pack" / "geonaf-demo-akanda-2025-01-v3")
BOUNDS = (9.40, 0.52, 9.52, 0.64)
PAGES = ("app.py", *(f"views/analyses_temporelles.py?section={section}" for section in ("comparer", "variation", "serie", "donnees")),
         "views/multisource.py", "views/territoires.py")


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv(ENV_NAME, "1")


def _tree(root: Path) -> dict:
    """Every file and folder below ``root`` with size and modification time (writes change it)."""
    return {path.relative_to(root).as_posix(): (path.is_dir(), path.stat().st_size if path.is_file() else 0,
                                                 path.stat().st_mtime_ns)
            for path in Path(root).rglob("*")}


def _boom():
    return Mock(side_effect=AssertionError("network or worker used"))


def _forbidden_calls(data_dir: Path, client):
    from src.data.geoboundaries import fetch_boundaries, import_geoboundaries
    from src.data.openstreetmap import fetch_osm_snapshot
    from src.data.sentinel1 import fetch_sentinel1_window
    from src.data.worldcover import fetch_worldcover_windows
    from src.services import external_retrieval as retrieval
    from src.services import territory_catalog as catalog
    from src.services.download_queue import DownloadQueue
    from src.services.external_layers import open_store

    store, repository = open_store(data_dir)   # Constructing a store writes nothing.
    return {
        "retrieve_worldcover": lambda: retrieval.retrieve_worldcover(data_dir, aoi_id="akanda", wgs84_bounds=BOUNDS,
                                                                     client=client),
        "retrieve_osm": lambda: retrieval.retrieve_osm(data_dir, wgs84_bounds=BOUNDS, force_refresh=True,
                                                       client=client),
        "retrieve_sentinel1": lambda: retrieval.retrieve_sentinel1(data_dir, aoi_id="akanda", wgs84_bounds=BOUNDS,
                                                                   month=date(2025, 1, 1), stac_io=client,
                                                                   s3_client=client),
        "load_referential": lambda: catalog.load_referential(data_dir, "gbOpen", "ADM2", client=client),
        "import_custom_zone": lambda: catalog.import_custom_zone(
            data_dir, b'{"type": "FeatureCollection", "features": []}', label="Zone", author="Auteur",
            license_spdx="CC-BY-4.0", source_date="2026-11-02"),
        "import_geoboundaries": lambda: import_geoboundaries(data_dir, release="gbOpen", iso3="GAB", level="ADM2",
                                                             client=client),
        "fetch_boundaries": lambda: fetch_boundaries(client, store, release="gbOpen", iso3="GAB", level="ADM2"),
        "fetch_osm_snapshot": lambda: fetch_osm_snapshot(client, store, repository, wgs84_bounds=BOUNDS,
                                                         force_refresh=True),
        "fetch_worldcover_windows": lambda: fetch_worldcover_windows(client, store, repository, aoi_id="akanda",
                                                                     wgs84_bounds=BOUNDS),
        "fetch_sentinel1_window": lambda: fetch_sentinel1_window(client, store, repository, None, "VV",
                                                                 aoi_id="akanda", wgs84_bounds=BOUNDS),
        "download_queue": lambda: DownloadQueue(data_dir, resolver=client, client_factory=client),
    }


def _store_writes(data_dir: Path):
    from src.data.download_store import LocalDownloadStore, atomic_json
    from src.data.layer_store import LayerFileStore
    from src.data.territory_registry import TerritoryRegistry

    store = LayerFileStore(data_dir / "external")
    return {
        "layer_write_bytes": lambda: store.write_bytes("osm/a.json", b"{}"),
        "layer_temporary": lambda: store.temporary("osm/a.json"),
        "layer_publish": lambda: store.publish(data_dir / "missing.tmp", "osm/a.json"),
        "registry_register": lambda: TerritoryRegistry(data_dir).register(
            territory_id="import-zone", label="Zone", kind="custom_import", legal_status="user_provided",
            source=None, geometry_path=data_dir / "zone.geojson"),
        "atomic_json": lambda: atomic_json(data_dir / "queue.json", {}),
        "download_store": lambda: LocalDownloadStore(data_dir),
    }


@pytest.mark.parametrize("value, expected", [
    (None, False), ("", False), ("0", False), ("false", False), ("No", False), (" off ", False),
    ("1", True), ("true", True), ("YES", True), ("on", True), ("unexpected", True),   # Fails closed.
])
def test_mode_is_off_by_default_and_fails_closed(value, expected):
    environ = {} if value is None else {ENV_NAME: value}
    assert read_only(environ) is expected
    if expected:
        with pytest.raises(DemoReadOnlyError, match="lecture seule"):
            ensure_writable("Opération", environ)
    else:
        ensure_writable("Opération", environ)


def test_services_refuse_before_any_network_access_or_write(demo, tmp_path):
    client = _boom()
    for name, call in _forbidden_calls(tmp_path, client).items():
        with pytest.raises(DemoReadOnlyError):
            call()
    assert client.mock_calls == []
    assert _tree(tmp_path) == {}


def test_store_primitives_refuse_every_persistent_write(demo, tmp_path):
    for name, call in _store_writes(tmp_path).items():
        with pytest.raises(DemoReadOnlyError):
            call()
    assert _tree(tmp_path) == {}


def test_queue_created_before_the_mode_cannot_be_driven(monkeypatch, tmp_path):
    from src.data.download_control import DownloadBudget
    from src.services.download_queue import DownloadQueue

    monkeypatch.delenv(ENV_NAME, raising=False)
    client = _boom()
    queue = DownloadQueue(tmp_path, resolver=client, client_factory=client)
    try:
        before = _tree(tmp_path)
        monkeypatch.setenv(ENV_NAME, "1")
        selection = Mock(records=(object(),))
        for call in (lambda: queue.prepare(selection, DownloadBudget()), queue.start, queue.cancel):
            with pytest.raises(DemoReadOnlyError):
                call()
        assert not queue._cancel.is_set() and queue.phase == "idle"   # Another session's queue is untouched.
        assert client.mock_calls == [] and _tree(tmp_path) == before
    finally:
        queue.close()


def test_simultaneous_sessions_are_all_refused(demo, tmp_path):
    client = _boom()
    calls = [*_forbidden_calls(tmp_path, client).values(), *_store_writes(tmp_path).values()] * 4

    def attempt(call):
        try:
            call()
        except DemoReadOnlyError:
            return True
        return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(attempt, calls))
    assert client.mock_calls == [] and _tree(tmp_path) == {}


def test_refusal_is_shown_as_a_public_message():
    from src.components.errors import is_expected

    error = DemoReadOnlyError("Récupération WorldCover")
    assert is_expected(error)
    assert str(error).startswith("Récupération WorldCover : fonctionnalité indisponible en démonstration")


def test_local_writes_are_unchanged_when_the_mode_is_off(monkeypatch, tmp_path):
    from src.data.layer_store import LayerFileStore

    monkeypatch.delenv(ENV_NAME, raising=False)
    path = LayerFileStore(tmp_path / "external").write_bytes("osm/a.json", b"{}")
    assert path.read_bytes() == b"{}"


# --- Pages on a copy of the v3 demo pack ------------------------------------------------------------

@pytest.fixture(scope="module")
def pack_copy(tmp_path_factory):
    if not (PACK / "PACK_MANIFEST.json").is_file():
        pytest.skip("Paquet de démonstration absent.")
    target = tmp_path_factory.mktemp("pack") / PACK.name
    # As installed from the archive: files only. A local pack may hold empty folders left by
    # earlier local runs (e.g. ``downloads/``); copying them would hide a folder creation.
    for source in PACK.rglob("*"):
        if source.is_file():
            destination = target / source.relative_to(PACK)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return target


def _run(page: str, pack_copy: Path, monkeypatch) -> AppTest:
    import src.config
    from src.components import data_source

    data_source._resolve.clear()
    monkeypatch.setattr(src.config, "DATA_DIR", pack_copy)
    monkeypatch.setattr("src.components.resource_panel.available_memory_bytes", lambda: None)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    page, _, section = page.partition("?section=")
    if section:  # Section of the temporal analyses page (also its URL query parameter).
        from src.navigation import SECTION_KEY, SECTIONS

        app.session_state[SECTION_KEY] = SECTIONS[section]
    if page != "app.py":  # Secondary pages are reached through the real entrypoint (page links).
        app.switch_page(page).run()
    return app


def _infos(app) -> list[str]:
    return [info.value for info in app.info]


@pytest.mark.parametrize("page", PAGES)
def test_pages_work_on_the_pack_without_writing_it(page, pack_copy, demo, monkeypatch):
    from src.data.demo_pack import verify_pack

    before = _tree(pack_copy)
    app = _run(page, pack_copy, monkeypatch)
    assert not app.exception
    assert not [e.value for e in app.error]
    assert _tree(pack_copy) == before
    verify_pack(pack_copy, "geonaf-demo-akanda-2025-01")


def test_analyses_run_in_read_only_mode(pack_copy, demo, monkeypatch):
    dashboard = _run("app.py", pack_copy, monkeypatch)
    assert not dashboard.exception and dashboard.dataframe            # Key figures computed in memory.
    variation = _run("views/analyses_temporelles.py?section=variation", pack_copy, monkeypatch)
    assert not variation.exception and len(variation.metric) == 4     # Pair analysis computed in memory.


def test_forbidden_commands_are_replaced_by_a_message(pack_copy, demo, monkeypatch):
    territories = _run("views/territoires.py", pack_copy, monkeypatch)
    infos = _infos(territories)
    assert any(text.startswith("Import de zone personnalisée : fonctionnalité indisponible") for text in infos)
    assert not [button for button in territories.button if button.label.startswith("Charger")]
    assert not [button for button in territories.button if "enregistrer la zone" in button.label]

    multisource = _run("views/multisource.py", pack_copy, monkeypatch)
    assert any("Récupération des couches externes" in text and "lecture seule" in text
               for text in _infos(multisource))
    assert not [button for button in multisource.button if button.key and button.key.startswith("ms_fetch_")]


def test_mode_off_keeps_the_commands(pack_copy, monkeypatch):
    monkeypatch.delenv(ENV_NAME, raising=False)
    multisource = _run("views/multisource.py", pack_copy, monkeypatch)
    assert {"ms_fetch_wc", "ms_fetch_osm", "ms_fetch_s1"} <= {button.key for button in multisource.button}
    territories = _run("views/territoires.py", pack_copy, monkeypatch)
    assert not any("lecture seule" in text for text in _infos(territories))


def test_session_switch_does_not_leak_the_mode_into_the_pack(pack_copy, demo, monkeypatch):
    """Two sessions in a row on the same process: still no write, pack still verified."""
    from src.data.demo_pack import verify_pack

    before = _tree(pack_copy)
    for page in ("views/multisource.py", "views/territoires.py"):
        assert not _run(page, pack_copy, monkeypatch).exception
    assert _tree(pack_copy) == before
    verify_pack(pack_copy, "geonaf-demo-akanda-2025-01")
