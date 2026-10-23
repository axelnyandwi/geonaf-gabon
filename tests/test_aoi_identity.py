"""Study-zone geometry identity, strict validation, polygonal AOIs and layer reuse; offline."""

import json
import socket

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, MultiPolygon, Polygon, box, mapping

from src.data.layer_store import FileLayerRepository, LayerFileStore
from src.data.sentinel1 import fetch_sentinel1_window, parse_mosaic_item
from src.data.worldcover import fetch_worldcover_window
from src.geo.aoi_identity import load_aoi_identity
from src.services.external_layers import StoredLayer, layers_for_aoi
from src.services.territorial_indicators import build_indicators, landcover_areas
from tests.test_external_sources_fetch import NOW, FakeRangeHttp, FakeS3, mosaic_item, radar_cog, worldcover_tile
from tests.test_territorial_indicators import X0, Y0, worldcover_layer, write_landcover

L_SHAPE = Polygon([(X0, Y0), (X0 + 1000, Y0), (X0 + 1000, Y0 + 500), (X0 + 500, Y0 + 500), (X0 + 500, Y0 + 1000),
                   (X0, Y0 + 1000)])   # 1 km square without its top-right quarter: 0.75 km².


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Appel réseau interdit dans ces tests.")
    monkeypatch.setattr(socket, "create_connection", refuse)


def write_geojson(path, geometries, crs="EPSG:4326", properties=None, indent=None):
    features = [{"type": "Feature", "properties": (properties or {}), "geometry": mapping(g)} for g in geometries]
    payload = {"type": "FeatureCollection", "features": features}
    if crs != "EPSG:4326":
        payload["crs"] = {"type": "name", "properties": {"name": f"urn:ogc:def:crs:{crs.replace(':', '::')}"}}
    path.write_text(json.dumps(payload, indent=indent), encoding="utf-8")
    return path


def utm(geometry):
    return gpd.GeoSeries([geometry], crs="EPSG:32632").to_crs("EPSG:4326").iloc[0]


def test_fingerprint_ignores_encoding_but_not_geometry(tmp_path):
    square = utm(box(X0, Y0, X0 + 1000, Y0 + 1000))
    reference = load_aoi_identity(write_geojson(tmp_path / "a.geojson", [square]))
    coords = list(square.exterior.coords)[:-1]
    rotated = Polygon(coords[2:] + coords[:2])                       # Other ring start.
    reversed_ring = Polygon(list(reversed(coords)))                  # Other orientation.
    halves = [utm(box(X0, Y0, X0 + 500, Y0 + 1000)), utm(box(X0 + 500, Y0, X0 + 1000, Y0 + 1000))]
    for name, geometries, properties, indent in (("b", [rotated], {"name": "autre"}, 2),
                                                 ("c", [reversed_ring], None, None)):
        same = load_aoi_identity(write_geojson(tmp_path / f"{name}.geojson", geometries, properties=properties,
                                               indent=indent))
        assert same.geometry_sha256 == reference.geometry_sha256 and same.file_sha256 != reference.file_sha256
    split = load_aoi_identity(write_geojson(tmp_path / "d.geojson", halves))
    # Conservative: the dissolved union keeps the two collinear mid-edge vertices, so the same area
    # described differently is a different identity (new retrieval, never a wrong reuse).
    assert split.geometry_sha256 != reference.geometry_sha256 and (split.vertices, reference.vertices) == (7, 5)
    moved = load_aoi_identity(write_geojson(tmp_path / "e.geojson", [utm(box(X0, Y0, X0 + 1001, Y0 + 1000))]))
    assert moved.geometry_sha256 != reference.geometry_sha256


@pytest.mark.parametrize("geometries, message", [
    ([Polygon([(9.30, 0.40), (9.40, 0.50), (9.40, 0.40), (9.30, 0.50)])], "invalide"),     # Bow-tie.
    ([LineString([(9.3, 0.4), (9.4, 0.5)])], "non surfacique"),
    ([Polygon([(190, 0), (191, 0), (191, 1)])], "hors de la plage"),
])
def test_invalid_zones_are_refused_without_repair(tmp_path, geometries, message):
    path = write_geojson(tmp_path / "bad.geojson", geometries)
    before = path.read_bytes()
    with pytest.raises(ValueError, match=message):
        load_aoi_identity(path)
    assert path.read_bytes() == before


