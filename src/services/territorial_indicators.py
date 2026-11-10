"""Territorial key figures for the public: stored layers only, no UI, no network.

Sources already integrated are reused as they are:

* ESA WorldCover (``Classification externe``) → land, vegetation, built-up
  and water areas, on the WorldCover native grid with exact cell/AOI area
  weights over **every** classified cell. The Sentinel-2 NDVI mask is never
  used: land cover areas do not depend on clouds of one acquisition.
* OpenStreetMap (``Base collaborative``) → lengths of mapped road segments by
  category, mapped buildings (count, footprint, densities). OSM values are
  what has been mapped, not what exists on the ground.

Every value carries its unit, AOI, three distinct dates (observation period,
reference dataset state, ingestion), source, coverage, method, reliability
level and limits. A missing source gives ``value=None`` (status
``indisponible``), never an estimate. A temporal change is only produced by
:func:`assess_change` when both values are strictly comparable.

The implementation lives in :mod:`src.indicators` (one module per
responsibility); this module keeps the historical import path.
"""

from src.indicators.assembly import build_indicators, compute_territorial_indicators
from src.indicators.inventory import IndicatorInputs, indicator_inputs, inputs_for_zone
from src.indicators.landcover import landcover_areas
from src.indicators.model import (
    AOI_NATURE, AVAILABLE, HIGH, LOW, MEDIUM, NOT_ASSESSED, UNAVAILABLE, BreakdownItem, ChangeAssessment,
    IndicatorDates, IndicatorSource, TerritorialIndicator, TerritorialIndicators,
)
from src.indicators.osm import building_figures, road_lengths
from src.indicators.profiles import AFRICA_DEFAULT_V1, OTHER_ROADS, IndicatorProfile
from src.indicators.provenance import LANDCOVER_LIMITS, OSM_LIMITS
from src.indicators.quality import assess_change

__all__ = [
    "AFRICA_DEFAULT_V1", "AOI_NATURE", "AVAILABLE", "HIGH", "LANDCOVER_LIMITS", "LOW", "MEDIUM", "NOT_ASSESSED",
    "OSM_LIMITS", "OTHER_ROADS", "UNAVAILABLE", "BreakdownItem", "ChangeAssessment", "IndicatorDates",
    "IndicatorInputs", "IndicatorProfile", "IndicatorSource", "TerritorialIndicator", "TerritorialIndicators",
    "assess_change", "build_indicators", "building_figures", "compute_territorial_indicators", "indicator_inputs",
    "inputs_for_zone", "landcover_areas", "road_lengths",
]
