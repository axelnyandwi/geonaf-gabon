"""Multisource engine, map payload and Streamlit page on synthetic verified data; offline."""

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import socket
from unittest.mock import Mock

import geopandas as gpd
import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import LineString, box

from src.data.layer_store import FileLayerRepository, LayerFileStore
from src.data.openstreetmap import OpenStreetMapAdapter, fetch_osm_snapshot
from src.data.sentinel1 import describe_window, parse_mosaic_item
from src.geo.scientific_profile import GABON_EQUATORIAL_EXPERIMENTAL as PROFILE
from src.services.local_preview import analyze_local_observation, list_local_observations, local_observation_key
from src.services.multisource_demo import (
    CLASSIFICATION, COLLABORATIVE, MEASURED, analyze_multisource,
)
from src.services.multisource_map import prepare_multisource_map
from src.services.sentinel1_layer import load_radar_layer
from tests.test_external_retrieval_core import write_cog
from tests.test_external_sources_fetch import FakeOverpass, mosaic_item
from tests.test_local_preview import local_data  # noqa: F401  (fixture)
from tests.test_multisource import _worldcover

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Appel réseau interdit dans ces tests.")
    monkeypatch.setattr(socket, "create_connection", refuse)


def aoi_bounds(data_dir):
    return tuple(float(v) for v in gpd.read_file(data_dir / "aoi/akanda.geojson").to_crs("EPSG:4326").total_bounds)


def osm_payload():
    """A building on the top-right S2 cell and a road across the bottom row, in WGS84."""
    utm = gpd.GeoSeries([box(500010, 99990, 500020, 100000), LineString([(500001, 99985), (500019, 99985)])],
                        crs="EPSG:32632").to_crs("EPSG:4326")
    building = [{"lat": lat, "lon": lon} for lon, lat in utm.iloc[0].exterior.coords]
    road = [{"lat": lat, "lon": lon} for lon, lat in utm.iloc[1].coords]
    return {"osm3s": {"timestamp_osm_base": "2026-10-20T08:00:00Z"}, "elements": [
        {"type": "way", "id": 1, "tags": {"building": "yes"}, "geometry": building},
        {"type": "way", "id": 2, "tags": {"highway": "residential", "name": "<script>x</script>"}, "geometry": road}]}


@pytest.fixture
def layers(local_data, tmp_path):
    store = LayerFileStore(local_data / "external")
    repository = FileLayerRepository(store)
    path, _ = _worldcover(tmp_path, "EPSG:32632")
    from src.data.worldcover import WorldCoverAdapter
    descriptor = WorldCoverAdapter().describe_local(path, retrieved_at=NOW, extra_provenance={"aoi_id": "akanda"})
    store.path("worldcover").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, store.path("worldcover/wc_akanda.tif"))
    repository.save(descriptor, "worldcover/wc_akanda.tif")
    bounds = aoi_bounds(local_data)
    fetch_osm_snapshot(FakeOverpass(osm_payload()), store, repository, wgs84_bounds=bounds, now=NOW)
    radar = write_cog(tmp_path / "vv.tif", np.array([[0.1, 0.2], [0.01, -32768]], np.float32),
                      transform=from_origin(500000, 100000, 20, 20), nodata=-32768, block=16)
    store.path("sentinel1").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(radar, store.path("sentinel1/s1_vv_akanda.tif"))
    radar_descriptor = describe_window(store.path("sentinel1/s1_vv_akanda.tif"), parse_mosaic_item(mosaic_item()),
                                       "VV", retrieved_at=NOW, extra_provenance={"aoi_id": "akanda"})
    repository.save(radar_descriptor, "sentinel1/s1_vv_akanda.tif")
    return local_data, store, repository


def compute(data_dir, store, repository):
    from src.data.openstreetmap import load_osm_snapshot
    from src.services.external_layers import latest_osm, stored_layers

    observation = analyze_local_observation(list_local_observations(data_dir)[-1], data_dir,
                                            scientific_profile=PROFILE)
    bounds, aoi = aoi_bounds(data_dir), data_dir / "aoi/akanda.geojson"
    wc = stored_layers(repository, "esa-worldcover", bounds)[0]
    osm = latest_osm(repository, bounds)
    radar = tuple(load_radar_layer(layer.path, layer.descriptor, aoi_path=aoi, landcover_path=wc.path,
                                   landcover_classes=dict(wc.descriptor.classes))
                  for layer in stored_layers(repository, "sentinel-1-mosaic", bounds))
    return analyze_multisource(observation, aoi_path=aoi, worldcover=(wc.path, wc.descriptor),
                               osm=load_osm_snapshot(store, osm.storage_key, bounds), radar=radar)


def test_single_date_ndvi_reuses_pair_chain_and_cache_key_tracks_files(local_data):
    observations = list_local_observations(local_data)
    observation = analyze_local_observation(observations[-1], local_data, scientific_profile=PROFILE)
    assert observation.valid.tolist() == [[False, True], [True, False]]
    assert observation.summary.median == pytest.approx(0.6) and observation.summary.count == 2
    assert observation.valid_coverage_percent == pytest.approx(50, abs=0.01)
    assert observation.calibration["red_offset"] == -1000
    key = local_observation_key(observations[-1], local_data, PROFILE)
    assert key != local_observation_key(observations[0], local_data, PROFILE)
    (local_data / "aoi/akanda.geojson").write_bytes((local_data / "aoi/akanda.geojson").read_bytes() + b" ")
    assert key != local_observation_key(observations[-1], local_data, PROFILE)