def test_l_shaped_zone_counts_only_its_own_cells(tmp_path):
    aoi = write_geojson(tmp_path / "l.geojson", [utm(L_SHAPE)])
    identity = load_aoi_identity(aoi)
    assert identity.geometry_type == "Polygon" and identity.vertices == 7
    codes = np.full((100, 100), 10, dtype="uint8")
    codes[:50, 50:] = 50     # Built-up only in the missing quarter.
    areas, classified, total = landcover_areas([worldcover_layer(write_landcover(tmp_path / "wc.tif", codes))], aoi)
    assert total == pytest.approx(0.75e6, rel=2e-3) and classified == pytest.approx(total, rel=1e-6)
    # The notch is outside the zone: its cells only receive floating-point residues along the shared edge.
    assert areas.get(50, 0.0) < 1.0 and areas[10] == pytest.approx(total, rel=1e-6)


def test_multipolygon_with_an_island(tmp_path):
    mainland = box(X0, Y0, X0 + 600, Y0 + 1000)
    island = box(X0 + 800, Y0 + 400, X0 + 900, Y0 + 500)
    aoi = write_geojson(tmp_path / "m.geojson", [utm(MultiPolygon([mainland, island]))])
    identity = load_aoi_identity(aoi)
    assert (identity.geometry_type, identity.parts) == ("MultiPolygon", 2)
    codes = np.full((100, 100), 80, dtype="uint8")
    codes[:, :60] = 10
    codes[50:60, 80:90] = 95
    result = build_indicators(aoi_id="m", aoi_path=aoi, worldcover=[worldcover_layer(
        write_landcover(tmp_path / "wc.tif", codes), aoi_id="m")])
    km2 = result.aoi_area_km2
    assert km2 == pytest.approx(0.61, rel=2e-3)
    vegetation = {item.label: item.value for item in result.get("landcover.vegetation_km2").breakdown}
    assert vegetation["Végétation naturelle"] == pytest.approx(0.60 / 0.61 * km2, rel=1e-5)
    assert vegetation["Mangroves"] == pytest.approx(0.01 / 0.61 * km2, rel=1e-5)
    assert result.get("landcover.land_km2").value == pytest.approx(km2, rel=1e-6)   # Sea between parts excluded.


def _layer(tmp_path, name, bounds, aoi_id="z", geometry=None):
    """A stored WorldCover-like window covering ``bounds`` (EPSG:4326 cells of 1/12 000°)."""
    res = 1 / 12000
    width, height = int(round((bounds[2] - bounds[0]) / res)), int(round((bounds[3] - bounds[1]) / res))
    path = write_landcover(tmp_path / f"{name}.tif", np.full((height, width), 10, dtype="uint8"),
                           origin=(bounds[0], bounds[3]), crs="EPSG:4326", res=res)
    _, descriptor = worldcover_layer(path, aoi_id=aoi_id)
    if geometry:
        from dataclasses import replace
        descriptor = replace(descriptor, provenance={**descriptor.provenance, "aoi_geometry_sha256": geometry})
    return StoredLayer(descriptor, f"worldcover/{name}.tif", path)


def test_layers_for_a_modified_geometry_are_never_reused(tmp_path):
    small = load_aoi_identity(write_geojson(tmp_path / "z.geojson", [box(9.40, 0.40, 9.41, 0.41)]), "z")
    legacy = _layer(tmp_path, "legacy", (9.40, 0.40, 9.41, 0.41))
    assert layers_for_aoi([legacy], small) == (legacy,)                  # Historical window, covering.
    larger = load_aoi_identity(write_geojson(tmp_path / "z2.geojson", [box(9.40, 0.40, 9.42, 0.41)]), "z")
    assert layers_for_aoi([legacy], larger) == ()                        # No longer covers: retrieve again.
    other = _layer(tmp_path, "other", (9.39, 0.39, 9.43, 0.42), geometry="f" * 64)
    assert layers_for_aoi([other], larger) == ()                         # Recorded for another geometry.
    exact = _layer(tmp_path, "exact", (9.40, 0.40, 9.42, 0.41), geometry=larger.geometry_sha256)
    assert layers_for_aoi([legacy, other, exact], larger) == (exact,)
    neighbour = _layer(tmp_path, "neighbour", (9.39, 0.39, 9.43, 0.42), aoi_id="voisine")
    assert layers_for_aoi([neighbour], larger) == ()                     # Adjacent zone identifier.


