"""Estuaire v4 demonstration pack (MVP lot 2): builder rules, loader limits and the built pack.

Builder and loader tests run everywhere (synthetic inputs). Tests on the built pack need
``GEONAF_DEMO_PACK_V4_DIR`` (unpacked pack, never modified: pages run on a copy); the
full-tile comparison also needs the verified full tiles in ``data/raw/sentinel2``.

Tolerance policy (same as v3, ``tests/test_demo_pack_science.py``): arrays, masks, classes,
grids, counts and NDVI statistics exact; equal-area surfaces and coverage within 1e-12
relative across platforms; exact on the reference platform (Windows).
"""

from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import stat
import zipfile

import pytest

from scripts import build_demo_pack as builder
from src.config import DATA_DIR
from src.data import demo_pack
from src.data.demo_pack import DemoPackError, pack_summary, safe_extract

ROOT = Path(__file__).resolve().parents[1]
PACK = Path(os.environ.get("GEONAF_DEMO_PACK_V4_DIR") or DATA_DIR / "demo_pack" / "geonaf-demo-estuaire-2025-01-v4")
REFERENCE = ROOT / "docs" / "benchmarks" / "pack_v4_science_windows_py310.json"
AREA_RTOL = 1e-12
TERRITORIES = ("akanda", "libreville", "owendo")
LABELS = {"akanda": "Akanda", "libreville": "Libreville", "owendo": "Owendo"}
DATES = {"akanda": ["2021-01-31", "2025-01-15", "2025-01-20", "2025-01-25"],
         "libreville": ["2021-01-31", "2025-01-15", "2025-01-25"],
         "owendo": ["2021-01-31", "2025-01-15", "2025-01-25"]}
needs_pack = pytest.mark.skipif(not (PACK / "PACK_MANIFEST.json").is_file(), reason="Paquet Estuaire v4 absent.")


# --- Builder rules (no data) --------------------------------------------------------------------------------

def test_three_territories_with_their_own_products_and_layers():
    assert tuple(builder.TERRITORIES) == TERRITORIES
    for aoi_id, territory in builder.TERRITORIES.items():
        days = [f"{p[11:15]}-{p[15:17]}-{p[17:19]}" for p in territory.products]
        assert days == DATES[aoi_id]
    descriptors = [d for territory in builder.TERRITORIES.values() for d in territory.descriptors]
    assert len(descriptors) == len(set(descriptors)) == 12        # No layer shared between squares.
    builder.check_territories(builder.TERRITORIES)


@pytest.mark.parametrize("change", [
    {"": builder.TERRITORIES["akanda"]}, {"Akanda": builder.TERRITORIES["akanda"]},
    {"../owendo": builder.TERRITORIES["owendo"]}, {"a/b": builder.TERRITORIES["owendo"]},
    {"akanda": builder.Territory("Akanda", (builder.P2025_01_15, builder.P2025_01_15),
                                 builder.TERRITORIES["akanda"].descriptors)},
    {"akanda": builder.Territory("Akanda", (), builder.TERRITORIES["akanda"].descriptors)},
    {"akanda": builder.Territory("Akanda", (builder.P2025_01_15,), builder.TERRITORIES["akanda"].descriptors[:3])},
    {},
])
def test_invalid_territory_definitions_are_refused_before_any_write(change):
    with pytest.raises(ValueError):
        builder.check_territories(change)


@pytest.mark.parametrize("value", ["2026-11-10T12:00:00", "2026-11-10T12:00:00+01:00", "demain"])
def test_creation_time_must_be_explicit_utc(value):
    with pytest.raises(ValueError):
        builder._created_at(value)
    assert builder._created_at("2026-11-10T12:00:00Z") == datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def test_existing_output_is_never_overwritten(tmp_path):
    (tmp_path / "pack").mkdir()
    with pytest.raises(FileExistsError):
        builder.build_pack(tmp_path, tmp_path / "pack", created_at=datetime.now(timezone.utc),
                           code={"commit": None, "uncommitted_paths": None})
    assert list((tmp_path / "pack").iterdir()) == []


