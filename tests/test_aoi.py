"""Tests isolés des fonctions de gestion des AOI."""

import json

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from src.geo import aoi


def _write_geojson(path, geometry, crs="EPSG:4326"):
    """Écrit une géométrie synthétique dans un GeoJSON temporaire."""
    frame = gpd.GeoDataFrame(
        {"geometry": [geometry]},
        geometry="geometry",
        crs=crs,
    )
    frame.to_file(path, driver="GeoJSON")
    return path


def test_load_aoi_uses_lowercase_filename_and_reads_geojson(tmp_path, monkeypatch):
    """Le nom logique devient un nom de fichier minuscule sous AOI_DIR."""
    aoi_dir = tmp_path / "aoi"
    aoi_dir.mkdir()
    expected = {"type": "FeatureCollection", "features": []}
    (aoi_dir / "libreville.geojson").write_text(
        json.dumps(expected), encoding="utf-8"
    )
    monkeypatch.setattr(aoi, "AOI_DIR", aoi_dir)

    assert aoi.load_aoi("LIBREVILLE") == expected


def test_load_aoi_missing_file_raises_file_not_found(tmp_path, monkeypatch):
    """Une AOI absente produit une erreur explicite au lieu d'un JSON vide."""
    monkeypatch.setattr(aoi, "AOI_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="AOI introuvable"):
        aoi.load_aoi("inconnue")


def test_get_aoi_bbox_orders_bounds_as_minx_miny_maxx_maxy(tmp_path):
    """Les bornes sont triées, même si les sommets sont fournis en sens inverse."""
    geometry = Polygon(
        [(10.0, 2.0), (10.0, 1.0), (8.0, 1.0), (8.0, 2.0)]
    )
    path = _write_geojson(tmp_path / "aoi.geojson", geometry)

    assert aoi.get_aoi_bbox(path) == [8.0, 1.0, 10.0, 2.0]


def test_get_aoi_bbox_reprojects_to_wgs84_and_preserves_extent(tmp_path):
    """Une AOI projetée revient en degrés et garde une emprise non nulle."""
    geometry = Polygon(
        [
            (500000, 40000),
            (510000, 40000),
            (510000, 50000),
            (500000, 50000),
        ]
    )
    path = _write_geojson(tmp_path / "projected.geojson", geometry, "EPSG:32632")

    bbox = aoi.get_aoi_bbox(path)

    assert len(bbox) == 4
    assert bbox[0] == pytest.approx(9.0, abs=0.1)
    assert -1.0 < bbox[1] < bbox[3] < 1.0
    assert bbox[0] < bbox[2]
    assert bbox[2] - bbox[0] > 0
    assert bbox[3] - bbox[1] > 0


def test_get_aoi_bbox_rejects_empty_aoi(tmp_path, monkeypatch):
    """Une couche sans géométrie ne peut pas fournir de bornes."""
    empty = gpd.GeoDataFrame(
        {"geometry": []}, geometry="geometry", crs="EPSG:4326"
    )
    monkeypatch.setattr(gpd, "read_file", lambda _path: empty)

    with pytest.raises(ValueError, match="AOI vide"):
        aoi.get_aoi_bbox(tmp_path / "empty.geojson")


def test_get_aoi_bbox_rejects_missing_crs(tmp_path, monkeypatch):
    """Sans CRS, les coordonnées ne peuvent pas être interprétées correctement."""
    frame = gpd.GeoDataFrame(
        {"geometry": [Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])]},
        geometry="geometry",
    )
    monkeypatch.setattr(gpd, "read_file", lambda _path: frame)

    with pytest.raises(ValueError, match="ne possède pas de CRS"):
        aoi.get_aoi_bbox(tmp_path / "no-crs.geojson")


def test_get_aoi_bbox_rejects_invalid_geometry(tmp_path):
    """Une auto-intersection ne doit pas devenir une emprise AOI exploitable."""
    bow_tie = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    path = _write_geojson(tmp_path / "invalid.geojson", bow_tie)

    with pytest.raises(ValueError, match="géométrie.*invalide|invalide.*géométrie"):
        aoi.get_aoi_bbox(path)


def test_get_aoi_bbox_rejects_coordinates_outside_wgs84_ranges(tmp_path):
    """Les longitudes et latitudes WGS84 doivent rester dans leurs plages légales."""
    outside_wgs84 = Polygon(
        [(200, 0), (201, 0), (201, 1), (200, 1), (200, 0)]
    )
    path = _write_geojson(tmp_path / "outside-wgs84.geojson", outside_wgs84)

    with pytest.raises(ValueError, match="coordonnées.*WGS84|WGS84.*coordonnées"):
        aoi.get_aoi_bbox(path)
