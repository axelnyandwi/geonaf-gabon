"""Offline adapter and Streamlit checks; synthetic data, not scientific validation."""

from datetime import datetime, timezone
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from src.data.acquisition_manifest import build_acquisition_manifest, write_acquisition_manifest
from src.services.local_preview import (
    LocalPreviewError, analyze_local_akanda, list_local_observations,
)


@pytest.fixture
def local_data(tmp_path):
    for day in (15, 25):
        folder = tmp_path / "raw" / "sentinel2" / f"product-{day}"
        folder.mkdir(parents=True)
        product_id = f"S2B_TEST_{day}"
        paths = {}
        for role, values in (
            ("b04", np.full((2, 2), 2000, dtype="uint16")),
            ("b08", np.full((2, 2), 5000, dtype="uint16")),
            ("scl", np.array([[2, 4], [5, 7]], dtype="uint8")),
        ):
            paths[role] = folder / f"{role}.tif"
            with rasterio.open(paths[role], "w", driver="GTiff", width=2, height=2,
                               count=1, dtype=values.dtype, crs="EPSG:32632",
                               transform=from_origin(500000, 100000, 10, 10), nodata=0) as dst:
                dst.write(values, 1)
        paths["calibration_xml"] = folder / "metadata.xml"
        paths["calibration_xml"].write_text(f'''<Product><PRODUCT_URI>{product_id}.SAFE</PRODUCT_URI>
        <BOA_QUANTIFICATION_VALUE>10000</BOA_QUANTIFICATION_VALUE>
        <BOA_ADD_OFFSET band_id="3">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="7">-1000</BOA_ADD_OFFSET></Product>''', encoding="utf-8")
        ids = {role: role for role in paths}
        manifest = build_acquisition_manifest(
            product={"id": product_id, "collection": "sentinel-2-l2a",
                     "properties": {"datetime": f"2025-01-{day}T09:30:00Z"},
                     "assets": {role: {"href": f"s3://test/{role}"} for role in paths}},
            asset_paths=paths, asset_ids=ids, stac_source="https://example.org/stac",
            ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc), product_directory=folder,
            integrity={"mode": "strict", "evidence": {}, "source_assets": {
                role: {"status": "verified", "method": "stac_file_checksum", "algorithm": "sha256",
                       "declared_checksum_status": "valid"} for role in paths}},
        )
        write_acquisition_manifest(manifest, folder / "acquisition_manifest.json")
    aoi = tmp_path / "aoi"
    aoi.mkdir()
    gpd.GeoDataFrame(geometry=[box(500000, 99980, 500020, 100000)], crs="EPSG:32632").to_file(aoi / "akanda.geojson")
    return tmp_path


def test_local_adapter_uses_calibration_and_excludes_shadows(local_data):
    earlier, later = list_local_observations(local_data)
    result = analyze_local_akanda(earlier, later, local_data)
    assert result.scl_policy == "usable_land"
    assert result.analysis.comparable_mask.tolist() == [[False, True], [True, False]]
    assert result.analysis.comparable_coverage_percent == pytest.approx(50, abs=0.01)
    np.testing.assert_allclose(result.comparison.ndvi_date_1[0, 1], 0.6)
    assert result.analysis.statistics["mean"] == pytest.approx(0)


def test_adapter_rejects_reversed_dates(local_data):
    earlier, later = list_local_observations(local_data)
    with pytest.raises(LocalPreviewError, match="chronologique"):
        analyze_local_akanda(later, earlier, local_data)


def test_maps_share_ndvi_limits_and_show_missing_data(local_data):
    from src.components.ndvi_maps import ndvi_maps

    result = analyze_local_akanda(*list_local_observations(local_data), local_data)
    figure = ndvi_maps(result)
    maps = [axis.images[0] for axis in figure.axes if axis.images]
    assert len(maps) == 3
    assert [image.get_clim() for image in maps] == [(-1, 1), (-1, 1), (-2, 2)]
    assert all(image.get_array().mask[0, 0] for image in maps)
    assert "non comparable" in figure.legends[0].get_texts()[0].get_text()


def test_tampered_asset_fails_without_exposing_paths(local_data):
    earlier, later = list_local_observations(local_data)
    (earlier.manifest_path.parent / "b04.tif").write_bytes(b"changed")
    with pytest.raises(LocalPreviewError) as error:
        analyze_local_akanda(earlier, later, local_data)
    assert str(local_data) not in str(error.value)
    assert error.value.__suppress_context__


def test_unverified_manifest_is_rejected(local_data):
    earlier, _ = list_local_observations(local_data)
    manifest = json.loads(earlier.manifest_path.read_text())
    manifest.pop("integrity")
    earlier.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(LocalPreviewError, match="non vérifié"):
        list_local_observations(local_data)