def test_sources_are_typed_dated_and_radar_never_mixed_with_ndvi(layers):
    result = compute(*layers)
    natures = {s.source: s.nature for s in result.sources}
    assert natures == {"Sentinel-2 L2A": MEASURED, "ESA WorldCover": CLASSIFICATION,
                       "OpenStreetMap": COLLABORATIVE, "Sentinel-1 VV": MEASURED}
    offsets = {s.source: (s.relation, s.offset_days) for s in result.sources}
    assert offsets["ESA WorldCover"][0] == "after_layer" and offsets["ESA WorldCover"][1] > 1000
    assert offsets["OpenStreetMap"][0] == "before_layer" and offsets["Sentinel-1 VV"] == ("within", 0.0)
    assert any("WorldCover" in w for w in result.warnings) and any("OpenStreetMap" in w for w in result.warnings)
    assert all("Sentinel-1" not in w for w in result.warnings)
    stats = {z.code: z for z in result.worldcover.zones}
    assert stats[95].median == pytest.approx(0.6) and stats[80].pixel_count == 1
    infrastructure = {i.group: i for i in result.infrastructure}
    assert infrastructure["Bâtiments"].features == 1 and infrastructure["Bâtiments"].area_ha == pytest.approx(0.01, rel=1e-3)
    assert infrastructure["Bâtiments"].ndvi.pixel_count == 1 and infrastructure["Routes et voies"].length_km > 0
    radar = result.radar[0]
    # Only the top-left 20 m cell touches the 20 m AOI; the NoData cell lies outside and is not counted.
    assert radar.invalid_counts["nodata"] == 0 and radar.median_db == pytest.approx(-10.0)
    assert int(radar.aoi_pixels.sum()) == 1
    # Radar statistics are per land-cover class only: no NDVI field in radar outputs.
    assert all(not hasattr(zone, "ndvi") for zone in radar.per_class)
    assert any("ne valide" in sentence for sentence in result.interpretations) or result.interpretations == ()
    assert "aucune relation causale" in result.scope


def test_map_payload_is_public_filtered_and_escapes_nothing_into_html(layers):
    data_dir, store, repository = layers
    result = compute(*layers)
    aoi = data_dir / "aoi/akanda.geojson"
    payload = prepare_multisource_map(result, aoi_path=aoi, worldcover_classes={95}, radar_polarization="none")
    encoded = json.dumps(payload, ensure_ascii=False)
    assert payload["schema_version"] == "geonaf.multisource-map/v1" and len(payload["coordinates"]) == 4
    assert str(data_dir) not in encoded and "storage_key" not in encoded and "\\\\" not in encoded
    ids = [r["id"] for r in payload["rasters"]]
    assert ids == ["ndvi", "worldcover", "osm-buildings"]
    worldcover = payload["rasters"][1]
    assert [item["label"] for item in worldcover["legend"]["items"]] == ["Mangroves"]
    assert "© OpenStreetMap contributors" in payload["attributions"]
    assert any("WorldCover" in a for a in payload["attributions"])
    assert [f["properties"]["group"] for f in payload["vectors"]["features"]] == ["Routes et voies"]
    assert "name" not in payload["vectors"]["features"][0]["properties"]  # Free-text tags are not forwarded.
    with_radar = prepare_multisource_map(result, aoi_path=aoi)
    assert "s1-vv" in [r["id"] for r in with_radar["rasters"]]
    with pytest.raises(ValueError):
        prepare_multisource_map(result, aoi_path=aoi, max_size=10_000)
    js = (Path(__file__).resolve().parents[1] / "src/components/maplibre/multisource.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js  # Contributed OSM text is only inserted with textContent.


def _page(data_dir, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import src.config
    import src.components.multisource as ui
    from src.services import external_retrieval

    monkeypatch.setattr(src.config, "DATA_DIR", data_dir)
    for name in ("cached_observation", "cached_osm", "cached_radar", "cached_multisource", "cached_map_payload"):
        getattr(ui, name).clear()
    retrieval = {name: Mock(return_value={"reused": True, "network_bytes": 0, "storage_key": "x/y"})
                 for name in ("retrieve_worldcover", "retrieve_osm", "retrieve_sentinel1")}
    for name, mock in retrieval.items():
        monkeypatch.setattr(external_retrieval, name, mock)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
    app.run(timeout=30)
    assert any(link.label == "Démonstrateur multisource" for link in app.get("page_link"))
    app.switch_page("pages/3_Multisource.py").run(timeout=60)
    return app, retrieval


def test_page_renders_offline_and_fetches_only_on_click(layers, monkeypatch):
    data_dir = layers[0]
    app, retrieval = _page(data_dir, monkeypatch)
    assert not app.exception and not app.error
    assert app.title[0].value == "Démonstrateur multisource"
    assert all(mock.call_count == 0 for mock in retrieval.values())
    warnings = [w.value for w in app.warning]
    assert "Aucun changement environnemental" in warnings[0]
    assert any("WorldCover" in w and "jours" in w for w in warnings)
    assert len(app.metric) == 5 and app.metric[2].value == "1"
    assert len(app.dataframe) >= 4
    texts = [e.value for kind in (app.caption, app.warning, app.info, app.markdown, app.success) for e in kind]
    assert all(str(data_dir) not in text for text in texts)
    app.button(key="ms_fetch_osm").click().run(timeout=60)
    assert retrieval["retrieve_osm"].call_count == 1 and retrieval["retrieve_worldcover"].call_count == 0
    assert retrieval["retrieve_osm"].call_args.kwargs["force_refresh"] is False
    assert not app.exception and any("OpenStreetMap" in s.value for s in app.success)
    assert all("x/y" not in str(j.value) for j in app.json)  # Storage keys are not displayed.


def test_page_reports_missing_layers_without_failing(local_data, monkeypatch):
    app, _ = _page(local_data, monkeypatch)
    assert not app.exception and not app.error
    assert any("Sources absentes" in info.value for info in app.info)