def test_worldcover_retrieval_after_a_geometry_change_uses_a_new_key(tmp_path):
    tile, _ = worldcover_tile(tmp_path)
    store = LayerFileStore(tmp_path / "external")
    repository = FileLayerRepository(store)
    client = FakeRangeHttp(tile.read_bytes())
    old = (9.40, 0.40, 9.42, 0.42)
    path, _, _ = fetch_worldcover_window(client, store, repository, aoi_id="test", wgs84_bounds=old, now=NOW)
    new = (9.40, 0.40, 9.45, 0.42)
    with pytest.raises(ValueError, match="choisir un autre identifiant"):   # Historical behaviour kept.
        fetch_worldcover_window(client, store, repository, aoi_id="test", wgs84_bounds=new)
    digest = "a" * 64
    fresh, descriptor, report = fetch_worldcover_window(client, store, repository, aoi_id="test", wgs84_bounds=new,
                                                        now=NOW, aoi_geometry_sha256=digest)
    assert not report["reused"] and fresh != path and fresh.name.endswith(f"_test-{digest[:12]}.tif")
    assert descriptor.provenance["aoi_geometry_sha256"] == digest and path.exists()   # Nothing overwritten.
    calls = len(client.calls)
    _, again, report = fetch_worldcover_window(None, store, repository, aoi_id="test", wgs84_bounds=old,
                                               aoi_geometry_sha256="b" * 64)
    assert report["reused"] and again.provenance.get("aoi_geometry_sha256") is None   # Legacy, covering.
    assert len(client.calls) == calls


def test_sentinel1_window_is_not_reused_when_it_no_longer_covers(tmp_path, monkeypatch):
    from src.network_config import NetworkSettings

    monkeypatch.setattr(NetworkSettings, "from_environment", classmethod(lambda cls, **kw: cls(max_attempts=1)))
    tile, _ = radar_cog(tmp_path)
    mosaic = parse_mosaic_item(mosaic_item())
    store = LayerFileStore(tmp_path / "external")
    repository = FileLayerRepository(store)
    s3 = FakeS3(tile.read_bytes())
    old = (9.5435, 0.4250, 9.5615, 0.4430)
    path, _, _ = fetch_sentinel1_window(s3, store, repository, mosaic, "VV", aoi_id="t", wgs84_bounds=old, now=NOW)
    new = (9.5435, 0.4250, 9.5715, 0.4430)
    with pytest.raises(ValueError, match="ne couvrant pas"):   # Before: the old window was silently returned.
        fetch_sentinel1_window(s3, store, repository, mosaic, "VV", aoi_id="t", wgs84_bounds=new)
    fresh, descriptor, report = fetch_sentinel1_window(s3, store, repository, mosaic, "VV", aoi_id="t",
                                                       wgs84_bounds=new, now=NOW, aoi_geometry_sha256="c" * 64)
    assert not report["reused"] and fresh != path and descriptor.provenance["aoi_geometry_sha256"] == "c" * 64


def test_historical_zones_keep_their_identity_and_layers():
    from src.config import DATA_DIR
    from src.services.external_layers import open_store, stored_layers

    path = DATA_DIR / "aoi" / "akanda.geojson"
    if not path.exists() or not (DATA_DIR / "external" / "catalogue").is_dir():
        pytest.skip("Données réelles absentes.")
    identity = load_aoi_identity(path, "akanda")
    _, repository = open_store(DATA_DIR)
    worldcover = layers_for_aoi(stored_layers(repository, "esa-worldcover", identity.wgs84_bounds), identity)
    radar = [l for l in stored_layers(repository, "sentinel-1-mosaic", identity.wgs84_bounds)
             if l.descriptor.provenance.get("polarization") == "VV"]
    assert len(worldcover) == 1 and len(layers_for_aoi(radar, identity)) == 1
