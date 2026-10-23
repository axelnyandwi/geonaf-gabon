"""geoBoundaries adapter with the real HTTP client on a fake session; offline."""

from datetime import datetime, timezone
import json
import socket

import pytest
from shapely.geometry import Polygon, box, mapping

from src.data.geoboundaries import (
    API_URL, HOSTS, GeoBoundariesError, fetch_boundaries, import_geoboundaries, license_of, territory_id,
)
from src.data.territory_registry import COMMUNITY, HUMANITARIAN, TerritoryRegistry
from tests.test_external_retrieval_core import FakeResponse, client

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
DOWNLOAD = "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/GAB/ADM2/geoBoundaries-GAB-ADM2.geojson"
MEDIA = "https://media.githubusercontent.com/media/wmgeolab/geoBoundaries/9469f09/releaseData/gbOpen/GAB/ADM2/x.geojson"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Appel réseau interdit dans ces tests.")
    monkeypatch.setattr(socket, "create_connection", refuse)


def metadata(release="gbOpen", level="ADM2", **overrides):
    values = {"boundaryISO": "GAB", "boundaryType": level, "boundaryName": "Gabon", "boundaryYearRepresented": "2018",
              "boundarySource": "geoBoundaries, Wikimedia", "boundaryLicense": "Creative Commons Attribution 3.0 License",
              "licenseSource": "commons.wikimedia.org/wiki/File", "buildDate": "Dec 12, 2023",
              "boundaryCanonical": "Department", "admUnitCount": "4", "gjDownloadURL": DOWNLOAD}
    values.update(overrides)
    return json.dumps(values).encode()


def geojson(units=None):
    units = units or [("Libreville", "GAB-ADM2-1", box(9.40, 0.30, 9.50, 0.45)),
                      ("Komo-Mondah", "GAB-ADM2-2", box(9.50, 0.30, 9.80, 0.70)),
                      ("Ogooué-Ivindo\t", "GAB-ADM2-3", box(12.0, 0.0, 13.0, 1.0)),
                      ("Bad", "GAB-ADM2-4", Polygon([(9.0, 0.0), (9.1, 0.1), (9.1, 0.0), (9.0, 0.1)]))]
    return json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"shapeName": name, "shapeID": unit_id, "shapeISO": "", "shapeGroup": "GAB",
                                           "shapeType": "ADM2"}, "geometry": mapping(geometry)}
        for name, unit_id, geometry in units]}).encode()


def redirect(location):
    return FakeResponse(302, b"", {"Location": location})


def happy_path(meta=None, body=None):
    return [FakeResponse(200, meta or metadata()), redirect(MEDIA), FakeResponse(200, body or geojson())]


def run(tmp_path, responses, **kwargs):
    return import_geoboundaries(tmp_path, release=kwargs.pop("release", "gbOpen"), iso3="GAB",
                                level=kwargs.pop("level", "ADM2"), client=client(responses, hosts=HOSTS), now=NOW,
                                **kwargs)


def test_import_registers_valid_units_with_full_provenance(tmp_path):
    report = run(tmp_path, happy_path())
    assert not report.snapshot.reused and report.snapshot.network_bytes > 0
    assert report.features == 4 and report.declared_units == 4
    assert [r.territory_id for r in report.registered] == [
        "gab-gbopen-adm2-libreville", "gab-gbopen-adm2-komo-mondah", "gab-gbopen-adm2-ogooue-ivindo"]
    assert report.rejected == (("Bad", report.rejected[0][1]),) and "invalide" in report.rejected[0][1]
    record = report.registered[0]
    assert record.legal_status == COMMUNITY and not record.is_official and record.label == "Libreville"
    assert report.registered[2].label == "Ogooué-Ivindo"          # Tab removed, accents kept.
    source = record.source
    assert (source.source_id, source.version, source.license_spdx, source.reference_year, source.share_alike) == (
        "geoboundaries-gbopen", "2023-12-12", "CC-BY-3.0", 2018, False)
    assert source.url == DOWNLOAD and source.retrieved_at_utc == NOW.isoformat() and "Wikimedia" in source.attribution
    assert (record.admin.level_code, record.admin.local_type, record.admin.source_unit_id) == (
        "ADM2", "Department", "GAB-ADM2-1")
    assert any("non officielles" in limit for limit in record.limits)
    stored = json.loads(report.snapshot.path.with_name(report.snapshot.path.name.replace(".geojson", ".meta.json"))
                        .read_text(encoding="utf-8"))
    assert stored["sha256"] == report.snapshot.sha256 and stored["retrieved_at_utc"] == NOW.isoformat()


def test_same_build_is_reused_without_downloading(tmp_path):
    first = run(tmp_path, happy_path())
    again = run(tmp_path, [FakeResponse(200, metadata())])        # Only the metadata request is served.
    assert again.snapshot.reused and again.snapshot.network_bytes == 0
    assert again.registered == first.registered                    # Idempotent: no new version.


