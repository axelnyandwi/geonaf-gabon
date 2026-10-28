"""Versioned indicator profiles; another country or ecosystem gets a new profile, never an edit."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


@dataclass(frozen=True)
class IndicatorProfile:
    """Versioned groupings; another country or ecosystem gets a new profile, never an edit.

    Class codes refer to ``landcover_source`` (ESA WorldCover nomenclature).
    """

    profile_id: str
    version: str
    landcover_source: str
    vegetation_groups: tuple[tuple[str, tuple[int, ...]], ...]
    builtup_classes: tuple[int, ...]
    water_classes: tuple[int, ...]
    road_categories: tuple[tuple[str, tuple[str, ...]], ...]
    excluded_highways: tuple[str, ...]
    min_coverage_percent: float = 99.0

    @property
    def vegetation_classes(self) -> tuple[int, ...]:
        return tuple(code for _, codes in self.vegetation_groups for code in codes)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


AFRICA_DEFAULT_V1 = IndicatorProfile(
    profile_id="geonaf-indicateurs-afrique", version="1.0.0", landcover_source="esa-worldcover",
    vegetation_groups=(("Végétation naturelle", (10, 20, 30, 90)), ("Cultures", (40,)), ("Mangroves", (95,))),
    builtup_classes=(50,), water_classes=(80,),
    road_categories=(
        ("Axes principaux", ("motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link")),
        ("Axes secondaires", ("secondary", "secondary_link")),
        ("Axes tertiaires", ("tertiary", "tertiary_link")),
        ("Voies de desserte", ("residential", "unclassified", "living_street", "road")),
        ("Voies de service", ("service",)),
        ("Pistes", ("track",)),
        ("Chemins piétons et cyclables", ("footway", "path", "pedestrian", "cycleway", "steps", "bridleway")),
    ),
    # Not (or no longer) usable roads, or features that are not segments of a network.
    excluded_highways=("proposed", "construction", "abandoned", "disused", "razed", "platform", "raceway",
                       "bus_stop", "corridor", "elevator", "rest_area", "services", "emergency_bay"),
)
OTHER_ROADS = "Autres voies"