def _layer(data_dir: Path, *, source_id="openstreetmap", bounds=(9.30, 0.40, 9.50, 0.60), checksum=True,
           resolution_m=None) -> str:
    content = b'{"elements": []}'
    key = "openstreetmap/x/snapshot.json"
    (data_dir / "external" / key).parent.mkdir(parents=True, exist_ok=True)
    (data_dir / "external" / key).write_bytes(content)
    descriptor = {"source_id": source_id, "product": "test", "version": "1", "resolution_m": resolution_m,
                  "temporal": {"kind": "snapshot"}, "license": {"spdx_id": "ODbL-1.0"},
                  "extent": {"wgs84_bounds": list(bounds)},
                  "quality": {"checksum_sha256": hashlib.sha256(content).hexdigest() if checksum else None,
                              "notes": []}}
    fingerprint = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    descriptor["fingerprint"] = fingerprint
    (data_dir / "external" / "catalogue").mkdir(parents=True, exist_ok=True)
    (data_dir / "external" / "catalogue" / f"{fingerprint}.json").write_text(
        json.dumps({"descriptor": descriptor, "storage_key": key}), encoding="utf-8")
    return fingerprint


def _square(data_dir: Path, bounds=(9.35, 0.45, 9.45, 0.55)) -> Path:
    west, south, east, north = bounds
    path = data_dir / "aoi" / "akanda.geojson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                    "geometry": {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north],
                                                                     [west, north], [west, south]]]}}]}),
                    encoding="utf-8")
    return path


@pytest.mark.parametrize("layer, message", [
    (dict(bounds=(9.40, 0.40, 9.60, 0.60)), "ne couvre pas"),                  # Layer of another square.
    (dict(bounds=(9.3505, 0.45, 9.45, 0.55), resolution_m=10.0), "ne couvre pas"),  # Short by > 1 pixel.
    (dict(source_id="esa-worldcover"), "Source inattendue"),
    (dict(checksum=False), "sans empreinte"),
])
def test_external_layer_of_another_square_or_without_digest_is_refused(tmp_path, monkeypatch, layer, message):
    monkeypatch.setattr(builder, "EXPECTED_LAYERS", ("openstreetmap",))  # One layer expected in these cases.
    aoi = _square(tmp_path)
    fingerprint = _layer(tmp_path, **layer)
    with pytest.raises(ValueError, match=message):
        builder.build_external(tmp_path, tmp_path / "out", "akanda", (fingerprint,), aoi)


def test_raster_window_one_pixel_inside_the_aoi_edge_is_accepted(tmp_path, monkeypatch):
    # A raster layer (WorldCover): OSM snapshots are no longer byte copies but minimised (tests below).
    monkeypatch.setattr(builder, "EXPECTED_LAYERS", ("esa-worldcover",))
    aoi = _square(tmp_path)
    fingerprint = _layer(tmp_path, source_id="esa-worldcover", bounds=(9.35006, 0.45, 9.45, 0.55),
                         resolution_m=10.0)  # 6.7 m inside.
    layers = builder.build_external(tmp_path, tmp_path / "out", "akanda", (fingerprint,), aoi)
    assert layers[0]["aoi_id"] == "akanda" and (tmp_path / "out" / "external" / layers[0]["storage_key"]).is_file()


# --- OpenStreetMap minimisation of the public pack -----------------------------------------------------------

OSM_BOUNDS = (9.35, 0.45, 9.45, 0.55)


def _ring(west, south, east, north):
    return [{"lat": south, "lon": west}, {"lat": south, "lon": east}, {"lat": north, "lon": east},
            {"lat": north, "lon": west}, {"lat": south, "lon": west}]


