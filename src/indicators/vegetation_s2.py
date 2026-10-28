"""Experimental vegetation estimate from one Sentinel-2 NDVI observation: pure computation on arrays.

No file is read here. The NDVI, its validity mask and the exact pixel/zone
area weights come from the existing chain (``analyze_ndvi_observation``);
SCL classes, when supplied, come from ``scl_values_on_grid`` on the same grid.

Every pixel touching the zone receives exactly one label, in this priority
order (first matching rule wins, so labels never overlap):

1. ``vegetated``          valid and NDVI ≥ threshold (float32 comparison);
2. ``not_vegetated``      valid and NDVI < threshold;
3. ``outside_footprint``  SCL absent there (source NoData, class 0, outside the SCL raster);
4. ``not_observed``       SCL defective, cloud, cloud shadow or cirrus (1, 3, 8, 9, 10);
5. ``excluded_observed``  observed class outside the SCL policy (e.g. water 6, dark 2, unclassified 7);
6. ``other_invalid``      class accepted by the policy but invalid reflectance/NDVI (raster NoData, non
                          finite or out of [-1, 1]).

Without SCL classes, rules 3–6 collapse into ``unsplit`` (not usable, reason
unknown). The part of the zone not covered by the grid is added to
``outside_footprint``. Areas are sums of exact equal-area weights (EPSG:6933),
so partially covered pixels count for their part inside the zone; the labels
add up to the zone area by construction (checked).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.data.cloud_mask import (
    SCL_CLOUD_SHADOW_CLASSES, SCL_DEFECTIVE_CLASSES, SCL_MASK_POLICIES, SCL_NO_DATA_CLASSES,
)
from src.geo.change_analysis import EqualAreaAOI, measure_mask_coverage

VEGETATED, NOT_VEGETATED, OUTSIDE_FOOTPRINT, NOT_OBSERVED, EXCLUDED_OBSERVED, OTHER_INVALID, UNSPLIT = range(1, 8)
LABEL_NAMES = {VEGETATED: "vegetated", NOT_VEGETATED: "not_vegetated", OUTSIDE_FOOTPRINT: "outside_footprint",
               NOT_OBSERVED: "not_observed", EXCLUDED_OBSERVED: "excluded_observed",
               OTHER_INVALID: "other_invalid", UNSPLIT: "unsplit"}
SCL_NOT_OBSERVED_CLASSES = SCL_DEFECTIVE_CLASSES | SCL_CLOUD_SHADOW_CLASSES
PARTITION_RTOL = 1e-9


@dataclass(frozen=True)
class VegetationMeasure:
    """Areas in m² of one observation over the zone; ``None`` where SCL classes were not supplied."""

    zone_m2: float
    observed_m2: float                  # Usable observed area (validity mask of the scientific profile).
    vegetated_m2: float                 # Observed area with NDVI ≥ threshold.
    not_vegetated_m2: float
    outside_footprint_m2: float | None
    not_observed_m2: float | None
    excluded_observed_m2: float | None
    other_invalid_m2: float | None
    unsplit_m2: float | None            # Not usable, reason unknown (no SCL classes).
    threshold: float
    sensitivity_m2: tuple[tuple[float, float], ...]   # (threshold, vegetated m²) on the same observed area.
    scl_policy: str
    resolution: tuple[float, float]
    crs: str
    observed_pixels: int
    vegetated_pixels: int

    @property
    def observed_coverage_percent(self) -> float:
        return 100.0 * self.observed_m2 / self.zone_m2

    @property
    def vegetated_share_percent(self) -> float | None:
        """Share of the observed area, area-weighted; never a share of the whole zone."""
        return None if self.observed_m2 <= 0 else 100.0 * self.vegetated_m2 / self.observed_m2


def _check_inputs(ndvi, valid, weights, grid, scl):
    shape = (grid.height, grid.width)
    if not isinstance(ndvi, np.ndarray) or ndvi.dtype != np.float32:
        raise TypeError("NDVI attendu en float32 (précision de la comparaison au seuil).")
    for name, array in (("NDVI", ndvi), ("masque de validité", valid), ("poids surfaciques", weights)):
        if np.shape(array) != shape:
            raise ValueError(f"Dimensions du {name} différentes de la grille.")
    if scl is not None and any(np.shape(array) != shape for array in scl):
        raise ValueError("Dimensions des classes SCL différentes de la grille.")
    xres, yres = (abs(float(value)) for value in grid.resolution)
    if not (xres > 0 and yres > 0):
        raise ValueError("Résolution de grille invalide.")


def partition_labels(ndvi, valid, threshold: float, policy: str, scl=None) -> np.ndarray:
    """One label per pixel (uint8, see module docstring); ``scl``: (classes uint8, scl_valid bool) or None."""
    if policy not in SCL_MASK_POLICIES:
        raise ValueError(f"Politique SCL inconnue : {policy}.")
    valid = np.asarray(valid, dtype=bool)
    above = ndvi >= np.float32(threshold)
    conditions, labels = [valid & above, valid & ~above], [VEGETATED, NOT_VEGETATED]
    if scl is None:
        default = UNSPLIT
    else:
        classes, scl_valid = np.asarray(scl[0]), np.asarray(scl[1], dtype=bool)
        known = scl_valid & ~np.isin(classes, tuple(SCL_NO_DATA_CLASSES))
        conditions += [~known, np.isin(classes, tuple(SCL_NOT_OBSERVED_CLASSES)),
                       ~np.isin(classes, tuple(SCL_MASK_POLICIES[policy]))]
        labels += [OUTSIDE_FOOTPRINT, NOT_OBSERVED, EXCLUDED_OBSERVED]
        default = OTHER_INVALID
    return np.select(conditions, labels, default=default).astype(np.uint8)


def measure_vegetation(ndvi, valid, weights, grid, zone: EqualAreaAOI, *, threshold: float, policy: str,
                       scl=None, sensitivity=()) -> VegetationMeasure:
    """Partition the zone for one observation and measure each part (see module docstring)."""
    _check_inputs(ndvi, valid, weights, grid, scl)
    weights = np.asarray(weights)
    labels = partition_labels(ndvi, valid, threshold, policy, scl)
    area = lambda mask: measure_mask_coverage(mask, grid, zone, pixel_area_weights=weights)
    parts = {label: area(labels == label) for label in LABEL_NAMES}
    observed = area(np.asarray(valid, dtype=bool))   # Same sum as NDVIObservation.valid_area_m2.
    uncovered = zone.area_m2 - float(weights.sum(dtype=np.float64))
    if uncovered < -PARTITION_RTOL * zone.area_m2:
        raise ValueError("Poids surfaciques supérieurs à l'aire de la zone : grille ou zone incohérente.")
    uncovered = max(uncovered, 0.0)
    total = sum(part.area_m2 for part in parts.values()) + uncovered
    if abs(total - zone.area_m2) > PARTITION_RTOL * zone.area_m2:
        raise ValueError("Partition de la zone incomplète : somme des catégories différente de son aire.")
    split = scl is not None
    m2 = lambda label: parts[label].area_m2
    usable = np.asarray(valid, dtype=bool)
    sensitivity_m2 = tuple((float(t), m2(VEGETATED) if t == threshold else
                            area(usable & (ndvi >= np.float32(t))).area_m2)
                           for t in sorted(set(sensitivity) | {threshold}))
    return VegetationMeasure(
        zone_m2=zone.area_m2, observed_m2=observed.area_m2, vegetated_m2=m2(VEGETATED),
        not_vegetated_m2=m2(NOT_VEGETATED),
        outside_footprint_m2=(m2(OUTSIDE_FOOTPRINT) if split else 0.0) + uncovered,
        not_observed_m2=m2(NOT_OBSERVED) if split else None,
        excluded_observed_m2=m2(EXCLUDED_OBSERVED) if split else None,
        other_invalid_m2=m2(OTHER_INVALID) if split else None,
        unsplit_m2=None if split else m2(UNSPLIT),
        threshold=float(threshold), sensitivity_m2=sensitivity_m2,
        scl_policy=policy, resolution=tuple(abs(float(v)) for v in grid.resolution), crs=str(grid.crs),
        observed_pixels=observed.pixel_count, vegetated_pixels=parts[VEGETATED].pixel_count)


def measure_observed_vegetation(observation, profile, zone: EqualAreaAOI, *, scl=None) -> VegetationMeasure:
    """:func:`measure_vegetation` on an ``NDVIObservation`` with a ``VegetationIndexProfile``."""
    return measure_vegetation(observation.ndvi, observation.valid, observation.pixel_area_weights,
                              observation.grid, zone, threshold=profile.threshold,
                              policy=observation.scl_policy, scl=scl,
                              sensitivity=profile.sensitivity_thresholds)