def test_new_build_creates_new_versions_and_keeps_history(tmp_path):
    first = run(tmp_path, happy_path())
    changed = geojson([("Libreville", "GAB-ADM2-1", box(9.40, 0.30, 9.52, 0.45))])
    second = run(tmp_path, happy_path(metadata(buildDate="Jan 05, 2026", admUnitCount="1"), changed))
    assert second.registered[0].version == 2 and second.registered[0].supersedes == first.registered[0].fingerprint
    assert first.snapshot.path.exists() and second.snapshot.path != first.snapshot.path
    registry = TerritoryRegistry(tmp_path)
    assert registry.get("gab-gbopen-adm2-libreville", 1) == first.registered[0]


def test_two_releases_stay_separate(tmp_path):
    run(tmp_path, happy_path())
    humanitarian = run(tmp_path, happy_path(metadata(
        boundaryLicense="Creative Commons Attribution 3.0 Intergovernmental Organisations (CC BY 3.0 IGO)",
        boundaryCanonical="Unknown", boundaryYearRepresented="2015")), release="gbHumanitarian")
    record = humanitarian.registered[0]
    assert record.territory_id == "gab-gbhumanitarian-adm2-libreville" and record.legal_status == HUMANITARIAN
    assert record.source.license_spdx == "CC-BY-3.0-IGO" and record.admin.local_type == "non déclaré par la source (ADM2)"
    registry = TerritoryRegistry(tmp_path)
    assert {r.territory_id for r in registry.territories() if r.label == "Libreville"} == {
        "gab-gbopen-adm2-libreville", "gab-gbhumanitarian-adm2-libreville"}


def test_duplicate_names_get_distinct_identifiers(tmp_path):
    body = geojson([("Noya", "GAB-ADM2-7", box(9.6, 0.5, 9.7, 0.6)), ("Noya", "GAB-ADM2-8", box(9.7, 0.5, 9.8, 0.6))])
    report = run(tmp_path, happy_path(metadata(admUnitCount="2"), body))
    ids = [r.territory_id for r in report.registered]
    assert len(set(ids)) == 2 and all(i.startswith("gab-gbopen-adm2-noya-") for i in ids)
    assert territory_id("gbOpen", "GAB", "ADM2", "Noya", "GAB-ADM2-7", True) == ids[0]


def test_unpublished_level_is_not_invented(tmp_path):
    with pytest.raises(GeoBoundariesError, match="ne publie pas le niveau ADM3"):
        run(tmp_path, [FakeResponse(404, b"")], level="ADM3")
    assert TerritoryRegistry(tmp_path).territories() == ()


@pytest.mark.parametrize("responses, message", [
    ([FakeResponse(200, metadata()), redirect("https://evil.example.org/x.geojson")], "Hôte non autorisé"),
    ([FakeResponse(200, metadata()), redirect("http://media.githubusercontent.com/x")], "non HTTPS"),
    ([FakeResponse(200, metadata())] + [redirect(MEDIA)] * 4, "Trop de redirections"),
    ([FakeResponse(200, metadata()), FakeResponse(302, b"")], "sans destination"),
    ([FakeResponse(200, metadata()), redirect(MEDIA), FakeResponse(200, b"x" * 10, {"Content-Length": str(64 * 2**20)})],
     "ResponseTooLarge"),
    ([FakeResponse(200, metadata()), redirect(MEDIA), FakeResponse(200, b"<html>maintenance</html>")],
     "GeoJSON invalide"),
    ([FakeResponse(200, metadata()), redirect(MEDIA),
      FakeResponse(200, json.dumps({"type": "FeatureCollection", "features": []}).encode())], "sans entité"),
    ([FakeResponse(503, b"")] * 3, "inaccessibles"),
    ([FakeResponse(200, b"not json")], "JSON attendu"),
    ([FakeResponse(200, metadata(boundaryISO="CMR"))], "incohérentes"),
    ([FakeResponse(200, metadata(gjDownloadURL=""))], "incomplètes"),
    ([FakeResponse(200, metadata(boundaryLicense="Licence maison"))], "Licence non reconnue"),
    ([FakeResponse(200, metadata(boundaryLicense="Creative Commons Attribution-NonCommercial 4.0"))],
     "non commerciale"),
])
def test_protected_retrieval_refuses_and_stores_nothing(tmp_path, responses, message):
    with pytest.raises(GeoBoundariesError, match=message):
        run(tmp_path, responses)
    assert TerritoryRegistry(tmp_path).territories() == ()
    assert not (tmp_path / "external" / "geoboundaries").exists() or not any(
        p.suffix == ".geojson" for p in (tmp_path / "external" / "geoboundaries").rglob("*"))


def test_share_alike_licence_is_recorded():
    assert license_of({"boundaryLicense": "Creative Commons Attribution-ShareAlike 3.0 Unported"}) == (
        "CC-BY-SA-3.0", True)


def test_tampered_snapshot_is_detected(tmp_path):
    report = run(tmp_path, happy_path())
    report.snapshot.path.write_bytes(report.snapshot.path.read_bytes() + b" ")
    from src.services.external_layers import open_store

    store, _ = open_store(tmp_path)
    with pytest.raises(GeoBoundariesError, match="modifié"):
        fetch_boundaries(client([FakeResponse(200, metadata())], hosts=HOSTS), store, release="gbOpen", iso3="GAB",
                         level="ADM2")


def test_metadata_endpoint_is_per_level():
    assert API_URL.format(release="gbOpen", iso3="GAB", level="ADM2").endswith("/gbOpen/GAB/ADM2/")