def _osm_source(data_dir: Path) -> tuple[str, str, bytes]:
    """A stored snapshot as fetch_osm_snapshot writes it, with contact tags; returns (fingerprint, key, raw)."""
    from src.data.layer_store import FileLayerRepository, LayerFileStore
    from src.data.openstreetmap import load_osm_snapshot

    elements = [
        {"type": "way", "id": 1, "geometry": _ring(9.36, 0.46, 9.37, 0.47), "nodes": [1, 2, 3, 4, 1],
         "tags": {"building": "yes", "name": "Boutique Exemple", "email": "personne@gmail.com",
                  "contact:phone": "+241 00 00 00", "addr:street": "Rue A", "addr:housenumber": "12",
                  "website": "https://example.org", "opening_hours": "Mo-Fr 08:00-18:00"}},
        {"type": "way", "id": 2, "geometry": _ring(9.38, 0.48, 9.39, 0.49), "nodes": [5, 6, 7, 8, 5],
         "tags": {"highway": "pedestrian", "area": "yes", "name": "Place B", "contact:email": "x@yahoo.fr"}},
        {"type": "way", "id": 3, "geometry": [{"lat": 0.50, "lon": 9.40}, {"lat": 0.51, "lon": 9.41}],
         "nodes": [9, 10], "tags": {"highway": "residential", "name": "Rue C", "maxspeed": "50"}},
        {"type": "relation", "id": 4, "members": [{"type": "way", "ref": 11, "role": "outer",
                                                   "geometry": _ring(9.42, 0.50, 9.44, 0.52)}],
         "tags": {"type": "multipolygon", "natural": "water", "name": "Lac D", "phone": "+241 11"}},
        {"type": "node", "id": 5, "lat": 0.47, "lon": 9.37, "tags": {"amenity": "cafe", "email": "y@hotmail.com"}},
    ]
    wrapper = {"geonaf": {"query": "q", "query_sha256": "a" * 64, "endpoint": "https://overpass-api.de/api/interpreter",
                          "retrieved_at_utc": "2026-10-22T16:58:23+00:00", "response_bytes": 1, "response_sha256": "b" * 64},
               "response": {"version": 0.6, "generator": "test", "osm3s": {"timestamp_osm_base": "2026-10-22T16:57:05Z",
                                                                            "copyright": "OpenStreetMap contributors (ODbL)"},
                            "elements": elements}}
    raw = json.dumps(wrapper, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    store = LayerFileStore(data_dir / "external")
    key = "openstreetmap/" + "a" * 24 + "/20261022T165823Z.json"
    store.write_bytes(key, raw)
    _, descriptor = load_osm_snapshot(store, key, OSM_BOUNDS)
    FileLayerRepository(store).save(descriptor, key)
    return descriptor.fingerprint, key, raw


def test_public_osm_snapshot_keeps_only_the_tags_geonaf_reads(tmp_path, monkeypatch):
    from src.data.layer_store import LayerFileStore
    from src.data.openstreetmap import load_osm_snapshot

    monkeypatch.setattr(builder, "EXPECTED_LAYERS", ("openstreetmap",))
    aoi = _square(tmp_path, OSM_BOUNDS)
    fingerprint, key, raw = _osm_source(tmp_path)
    layers = builder.build_external(tmp_path, tmp_path / "out", "akanda", (fingerprint,), aoi)

    assert (tmp_path / "external" / key).read_bytes() == raw                 # Source never modified.
    public = (tmp_path / "out" / "external" / key).read_bytes()
    text = public.decode("utf-8")
    for removed in ("gmail", "yahoo", "hotmail", "+241", "Boutique", "Rue A", "example.org", "Mo-Fr", "maxspeed",
                    "addr:", "contact:", '"name"', '"email"', '"phone"', "opening_hours", "amenity"):
        assert removed not in text, removed
    elements = json.loads(text)["response"]["elements"]
    assert [e["tags"] for e in elements] == [{"building": "yes"}, {"highway": "pedestrian", "area": "yes"},
                                             {"highway": "residential"}, {"type": "multipolygon", "natural": "water"},
                                             {}]
    assert [(e["type"], e["id"]) for e in elements] == [("way", 1), ("way", 2), ("way", 3), ("relation", 4), ("node", 5)]
    assert all(e.get("geometry") == s.get("geometry") and e.get("members") == s.get("members")
               for e, s in zip(elements, json.loads(raw)["response"]["elements"]))
    meta = json.loads(text)["geonaf"]
    assert meta["public_minimisation"]["policy"] == builder.OSM_TAG_POLICY
    assert meta["public_minimisation"]["source_file_sha256"] == hashlib.sha256(raw).hexdigest()
    assert meta["public_minimisation"]["removed_tag_values"] == 15        # 7 + 2 + 2 + 2 + 2 tags dropped.
    assert meta["response_sha256"] == "b" * 64 and "OpenStreetMap" in json.loads(text)["response"]["osm3s"]["copyright"]

    # The recorded descriptor is the one the application rebuilds from the distributed file.
    source_frame, source_descriptor = load_osm_snapshot(LayerFileStore(tmp_path / "external"), key, OSM_BOUNDS)
    public_frame, public_descriptor = load_osm_snapshot(LayerFileStore(tmp_path / "out" / "external"), key, OSM_BOUNDS)
    assert layers[0]["descriptor_fingerprint"] == public_descriptor.fingerprint != fingerprint
    assert layers[0]["file_sha256"] == hashlib.sha256(public).hexdigest() == public_descriptor.quality.checksum_sha256
    assert (tmp_path / "out" / "external" / "catalogue" / f"{public_descriptor.fingerprint}.json").is_file()
    assert public_descriptor.provenance["public_tag_policy"] == builder.OSM_TAG_POLICY
    assert public_descriptor.license == source_descriptor.license                # ODbL kept.
    assert source_frame.drop(columns="geometry").equals(public_frame.drop(columns="geometry"))
    assert list(source_frame.geometry.to_wkb()) == list(public_frame.geometry.to_wkb())
    assert layers[0]["public_minimisation"]["source_descriptor_fingerprint"] == fingerprint


def test_a_policy_that_loses_a_feature_refuses_the_build(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "EXPECTED_LAYERS", ("openstreetmap",))
    monkeypatch.setattr(builder, "OSM_PUBLIC_TAGS", frozenset({"highway", "area", "type", "natural"}))  # No building.
    aoi = _square(tmp_path, OSM_BOUNDS)
    fingerprint, _, _ = _osm_source(tmp_path)
    with pytest.raises(ValueError, match="Minimisation OSM refusée"):
        builder.build_external(tmp_path, tmp_path / "out", "akanda", (fingerprint,), aoi)


def test_the_public_tag_policy_covers_every_tag_the_parser_reads():
    import inspect

    from src.data import openstreetmap

    source = inspect.getsource(openstreetmap.OpenStreetMapAdapter)
    read = set(re.findall(r'tags\.get\("([a-z:]+)"\)', source)) | set(re.findall(r'"([a-z]+)" in tags', source))
    assert read | set(openstreetmap.DEFAULT_TAGS) <= builder.OSM_PUBLIC_TAGS, read


# --- Loader limits -----------------------------------------------------------------------------------------

def _many(count: int) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for index in range(count):
            info = zipfile.ZipInfo(f"f/{index:04d}.txt")
            info.external_attr = (stat.S_IFREG | 0o644) << 16   # Regular files, as create_archive writes them.
            bundle.writestr(info, f"{index}")
    return buffer.getvalue()


def test_member_limit_is_the_measured_need_with_a_small_margin():
    assert demo_pack.MAX_MEMBERS == 320
    assert (demo_pack.MAX_ARCHIVE_BYTES, demo_pack.MAX_EXTRACTED_BYTES, demo_pack.MAX_COMPRESSION_RATIO) == (
        64 * 1024**2, 160 * 1024**2, 50)  # Unchanged: v4 fits them.


def test_member_limit_boundary(tmp_path):
    archive = tmp_path / "ok.zip"
    archive.write_bytes(_many(demo_pack.MAX_MEMBERS))
    safe_extract(archive, tmp_path / "ok")
    assert len(list((tmp_path / "ok" / "f").iterdir())) == demo_pack.MAX_MEMBERS
    archive = tmp_path / "too_many.zip"
    archive.write_bytes(_many(demo_pack.MAX_MEMBERS + 1))
    with pytest.raises(DemoPackError, match="trop de fichiers"):
        safe_extract(archive, tmp_path / "refused")
    assert not (tmp_path / "refused").exists()  # Refused before writing anything.


def test_summary_names_the_territory_of_each_acquisition_and_layer():
    manifest = {"pack_id": "geonaf-demo-estuaire-2025-01", "pack_version": "4",
                "aoi": {"statement": "Zones…", "territories": [{"id": "owendo", "label": "Owendo"}]},
                "sentinel2": {"tile": "T32NNF", "attribution": "x", "acquisitions": [
                    {"aoi_id": "owendo", "acquired_at_utc": "2025-01-15T09:32:39Z", "ingested_at_utc": "2026-10-16",
                     "quality": {"valid_coverage_percent": 0.12, "trend_use": "excluded_low_coverage"}}]},
                "external_layers": [{"aoi_id": "owendo", "product": "ESA WorldCover", "license": {"spdx_id": "CC-BY-4.0"}}]}
    lines = pack_summary(manifest)
    assert lines[1].startswith("Owendo - Sentinel-2 acquis le 15/01/2025") and "exclue des tendances" in lines[1]
    assert any(line.startswith("Owendo - ESA WorldCover") for line in lines)


# --- The built pack ---------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def manifest():
    from src.data.demo_pack import verify_pack

    return verify_pack(PACK, "geonaf-demo-estuaire-2025-01")


@needs_pack
def test_pack_identity_and_three_acquisition_squares(manifest):
    assert manifest["pack_version"] == "4" and manifest["aoi"]["ids"] == list(TERRITORIES)
    for item in manifest["aoi"]["territories"]:
        path = PACK / item["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"] and item["crs"] == "EPSG:4326"
    assert "ni des communes" in manifest["aoi"]["statement"]
    assert len(manifest["files"]) + 1 <= demo_pack.MAX_MEMBERS
    assert manifest["total_bytes"] <= demo_pack.MAX_EXTRACTED_BYTES


@needs_pack
@pytest.mark.parametrize("aoi_id", TERRITORIES)
def test_each_territory_discovers_only_its_own_subsets(manifest, aoi_id):
    from src.services.local_preview import list_local_observations, subset_territory

    found = list_local_observations(PACK, aoi_id)
    assert [item.acquired_at.date().isoformat() for item in found] == DATES[aoi_id]
    aoi_sha = hashlib.sha256((PACK / "aoi" / f"{aoi_id}.geojson").read_bytes()).hexdigest()
    for item in found:
        assert item.subset and item.manifest_path.parent.name.endswith(f"__{aoi_id}")
        declared = subset_territory(json.loads(item.manifest_path.read_text(encoding="utf-8")))
        assert declared == (aoi_id, aoi_sha)


@needs_pack
def test_discovery_without_territory_is_refused_on_a_multi_territory_pack():
    from src.services.local_preview import LocalPreviewError, list_local_observations

    with pytest.raises(LocalPreviewError):
        list_local_observations(PACK)


@needs_pack
def test_coverage_flags_and_no_usable_2025_pair_for_owendo(manifest):
    records = {(a["aoi_id"], a["acquired_at_utc"][:10]): a for a in manifest["sentinel2"]["acquisitions"]}
    assert len(records) == sum(len(dates) for dates in DATES.values())
    excluded = {key for key, a in records.items() if a["quality"]["trend_use"] == "excluded_low_coverage"}
    assert excluded == {("akanda", "2025-01-20"), ("owendo", "2025-01-15")}
    assert records[("owendo", "2025-01-15")]["quality"]["valid_coverage_percent"] < 1.0
    assert {a["processing_baseline"] for a in records.values() if a["acquired_at_utc"].startswith("2021")} == {"N0500"}
    assert any("Owendo : aucune paire 2025" in line for line in manifest["limits"])


@needs_pack
@pytest.mark.parametrize("aoi_id", TERRITORIES)
def test_pairs_match_the_windows_reference(aoi_id):
    from scripts.diagnose_pack_science import ESTUAIRE_PAIRS, fingerprint

    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))["territories"][aoi_id]
    assert set(reference) == {f"{a}_{b}" for a, b in ESTUAIRE_PAIRS[aoi_id]}
    for key, expected in reference.items():
        current = fingerprint(PACK, aoi_id, tuple(key.split("_")))
        for name in ("arrays", "grid", "statistics", "class_pixel_counts", "denominator_area_m2"):
            assert current[name] == expected[name], (key, name)
        surfaces = [(k, expected["areas_m2"][k], current["areas_m2"][k]) for k in expected["areas_m2"]]
        surfaces += [(k, expected[k], current[k]) for k in ("comparable_area_m2", "comparable_coverage_percent")]
        for name, value_expected, value in surfaces:
            assert float(value) == pytest.approx(float(value_expected), rel=AREA_RTOL, abs=0), (key, name)


