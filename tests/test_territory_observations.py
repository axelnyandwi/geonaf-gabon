"""Sentinel-2 discovery isolated per territory (MVP lot 1).

Synthetic packs hold extracts of Akanda, Libreville and Owendo side by side, plus full
tiles. Discovery reads manifests only, so the synthetic extracts carry valid manifests
and no raster; the end-to-end test copies the real Akanda v3 pack when it is present.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from src.data.derived_subset import build_subset_manifest
from src.services import local_preview
from src.services.local_preview import (
    LocalPreviewError, _load_observation, list_local_observations, subset_territory,
)
from tests import demo_pack_oracle as oracle

TERRITORIES = ("akanda", "libreville", "owendo")
DAYS = (15, 20, 25)


def _product_id(day: int, tile: str = "T32NNF") -> str:
    return f"S2B_MSIL2A_202501{day:02d}T093239_N0511_R136_{tile}_202501{day:02d}T120528"


def _parent(product_id: str, day: int) -> dict:
    roles = {"b04": "B04_10m", "b08": "B08_10m", "scl": "SCL_20m", "calibration_xml": "product_metadata"}
    return {
        "schema_version": "geonaf.sentinel2-acquisition/v1", "product_id": product_id,
        "collection": "sentinel-2-l2a", "acquired_at_utc": f"2025-01-{day:02d}T09:32:39.024000Z",
        "ingested_at_utc": "2026-10-16T17:10:20Z",
        "stac": {"source": "https://stac.example", "item_id": product_id}, "calibration": {"method": "BOA"},
        "assets": {role: {"id": asset_id, "product_id": product_id, "path": f"{role}.jp2", "sha256": "a" * 64}
                   for role, asset_id in roles.items()},
        "integrity": {"mode": "strict", "source_assets": {role: {"status": "verified"} for role in roles}},
    }


def _aoi(data_dir: Path, aoi_id: str, offset: float = 0.0) -> str:
    """Small square AOI; returns its file digest."""
    x, y = 9.33 + offset + 0.05 * TERRITORIES.index(aoi_id), 0.46
    geometry = {"type": "Polygon", "coordinates": [[[x, y], [x + 0.1, y], [x + 0.1, y + 0.1], [x, y + 0.1], [x, y]]]}
    path = data_dir / "aoi" / f"{aoi_id}.geojson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": geometry}]}), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _extract(data_dir: Path, aoi_id: str, day: int, *, aoi: dict | None = None, folder: str | None = None) -> Path:
    """One extract folder ``<product>__<aoi>`` with a valid subset manifest for ``aoi_id``."""
    product_id = _product_id(day)
    raster = {"path": "x.tif", "sha256": "b" * 64, "pixel_equality": "verified"}
    assets = {"b04": dict(raster), "b08": dict(raster), "scl": dict(raster),
              "calibration_xml": {"path": "MTD.xml", "sha256": "a" * 64}}
    if aoi is None:
        digest = hashlib.sha256((data_dir / "aoi" / f"{aoi_id}.geojson").read_bytes()).hexdigest()
        aoi = {"id": aoi_id, "path": f"aoi/{aoi_id}.geojson", "sha256": digest,
               "nature": "carré d'acquisition 12 × 12 km ; pas une unité administrative"}
    manifest = build_subset_manifest(parent_manifest=_parent(product_id, day), parent_manifest_sha256="c" * 64,
                                     assets=assets, aoi=aoi, margin_m=200, software={},
                                     created_at=datetime(2026, 10, 26, tzinfo=timezone.utc))
    directory = data_dir / "raw" / "sentinel2" / (folder or f"{product_id}__{aoi_id}")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "subset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def _full_tile(data_dir: Path, day: int, tile: str = "T32NNF") -> Path:
    """A full-tile folder with a strict acquisition manifest (valid for every AOI it covers)."""
    product_id = _product_id(day, tile)
    manifest = _parent(product_id, day)
    manifest.update({"schema_version": "geonaf.sentinel2-acquisition/v1"})
    directory = data_dir / "raw" / "sentinel2" / product_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "acquisition_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


@pytest.fixture
def multi_pack(tmp_path):
    """Extracts of the three territories for the same three dates, side by side."""
    for aoi_id in TERRITORIES:
        _aoi(tmp_path, aoi_id)
        for day in DAYS:
            _extract(tmp_path, aoi_id, day)
    return tmp_path


@pytest.fixture
def full_tiles_accepted(monkeypatch):
    """Full-tile manifests here are minimal: the acquisition schema itself is tested elsewhere."""
    real = local_preview.validate_acquisition_manifest
    monkeypatch.setattr(local_preview, "validate_acquisition_manifest", lambda manifest: None)
    return real


def _territories_of(observations) -> set[str]:
    return {subset_territory(json.loads(item.manifest_path.read_text(encoding="utf-8")))[0]
            for item in observations if item.subset}


@pytest.mark.parametrize("aoi_id", TERRITORIES)
def test_each_territory_discovers_only_its_own_extracts(multi_pack, aoi_id):
    observations = list_local_observations(multi_pack, aoi_id)
    assert len(observations) == len(DAYS)
    assert _territories_of(observations) == {aoi_id}
    assert all(item.manifest_path.parent.name.endswith(f"__{aoi_id}") for item in observations)
    assert [item.acquired_at.day for item in observations] == list(DAYS)


def test_without_territory_extracts_of_several_territories_are_refused(multi_pack):
    with pytest.raises(LocalPreviewError):
        list_local_observations(multi_pack)


def test_a_territory_without_extracts_finds_nothing(multi_pack):
    shutil.rmtree(multi_pack / "raw" / "sentinel2" / f"{_product_id(15)}__owendo")
    shutil.rmtree(multi_pack / "raw" / "sentinel2" / f"{_product_id(20)}__owendo")
    shutil.rmtree(multi_pack / "raw" / "sentinel2" / f"{_product_id(25)}__owendo")
    assert list_local_observations(multi_pack, "owendo") == ()
    assert len(list_local_observations(multi_pack, "libreville")) == len(DAYS)


def test_missing_sentinel2_directory_gives_an_empty_catalogue(tmp_path):
    _aoi(tmp_path, "akanda")
    assert list_local_observations(tmp_path, "akanda") == ()


def test_full_tiles_stay_available_to_every_territory(tmp_path, full_tiles_accepted):
    for aoi_id in TERRITORIES:
        _aoi(tmp_path, aoi_id)
    _full_tile(tmp_path, 15)
    _full_tile(tmp_path, 25)
    _extract(tmp_path, "akanda", 20)
    for aoi_id in TERRITORIES:
        found = list_local_observations(tmp_path, aoi_id)
        days = [item.acquired_at.day for item in found]
        assert days == ([15, 20, 25] if aoi_id == "akanda" else [15, 25])
        assert [item.subset for item in found] == ([False, True, False] if aoi_id == "akanda" else [False, False])


def test_single_territory_pack_is_still_listed_without_territory(tmp_path):
    _aoi(tmp_path, "akanda")
    for day in DAYS:
        _extract(tmp_path, "akanda", day)
    assert list_local_observations(tmp_path) == list_local_observations(tmp_path, "akanda")


@pytest.mark.parametrize("aoi", [
    None, {}, {"id": None}, {"id": "Akanda", "sha256": "a" * 64}, {"id": "../owendo", "sha256": "a" * 64},
    {"id": "akanda"}, {"id": "akanda", "sha256": "xyz"}, {"id": "akanda", "sha256": "A" * 64},
])
def test_extract_with_a_missing_or_malformed_territory_is_refused(multi_pack, aoi):
    directory = _extract(multi_pack, "akanda", 15)
    manifest = json.loads((directory / "subset_manifest.json").read_text(encoding="utf-8"))
    if aoi is None:
        del manifest["derivation"]["aoi"]
    else:
        manifest["derivation"]["aoi"] = aoi
    (directory / "subset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for aoi_id in TERRITORIES:  # Fail closed for every territory: nothing is guessed.
        with pytest.raises(LocalPreviewError):
            list_local_observations(multi_pack, aoi_id)
    with pytest.raises(ValueError):
        subset_territory(manifest)


def test_extract_of_another_geometry_of_the_same_territory_is_refused(multi_pack):
    _aoi(multi_pack, "libreville", offset=0.01)  # New version of the Libreville square.
    with pytest.raises(LocalPreviewError):
        list_local_observations(multi_pack, "libreville")
    assert len(list_local_observations(multi_pack, "akanda")) == len(DAYS)  # Other territories unaffected.


def test_extract_claiming_a_territory_without_aoi_file_is_refused(multi_pack):
    (multi_pack / "aoi" / "owendo.geojson").unlink()
    with pytest.raises(LocalPreviewError):
        list_local_observations(multi_pack, "owendo")


def test_folder_with_both_manifest_kinds_is_ambiguous(multi_pack, full_tiles_accepted):
    directory = _full_tile(multi_pack, 15, tile="T32NNG")
    extract = _extract(multi_pack, "akanda", 20)
    shutil.copyfile(extract / "subset_manifest.json", directory / "subset_manifest.json")
    for aoi_id in TERRITORIES:
        with pytest.raises(LocalPreviewError):
            list_local_observations(multi_pack, aoi_id)


def test_same_product_twice_for_one_territory_is_refused(multi_pack):
    _extract(multi_pack, "owendo", 15, folder=f"{_product_id(15)}__owendo_copie")
    with pytest.raises(LocalPreviewError):
        list_local_observations(multi_pack, "owendo")
    assert len(list_local_observations(multi_pack, "akanda")) == len(DAYS)


@pytest.mark.parametrize("aoi_id", ["", "Akanda", "../aoi/akanda", "akanda/../owendo", 3])
def test_invalid_territory_identifier_is_refused(multi_pack, aoi_id):
    with pytest.raises(LocalPreviewError):
        list_local_observations(multi_pack, aoi_id)


def test_loading_an_extract_for_another_territory_is_refused(multi_pack):
    """Defence in depth: an analysis never reads an extract cut for another AOI."""
    akanda = list_local_observations(multi_pack, "akanda")[0]
    with pytest.raises(ValueError, match="autre territoire"):
        _load_observation(akanda, multi_pack / "aoi" / "owendo.geojson")
    _aoi(multi_pack, "akanda", offset=0.02)
    with pytest.raises(ValueError, match="autre géométrie"):
        _load_observation(akanda, multi_pack / "aoi" / "akanda.geojson")


@pytest.mark.parametrize("analysis", ["analyze_local_observation", "analyze_local_group",
                                      "analyze_local_group_with_scl"])
def test_analyses_refuse_an_extract_of_another_territory(multi_pack, analysis):
    from src.geo.scientific_profile import reference_profile

    libreville = list_local_observations(multi_pack, "libreville")[0]
    target = libreville if analysis == "analyze_local_observation" else (libreville,)
    with pytest.raises(LocalPreviewError):
        getattr(local_preview, analysis)(target, multi_pack, scientific_profile=reference_profile("owendo"))


def test_pair_and_series_refuse_an_extract_of_another_territory(multi_pack):
    from src.geo.scientific_profile import reference_profile

    first, _, last = list_local_observations(multi_pack, "akanda")
    with pytest.raises(LocalPreviewError):
        local_preview.analyze_local_akanda(first, last, multi_pack, scientific_profile=reference_profile("libreville"))
    with pytest.raises(LocalPreviewError):
        local_preview.analyze_local_ndvi_series((first, last), multi_pack,
                                                scientific_profile=reference_profile("owendo"), period=None)


def test_every_application_caller_names_the_territory():
    """The six callers of the application pass the territory: no unfiltered discovery remains."""
    root = Path(__file__).resolve().parents[1]
    calls = []
    for path in [*sorted((root / "src").rglob("*.py")), *sorted((root / "views").glob("*.py"))]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "list_local_observations(" in line and "def list_local_observations" not in line \
                    and "import" not in line:
                calls.append((path.relative_to(root).as_posix(), number, line.strip()))
    assert len(calls) == 6, calls
    assert all("list_local_observations(data_dir)" not in line for _, _, line in calls), calls
    assert all("aoi_id" in line for _, _, line in calls), calls


@pytest.mark.skipif(not oracle.available(), reason="Paquet de démonstration absent.")
def test_akanda_v3_pack_unchanged_and_isolated_from_new_territories(tmp_path):
    """The released v3 pack keeps its three dates; extracts added for other territories never leak in."""
    pack = tmp_path / "pack"
    shutil.copytree(oracle.PACK_DIR, pack)
    before = list_local_observations(pack)
    assert list_local_observations(pack, "akanda") == before
    assert [item.product_id for item in before] == list(oracle.PRODUCTS)
    assert all(item.subset for item in before) and _territories_of(before) == {"akanda"}
    assert list_local_observations(pack, "libreville") == ()

    # A Libreville extract next to the Akanda ones (same product, other folder and AOI).
    _aoi(pack, "libreville")
    source = pack / "raw" / "sentinel2" / oracle.PRODUCTS[0]
    copy = pack / "raw" / "sentinel2" / f"{oracle.PRODUCTS[0]}__libreville"
    shutil.copytree(source, copy)
    manifest = json.loads((copy / "subset_manifest.json").read_text(encoding="utf-8"))
    manifest["derivation"]["aoi"] = {"id": "libreville", "path": "aoi/libreville.geojson",
                                     "sha256": hashlib.sha256((pack / "aoi" / "libreville.geojson").read_bytes())
                                     .hexdigest()}
    (copy / "subset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert list_local_observations(pack, "akanda") == before
    libreville = list_local_observations(pack, "libreville")
    assert [item.manifest_path.parent.name for item in libreville] == [copy.name]
    with pytest.raises(LocalPreviewError):
        list_local_observations(pack)  # Several territories: the territory must be named.
