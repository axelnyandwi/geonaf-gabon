"""Dashboard map of the analysed perimeter (zone_context_payload); display only, no statistic."""

import os
from pathlib import Path

import pytest

from src.config import DATA_DIR
from src.services import territory_catalog as catalog

PACK = Path(os.environ.get("GEONAF_DEMO_PACK_DIR") or DATA_DIR / "demo_pack" / "geonaf-demo-akanda-2025-01-v3")
needs_pack = pytest.mark.skipif(not (PACK / "PACK_MANIFEST.json").is_file(), reason="Paquet de démonstration absent.")


def _zone(data_dir, aoi_id="akanda"):
    return next(option for option in catalog.historical_zones(data_dir) if option.territory_id == aoi_id)


@needs_pack
def test_zone_and_overlapping_units_are_drawn_distinctly_around_the_zone():
    zone = _zone(PACK)
    units = catalog.admin_territories(PACK, "gbOpen", "ADM2")
    payload, labels = catalog.zone_context_payload(zone, units)
    assert labels == ["Komo-Mondah", "Libreville"]                     # Units overlapping the Akanda square.
    styles = [(layer["id"], layer["style"]) for layer in payload["layers"]]
    assert styles == [("units", "dashed"), ("zone", "outline")]           # Shared borders drawn once.
    assert payload["layers"][0]["label"] == "Limites administratives : Komo-Mondah, Libreville"
    assert "pas une limite administrative" in payload["note"] and "non officielles" in payload["note"]
    assert payload["attributions"] and all("geoBoundaries" in text for text in payload["attributions"])
    left, bottom, right, top = catalog.geometry_wgs84(zone).bounds
    (west, south), (east, north) = payload["bounds"]
    assert west >= left - 0.02 - 1e-9 and east <= right + 0.02 + 1e-9    # The map stays on the zone.
    assert south >= bottom - 0.02 - 1e-9 and north <= top + 0.02 + 1e-9
    assert {"area", "share", "statistics"}.isdisjoint(payload)           # No statistic per unit.


@needs_pack
def test_without_referential_only_the_zone_is_drawn():
    payload, labels = catalog.zone_context_payload(_zone(PACK), ())
    assert labels == [] and [layer["id"] for layer in payload["layers"]] == ["zone"]
    assert "Aucune limite administrative locale" in payload["note"] and payload["attributions"] == []