@needs_pack
def test_pair_coverage_supports_only_the_2021_2025_comparison_for_owendo():
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))["territories"]
    minimum = 5.0  # Reference profile minimum comparable coverage (unchanged).
    for aoi_id, pairs in reference.items():
        assert float(pairs["2021-01-31_2025-01-25"]["comparable_coverage_percent"]) >= minimum, aoi_id
    assert "2025-01-15_2025-01-25" not in reference["owendo"]
    from src.geo.scientific_profile import reference_profile
    from src.services.local_preview import analyze_local_akanda, list_local_observations

    found = {o.acquired_at.date().isoformat(): o for o in list_local_observations(PACK, "owendo")}
    result = analyze_local_akanda(found["2025-01-15"], found["2025-01-25"], PACK,
                                  scientific_profile=reference_profile("owendo"))
    assert result.analysis.comparable_coverage_percent < reference_profile("owendo").min_comparable_coverage_percent


@needs_pack
def test_akanda_2025_pair_is_identical_to_the_v3_reference():
    v3 = json.loads((ROOT / "docs" / "benchmarks" / "pack_science_windows_py310.json").read_text(encoding="utf-8"))
    v4 = json.loads(REFERENCE.read_text(encoding="utf-8"))["territories"]["akanda"]["2025-01-15_2025-01-25"]
    for name in ("arrays", "grid", "statistics", "class_pixel_counts", "areas_m2", "comparable_area_m2",
                 "denominator_area_m2", "comparable_coverage_percent"):
        assert v4[name] == v3[name], name


