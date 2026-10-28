"""Versioned indicator profiles; another country or ecosystem gets a new profile, never an edit."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math


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

EXPERIMENTAL, VALIDATED = "experimental", "validated"
SUPPORTED_INDICES = ("NDVI",)
RELIABILITY_CAPS = ("élevée", "moyenne", "faible")   # Same wording as src.indicators.model.


@dataclass(frozen=True)
class VegetationIndexProfile:
    """Versioned threshold on a spectral index; another territory, climate or ecosystem gets a new profile.

    The SCL validity rules are not part of this profile: they come from the
    scientific profile of the zone (``src.geo.scientific_profile``), which is
    left untouched. A pixel at or above ``threshold`` is counted, the
    comparison being made in the index raster's float32 precision.
    """

    profile_id: str
    version: str
    index: str
    threshold: float
    sensitivity_thresholds: tuple[float, ...]
    min_observed_coverage_percent: float     # Below it, no territorial estimate is published (diagnosis only).
    scientific_status: str                   # EXPERIMENTAL until an independent validation exists.
    reliability_cap: str                     # Highest reliability level the method may claim.
    context: str
    rationale: str

    def __post_init__(self):
        for name in ("profile_id", "version", "context", "rationale"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"Le profil de végétation doit renseigner {name}.")
        if self.index not in SUPPORTED_INDICES:
            raise ValueError("Indice spectral non pris en charge.")
        for value in (self.threshold, *self.sensitivity_thresholds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or not -1.0 <= value <= 1.0:
                raise ValueError("Seuil NDVI hors de [-1, 1] ou non numérique.")
        if not isinstance(self.sensitivity_thresholds, tuple) or self.threshold not in self.sensitivity_thresholds:
            raise ValueError("Les seuils de sensibilité doivent inclure le seuil du profil.")
        coverage = self.min_observed_coverage_percent
        if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not 0 <= coverage <= 100:
            raise ValueError("Couverture minimale hors de [0, 100].")
        if self.scientific_status not in (EXPERIMENTAL, VALIDATED):
            raise ValueError("Statut scientifique inconnu.")
        if self.reliability_cap not in RELIABILITY_CAPS:
            raise ValueError("Plafond de fiabilité inconnu.")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


S2_NDVI_VEGETATION_V0 = VegetationIndexProfile(
    profile_id="geonaf-vegetation-ndvi-s2", version="0.1.0-experimental", index="NDVI", threshold=0.4,
    sensitivity_thresholds=(0.3, 0.4, 0.5), min_observed_coverage_percent=30.0, scientific_status=EXPERIMENTAL,
    reliability_cap="faible",
    context="Afrique centrale équatoriale, Estuaire (Gabon) : seuil non calibré pour ce climat ni ces écosystèmes.",
    rationale="Seuil NDVI ≥ 0,4 retenu comme hypothèse de travail (décision projet du 28/10/2026), sans "
              "calibration locale. Non validé scientifiquement ; la couverture minimale de 30 % ne garantit pas "
              "la représentativité spatiale de la surface observée.",
)
