"""Mapped roads and buildings inside the zone (OpenStreetMap), in the zone's local UTM projection."""

from __future__ import annotations

from src.indicators.profiles import AFRICA_DEFAULT_V1, OTHER_ROADS, IndicatorProfile
from src.indicators.zone import metric_zone


def road_lengths(frame, aoi_path, profile: IndicatorProfile = AFRICA_DEFAULT_V1):
    """Return (total km, [(category, km, ((highway, km), …))], excluded km, area-highway count).

    Lengths of mapped ``highway`` ways clipped to the AOI, in the local UTM zone.
    Each OSM way counts once: a dual carriageway mapped as two ways counts twice.
    """
    aoi, crs = metric_zone(aoi_path)
    roads = frame[frame["category"] == "highway"].to_crs(crs)
    roads = roads[roads.intersects(aoi)]
    clipped = roads.geometry.intersection(aoi)
    lines = clipped.geom_type.isin(["LineString", "MultiLineString"])
    lengths = (clipped[lines].length / 1000.0).groupby(roads.loc[lines, "detail"].fillna("?")).sum()
    by_value = {str(value): float(km) for value, km in lengths.items()}
    category_of = {value: label for label, values in profile.road_categories for value in values}
    excluded = sum(km for value, km in by_value.items() if value in profile.excluded_highways)
    groups = {}
    for value, km in sorted(by_value.items(), key=lambda item: -item[1]):
        if value in profile.excluded_highways:
            continue
        groups.setdefault(category_of.get(value, OTHER_ROADS), []).append((value, km))
    order = [label for label, _ in profile.road_categories] + [OTHER_ROADS]
    categories = [(label, sum(km for _, km in groups[label]), tuple(groups[label])) for label in order if label in groups]
    return sum(km for _, km, _ in categories), categories, excluded, int((~lines).sum())


def building_figures(frame, aoi_path) -> tuple[int, float, int]:
    """Return (count, footprint km², straddling count) of mapped buildings.

    A building belongs to the AOI when its representative point (always inside
    the polygon) lies in the AOI: adjacent study zones never count it twice.
    The footprint is the building area clipped to the AOI.
    """
    aoi, crs = metric_zone(aoi_path)
    buildings = frame[frame["category"] == "building"].to_crs(crs)
    buildings = buildings[buildings.intersects(aoi)]
    counted = buildings.geometry.representative_point().intersects(aoi)
    footprint = float(buildings.geometry.intersection(aoi).area.sum()) / 1e6
    straddling = int((~buildings.geometry.within(aoi)).sum())
    return int(counted.sum()), footprint, straddling