@needs_pack
@pytest.mark.parametrize("aoi_id", TERRITORIES)
def test_indicators_use_the_layers_of_their_own_square(aoi_id):
    from src.indicators.assembly import compute_territorial_indicators
    from src.indicators.inventory import indicator_inputs

    inputs = indicator_inputs(PACK, aoi_id)
    assert inputs.worldcover and {layer.descriptor.provenance.get("aoi_id") for layer in inputs.worldcover} == {aoi_id}
    assert inputs.osm is not None
    values = {item.indicator_id: item.value for item in compute_territorial_indicators(inputs).indicators}
    for key in ("aoi.area_km2", "landcover.land_km2", "landcover.vegetation_km2", "osm.roads_km",
                "osm.buildings_count"):
        assert values[key] is not None and values[key] > 0, key
    assert values["aoi.area_km2"] == pytest.approx(144.1, abs=0.05)  # 12 × 12 km square, equal-area.


@needs_pack
def test_akanda_key_figures_unchanged_from_v3():
    from src.indicators.assembly import compute_territorial_indicators
    from src.indicators.inventory import indicator_inputs

    values = {i.indicator_id: i.value for i in compute_territorial_indicators(indicator_inputs(PACK, "akanda")).indicators}
    assert round(values["landcover.land_km2"], 2) == 107.44 and round(values["landcover.vegetation_km2"], 2) == 82.58
    assert round(values["landcover.builtup_km2"], 2) == 23.92 and int(values["osm.buildings_count"]) == 36006


