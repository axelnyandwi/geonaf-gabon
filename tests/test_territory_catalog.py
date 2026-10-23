"""Territory catalogue: historical zones, referentials, map payloads, custom import; offline."""

import json
import socket

import geopandas as gpd
import pytest
from shapely.geometry import MultiPolygon, Polygon, box, mapping

from src.data.territory_registry import CUSTOM_IMPORT, GEONAF_ZONE, USER_PROVIDED, TerritoryRegistry
from src.services import territory_catalog as catalog
from src.services.resource_guards import ResourceLimits
from tests.test_geoboundaries import NOW, geojson, happy_path, metadata, run


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Appel réseau interdit dans ces tests.")
    monkeypatch.setattr(socket, "create_connection", refuse)


def feature_collection(*geometries):
    return json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": mapping(g)} for g in geometries]}).encode()


def load_both(tmp_path, level="ADM2"):
    run(tmp_path, happy_path())
    humanitarian = geojson([("Libreville", "H-1", box(9.40, 0.30, 9.55, 0.45)),
                            ("Komo-Mondah", "H-2", box(9.50, 0.30, 9.90, 0.70)),
                            ("Komo-Océan", "H-3", box(9.0, 0.0, 9.2, 0.2))])
    run(tmp_path, happy_path(metadata(admUnitCount="3", boundaryYearRepresented="2015",
                                      boundaryLicense="Creative Commons Attribution 3.0 Intergovernmental "
                                                      "Organisations (CC BY 3.0 IGO)"), humanitarian),
        release="gbHumanitarian")


def test_historical_zones_are_read_only_acquisition_zones(tmp_path):
    (tmp_path / "aoi").mkdir()
    gpd.GeoDataFrame(geometry=[box(9.40, 0.40, 9.50, 0.50)], crs=4326).to_file(tmp_path / "aoi" / "akanda.geojson")
    zones = catalog.historical_zones(tmp_path)
    assert [z.territory_id for z in zones] == ["akanda"]
    assert zones[0].legal_status == GEONAF_ZONE and "12 × 12 km" in zones[0].label and zones[0].record is None
    assert "pas une limite administrative" in zones[0].status_text
    assert not (tmp_path / "territories").exists()            # Nothing registered.


def test_referentials_are_listed_separately_and_never_fetched_implicitly(tmp_path):
    assert catalog.admin_territories(tmp_path, "gbOpen", "ADM2") == ()
    load_both(tmp_path)
    open_ = catalog.admin_territories(tmp_path, "gbOpen", "ADM2")
    humanitarian = catalog.admin_territories(tmp_path, "gbHumanitarian", "ADM2")
    assert [o.label for o in open_] == ["Komo-Mondah", "Libreville", "Ogooué-Ivindo"]
    assert {o.territory_id for o in open_}.isdisjoint({o.territory_id for o in humanitarian})
    assert catalog.admin_territories(tmp_path, "gbOpen", "ADM1") == ()


def test_pairs_keep_unmatched_units_visible(tmp_path):
    load_both(tmp_path)
    pairs, only_open, only_humanitarian = catalog.referential_pairs(tmp_path, "ADM2")
    assert sorted(a.label for a, _ in pairs) == ["Komo-Mondah", "Libreville"]
    assert [o.label for o in only_open] == ["Ogooué-Ivindo"] and [o.label for o in only_humanitarian] == ["Komo-Océan"]


def test_map_payloads_keep_two_distinct_outlines(tmp_path):
    load_both(tmp_path)
    a, b = catalog.referential_pairs(tmp_path, "ADM2")[0][0]
    payload = catalog.comparison_payload(a, b)
    assert payload["schema_version"] == "geonaf.territory-map/v1"
    ids = [layer["id"] for layer in payload["layers"]]
    assert ids == ["both", "b-only", "a", "b"]       # gbOpen lies inside gbHumanitarian: no empty part drawn.
    assert next(l for l in payload["layers"] if l["id"] == "b")["style"] == "dashed"
    assert "pas un changement réel" in payload["note"]
    single = catalog.selection_payload(a)
    assert single["layers"][0]["style"] == "outline" and len(single["bounds"]) == 2


def test_tampered_registered_geometry_is_not_displayed(tmp_path):
    load_both(tmp_path)
    option = catalog.admin_territories(tmp_path, "gbOpen", "ADM2")[0]
    option.geometry_path.write_bytes(option.geometry_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="modifiée"):
        catalog.geometry_wgs84(option)


def test_custom_import_is_user_provided_and_idempotent(tmp_path):
    content = feature_collection(box(9.40, 0.40, 9.45, 0.45), box(9.46, 0.40, 9.50, 0.45))
    record, assessment = catalog.import_custom_zone(tmp_path, content, label="Quartier test", author="Mairie (test)",
                                                    license_spdx="CC-BY-4.0", source_date="2026-10-23", now=NOW)
    assert record.kind == CUSTOM_IMPORT and record.legal_status == USER_PROVIDED and not record.is_official
    assert record.parts == 2 and assessment.allowed                     # One zone per operation (union).
    again, _ = catalog.import_custom_zone(tmp_path, content, label="Quartier test", author="Mairie (test)",
                                          license_spdx="CC-BY-4.0", source_date="2026-10-23", now=NOW)
    assert again == record and [o.label for o in catalog.custom_zones(tmp_path)] == ["Quartier test"]


@pytest.mark.parametrize("content, fields, message", [
    (feature_collection(Polygon([(9.0, 0.0), (9.1, 0.1), (9.1, 0.0), (9.0, 0.1)])), {}, "invalide"),
    (b"{" + b" " * (6 * 2**20), {}, "limite"),
    (feature_collection(box(9.4, 0.4, 9.5, 0.5)), {"license_spdx": " "}, "Licence"),
    (feature_collection(box(9.4, 0.4, 9.5, 0.5)), {"author": ""}, "Auteur"),
    (feature_collection(MultiPolygon([box(9.4 + i / 100, 0.4, 9.405 + i / 100, 0.405) for i in range(5)])),
     {"limits": ResourceLimits(max_import_parts=2)}, "parties"),
], ids=["invalide", "trop-gros", "sans-licence", "sans-auteur", "trop-de-parties"])
def test_custom_import_refusals_store_nothing(tmp_path, content, fields, message):
    arguments = dict(label="Zone", author="Auteur", license_spdx="CC-BY-4.0", source_date="2026", now=NOW)
    arguments.update(fields)
    with pytest.raises(ValueError, match=message):
        catalog.import_custom_zone(tmp_path, content, **arguments)
    assert TerritoryRegistry(tmp_path).territories() == ()