def test_streamlit_renders_local_results_without_private_paths(local_data, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import src.config

    monkeypatch.setattr(src.config, "DATA_DIR", local_data)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
    assert not app.exception
    assert not app.error
    assert len(app.metric) == 4
    assert len(app.selectbox) == 2
    assert app.metric[0].value == "50,00 %"
    assert "limite administrative" in app.info[0].value
    for kind in (app.caption, app.info, app.warning):
        assert all(str(local_data) not in element.value for element in kind)
    app.selectbox[0].select_index(1)
    app.button[0].click().run()
    assert app.error
    assert "chronologique" in app.error[0].value


def test_streamlit_missing_data_is_safe(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import src.config

    monkeypatch.setattr(src.config, "DATA_DIR", tmp_path)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
    assert not app.exception
    assert "Deux acquisitions" in app.error[0].value
    assert str(tmp_path) not in app.error[0].value


def test_same_pair_submit_reuses_result_and_invalidates_changed_aoi(local_data, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from unittest.mock import Mock
    import src.config
    import src.services.local_preview as preview
    import src.components.preview_cache as images

    monkeypatch.setattr(src.config, "DATA_DIR", local_data)
    analyze = Mock(wraps=preview.analyze_local_akanda)
    monkeypatch.setattr(preview, "analyze_local_akanda", analyze)
    images.cached_map_png.clear()
    encode = Mock(wraps=images.render_ndvi_png)
    monkeypatch.setattr(images, "render_ndvi_png", encode)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
    original = app.session_state["result"].analysis.delta_ndvi.copy()
    app.button[0].click().run()
    assert not app.exception and not app.error
    assert analyze.call_count == 1
    assert encode.call_count == 1
    np.testing.assert_array_equal(original, app.session_state["result"].analysis.delta_ndvi)
    aoi = local_data / "aoi" / "akanda.geojson"
    aoi.write_bytes(aoi.read_bytes() + b"\n")
    app.run()
    assert not app.exception and not app.error
    assert analyze.call_count == 2
    assert encode.call_count == 2
    np.testing.assert_array_equal(original, app.session_state["result"].analysis.delta_ndvi)


@pytest.mark.parametrize("target", ["manifest", "b04", "b08", "scl", "calibration_xml", "aoi"])
def test_cache_key_invalidates_each_input(local_data, target):
    from src.services.local_preview import local_preview_key

    pair = list_local_observations(local_data)
    before = local_preview_key(*pair, local_data)
    manifest = json.loads(pair[0].manifest_path.read_text())
    if target == "manifest":
        path = pair[0].manifest_path
    elif target == "aoi":
        path = local_data / "aoi" / "akanda.geojson"
    else:
        path = pair[0].manifest_path.parent / manifest["assets"][target]["path"]
    path.write_bytes(path.read_bytes() + b"\n")
    assert before != local_preview_key(*pair, local_data)


def test_cache_key_includes_policy_and_threshold(local_data):
    from src.services.local_preview import local_preview_key

    pair = list_local_observations(local_data)
    key = local_preview_key(*pair, local_data)
    assert key != local_preview_key(*pair, local_data, threshold=0.2)
    assert key != local_preview_key(*pair, local_data, scl_policy="vegetation")


def test_cache_key_detects_same_size_asset_edit(local_data):
    import os
    from src.services.local_preview import local_preview_key

    pair = list_local_observations(local_data)
    key = local_preview_key(*pair, local_data)
    path = pair[0].manifest_path.parent / "b04.tif"
    stat = path.stat()
    contents = bytearray(path.read_bytes())
    contents[-1] ^= 1
    path.write_bytes(contents)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert path.stat().st_size == stat.st_size
    assert key != local_preview_key(*pair, local_data)


def test_cached_result_is_not_displayed_after_asset_tampering(local_data, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import src.config

    monkeypatch.setattr(src.config, "DATA_DIR", local_data)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
    observation = list_local_observations(local_data)[0]
    path = observation.manifest_path.parent / "b04.tif"
    path.write_bytes(path.read_bytes() + b"changed")
    app.run()
    assert not app.exception
    assert app.error
    assert not app.metric
    assert str(local_data) not in app.error[0].value


@pytest.mark.parametrize("missing_assets", [False, True])
def test_branding_and_asset_fallback(local_data, monkeypatch, missing_assets):
    from PIL import Image
    from streamlit.testing.v1 import AppTest
    from unittest.mock import Mock
    import streamlit as st
    import src.config

    monkeypatch.setattr(src.config, "DATA_DIR", local_data)
    if missing_assets:
        monkeypatch.setattr(src.config, "PROJECT_ROOT", local_data)
    configure = Mock(wraps=st.set_page_config)
    monkeypatch.setattr(st, "set_page_config", configure)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
    assert not app.exception and not app.error
    icon = configure.call_args.kwargs["page_icon"]
    slogans = [element for element in app.markdown if element.value == src.config.TAGLINE]
    if missing_assets:
        assert icon == "🌍"
        assert app.title[0].value == "GEONAF"
        assert len(slogans) == 1
    else:
        assert isinstance(icon, Image.Image)
        assert icon.size == (512, 512)
        assert not app.title
        assert not slogans  # The official logo already contains the slogan.