full_tiles = pytest.mark.skipif(not all((DATA_DIR / "raw" / "sentinel2" / p / "acquisition_manifest.json").is_file()
                                        for p in builder.TERRITORIES["akanda"].products),
                                reason="Tuiles complètes absentes.")


@needs_pack
@full_tiles
@pytest.mark.parametrize("aoi_id", TERRITORIES)
def test_subsets_give_exactly_the_full_tile_results(aoi_id):
    from src.geo.scientific_profile import reference_profile
    from src.services.local_preview import analyze_local_observation, list_local_observations
    from tests.demo_pack_oracle import compare

    profile = reference_profile(aoi_id)
    full = {o.acquired_at.date().isoformat(): o for o in list_local_observations(DATA_DIR, aoi_id)}
    for item in list_local_observations(PACK, aoi_id):
        day = item.acquired_at.date().isoformat()
        subset = analyze_local_observation(item, PACK, scientific_profile=profile)
        reference = analyze_local_observation(full[day], DATA_DIR, scientific_profile=profile)
        assert compare(reference, subset) == [], (aoi_id, day)


# --- Pages on a copy of the pack, read-only mode -----------------------------------------------------------

PAGES = ("app.py", *(f"views/analyses_temporelles.py?section={s}" for s in ("comparer", "variation", "serie")),
         "views/analyses_multisources.py", "views/territoires.py")


@pytest.fixture(scope="module")
def pack_copy(tmp_path_factory):
    if not (PACK / "PACK_MANIFEST.json").is_file():
        pytest.skip("Paquet Estuaire v4 absent.")
    target = tmp_path_factory.mktemp("v4") / PACK.name
    for source in PACK.rglob("*"):
        if source.is_file() and source.name != demo_pack.INSTALL_MARKER:
            destination = target / source.relative_to(PACK)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return target


def _tree(root: Path) -> dict:
    return {path.relative_to(root).as_posix(): (path.is_dir(), path.stat().st_size if path.is_file() else 0,
                                                 path.stat().st_mtime_ns) for path in Path(root).rglob("*")}


@pytest.mark.parametrize("aoi_id", TERRITORIES)
def test_pages_offer_three_territories_and_never_write(aoi_id, pack_copy, monkeypatch):
    import src.config
    from streamlit.testing.v1 import AppTest
    from src.components import data_source
    from src.components.analysis_settings import SHARED_TERRITORY_KEY
    from src.demo_mode import ENV_NAME
    from src.navigation import SECTION_KEY, SECTIONS

    monkeypatch.setenv(ENV_NAME, "1")
    monkeypatch.setattr(src.config, "DATA_DIR", pack_copy)
    monkeypatch.setattr("src.components.resource_panel.available_memory_bytes", lambda: None)
    before = _tree(pack_copy)
    for page in PAGES:
        data_source._resolve.clear()
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        selector = app.selectbox(key=SHARED_TERRITORY_KEY)
        assert list(selector.options) == [LABELS[t] for t in TERRITORIES]
        selector.select(aoi_id).run()
        path, _, section = page.partition("?section=")
        if section:
            app.session_state[SECTION_KEY] = SECTIONS[section]
        if path != "app.py":
            app.switch_page(path).run()
        assert not app.exception, (aoi_id, page, [e.value for e in app.exception])
        assert not [e.value for e in app.error], (aoi_id, page, [e.value for e in app.error])
        assert app.session_state[SHARED_TERRITORY_KEY] == aoi_id
    assert _tree(pack_copy) == before
    demo_pack.verify_pack(pack_copy, "geonaf-demo-estuaire-2025-01")
