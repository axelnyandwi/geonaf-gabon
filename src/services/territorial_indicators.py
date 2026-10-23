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
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np

from src.services.multisource_demo import CLASSIFICATION, COLLABORATIVE

AVAILABLE, UNAVAILABLE = "disponible", "indisponible"
HIGH, MEDIUM, LOW, NOT_ASSESSED = "élevée", "moyenne", "faible", "non évaluée"
AOI_NATURE = "Géométrie de référence"


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


@dataclass(frozen=True)
class IndicatorDates:
    """Three dates never merged: what was observed, which dataset state, when it was ingested."""

    observation_start_utc: datetime | None   # None: unknown (e.g. OSM objects' field date).
    observation_end_utc: datetime | None
    observation_note: str
    reference_label: str                     # Version or database state of the reference dataset.
    reference_at_utc: datetime | None        # Database timestamp when one exists (OSM).
    ingested_at_utc: datetime | None         # Retrieval by GEONAF.


@dataclass(frozen=True)
class IndicatorSource:
    source_id: str
    product: str
    version: str
    nature: str
    attribution: str
    fingerprint: str | None


@dataclass(frozen=True)
class BreakdownItem:
    key: str
    label: str
    value: float
    unit: str
    share_percent: float | None = None
    detail: tuple[tuple[str, float], ...] = ()   # e.g. (highway value, km) or (class label, km²).


@dataclass(frozen=True)
class TerritorialIndicator:
    indicator_id: str
    label: str
    value: float | None
    unit: str
    status: str
    aoi_id: str
    aoi_sha256: str
    dates: IndicatorDates | None
    source: IndicatorSource | None
    coverage_percent: float | None
    method: str
    reliability: str
    reliability_reasons: tuple[str, ...]
    limits: tuple[str, ...]
    profile_fingerprint: str
    shares: tuple[tuple[str, float], ...] = ()          # (denominator, percent)
    breakdown: tuple[BreakdownItem, ...] = ()


@dataclass(frozen=True)
class ChangeAssessment:
    indicator_id: str
    comparable: bool
    kind: str
    delta: float | None
    unit: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TerritorialIndicators:
    aoi_id: str
    aoi_label: str
    aoi_sha256: str
    aoi_area_km2: float
    profile: IndicatorProfile
    indicators: tuple[TerritorialIndicator, ...]
    temporal_notes: tuple[str, ...]
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    computed_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def get(self, indicator_id: str) -> TerritorialIndicator:
        return next(item for item in self.indicators if item.indicator_id == indicator_id)


# --- Calculations on loaded inputs -------------------------------------------------------------------


def _metric_aoi(aoi_path):
    """AOI union in its local UTM zone (lengths, footprints); the zone follows the AOI anywhere in Africa."""
    import geopandas as gpd

    aoi = gpd.read_file(aoi_path)
    if aoi.empty or aoi.crs is None:
        raise ValueError("L'AOI doit être non vide et avoir un CRS.")
    crs = aoi.estimate_utm_crs()
    return aoi.to_crs(crs).geometry.union_all(), crs


def _landcover_grid(tiles):
    """(grid, codes, valid) of WorldCover tiles on their own lattice; no resampling of classes."""
    import rasterio
    from rasterio.transform import from_origin

    from src.geo.temporal import RasterGrid, grid_from_raster
    from src.services.multisource import categorical_tiles_on_grid

    paths = [Path(path) for path, _ in tiles]
    if len(paths) == 1:
        grid = grid_from_raster(paths[0])
        with rasterio.open(paths[0]) as source:
            codes = source.read(1)
            valid = codes != 0
            if source.nodata is not None:
                valid &= codes != source.nodata
        return grid, codes, valid
    with rasterio.open(paths[0]) as first:
        crs, (xres, yres) = first.crs, first.res
    bounds = []
    for path in paths:
        with rasterio.open(path) as source:
            if source.crs != crs or not np.allclose(source.res, (xres, yres)):
                raise ValueError("Tuiles d'occupation du sol sur des grilles différentes.")
            bounds.append(source.bounds)
    left, top = min(b.left for b in bounds), max(b.top for b in bounds)
    width = int(round((max(b.right for b in bounds) - left) / xres))
    height = int(round((top - min(b.bottom for b in bounds)) / yres))
    grid = RasterGrid(crs, (xres, yres), from_origin(left, top, xres, yres), width, height)
    mosaic = categorical_tiles_on_grid([(path.stem, path) for path in paths], grid)
    return grid, np.where(mosaic.valid, mosaic.values, 0), mosaic.valid


def landcover_areas(tiles, aoi_path) -> tuple[dict[int, float], float, float]:
    """Return ({class: m²}, classified m², AOI m²) inside the AOI, exact equal-area weights.

    All classified cells count, whatever any Sentinel-2 mask says.
    """
    from src.geo.change_analysis import load_equal_area_aoi, prepare_pixel_area_weights

    grid, codes, valid = _landcover_grid(tiles)
    aoi = load_equal_area_aoi(aoi_path)
    weights = prepare_pixel_area_weights(grid, aoi)
    inside = valid & (weights > 0)
    areas = {int(code): float(weights[inside & (codes == code)].sum(dtype=np.float64))
             for code in np.unique(codes[inside])}
    return areas, float(weights[inside].sum(dtype=np.float64)), float(aoi.area_m2)


def road_lengths(frame, aoi_path, profile: IndicatorProfile = AFRICA_DEFAULT_V1):
    """Return (total km, [(category, km, ((highway, km), …))], excluded km, area-highway count).

    Lengths of mapped ``highway`` ways clipped to the AOI, in the local UTM zone.
    Each OSM way counts once: a dual carriageway mapped as two ways counts twice.
    """
    aoi, crs = _metric_aoi(aoi_path)
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
    aoi, crs = _metric_aoi(aoi_path)
    buildings = frame[frame["category"] == "building"].to_crs(crs)
    buildings = buildings[buildings.intersects(aoi)]
    counted = buildings.geometry.representative_point().intersects(aoi)
    footprint = float(buildings.geometry.intersection(aoi).area.sum()) / 1e6
    straddling = int((~buildings.geometry.within(aoi)).sum())
    return int(counted.sum()), footprint, straddling


# --- Indicator assembly -------------------------------------------------------------------------------


def _parse_utc(text):
    return None if not text else datetime.fromisoformat(str(text)).astimezone(timezone.utc)


def _source(descriptor, nature):
    return IndicatorSource(descriptor.source_id, descriptor.product, descriptor.version, nature,
                           descriptor.license.attribution, descriptor.fingerprint)


def _landcover_dates(descriptor):
    t = descriptor.temporal
    return IndicatorDates(
        t.start_utc, t.end_utc, f"Images satellitaires de {t.start_utc:%Y} classées par le producteur (carte annuelle).",
        f"{descriptor.product} ({descriptor.version})", None,
        _parse_utc(descriptor.provenance.get("retrieved_at_utc")))


def _osm_dates(descriptor):
    snapshot = descriptor.temporal.end_utc
    return IndicatorDates(
        None, None, "Date de terrain des objets inconnue : seule la date de la base est connue.",
        f"Base OpenStreetMap au {snapshot:%d/%m/%Y %H:%M} UTC", snapshot,
        _parse_utc(descriptor.provenance.get("retrieved_at_utc")))


def _unavailable(indicator_id, label, unit, aoi_id, aoi_sha, profile, reason):
    return TerritorialIndicator(indicator_id, label, None, unit, UNAVAILABLE, aoi_id, aoi_sha, None, None, None,
                                reason, NOT_ASSESSED, (reason,), (), profile.fingerprint)


LANDCOVER_LIMITS = (
    "Carte issue d'un modèle de classification ; exactitude déclarée par le producteur, non vérifiée localement.",
    "Résolution 10 m : les petits objets (jardins, alignements d'arbres) sont absorbés dans la classe dominante.",
    "Nomenclature mondiale non spécifique aux écosystèmes côtiers d'Afrique centrale.",
    "Décrit l'année de la carte, pas l'état actuel du territoire.",
)
OSM_LIMITS = (
    "Base collaborative : complétude inconnue ; absence d'objet ≠ absence sur le terrain.",
    "Un objet présent dans la base peut avoir été cartographié longtemps après sa construction.",
)


def _landcover_indicators(tiles, aoi_path, aoi_id, aoi_sha, profile, warnings):
    """Land, vegetation, built-up and water indicators from one WorldCover product."""
    from src.data.worldcover import CLASSES

    descriptor = tiles[0][1]
    areas, classified_m2, aoi_m2 = landcover_areas(tiles, aoi_path)
    coverage = 100.0 * classified_m2 / aoi_m2
    labels = dict(descriptor.classes or CLASSES)
    reliability, reasons = MEDIUM, ["Exactitude déclarée par le producteur, non vérifiée sur la zone d'étude.",
                                    f"Référentiel {descriptor.temporal.start_utc:%Y}, antérieur aux observations récentes."]
    if coverage < profile.min_coverage_percent:
        reliability = LOW
        reasons.append(f"Couverture {coverage:.1f} % < {profile.min_coverage_percent:g} % de la zone d'étude.")
        warnings.append(f"WorldCover ne couvre que {coverage:.1f} % de la zone d'étude : surfaces sous-estimées.")
    common = dict(aoi_id=aoi_id, aoi_sha256=aoi_sha, dates=_landcover_dates(descriptor),
                  source=_source(descriptor, CLASSIFICATION), coverage_percent=coverage, reliability=reliability,
                  reliability_reasons=tuple(reasons), limits=LANDCOVER_LIMITS,
                  profile_fingerprint=profile.fingerprint)
    km2 = lambda codes: sum(areas.get(code, 0.0) for code in codes) / 1e6
    detail = lambda codes: tuple((labels.get(code, str(code)), areas[code] / 1e6) for code in codes if code in areas)
    water = km2(profile.water_classes)
    land = classified_m2 / 1e6 - water
    aoi_km2 = aoi_m2 / 1e6
    shares = lambda value: (("zone d'étude", 100.0 * value / aoi_km2),) + (
        (("surface terrestre", 100.0 * value / land),) if land > 0 else ())
    weighting = ("Somme des cellules classées, pondérées par leur intersection exacte avec la zone d'étude "
                 "(aire équivalente EPSG:6933), sur la grille native du produit ; aucun masque nuageux Sentinel-2.")
    vegetation = km2(profile.vegetation_classes)
    groups = tuple(BreakdownItem(label, label, km2(codes), "km²", 100.0 * km2(codes) / aoi_km2, detail(codes))
                   for label, codes in profile.vegetation_groups)
    builtup = km2(profile.builtup_classes)
    return [
        TerritorialIndicator(
            "landcover.land_km2", "Surface terrestre", land, "km²", AVAILABLE, method=(
                f"Surface classée hors eaux permanentes (classes {', '.join(map(str, profile.water_classes))}). "
                + weighting), shares=(("zone d'étude", 100.0 * land / aoi_km2),),
            breakdown=(BreakdownItem("water", "Eaux permanentes", water, "km²", 100.0 * water / aoi_km2,
                                     detail(profile.water_classes)),), **common),
        TerritorialIndicator(
            "landcover.vegetation_km2", "Surface végétalisée", vegetation, "km²", AVAILABLE, method=(
                "Classes " + " ; ".join(f"{label} : {', '.join(map(str, codes))}"
                                        for label, codes in profile.vegetation_groups) + ". " + weighting),
            shares=shares(vegetation), breakdown=groups, **common),
        TerritorialIndicator(
            "landcover.builtup_km2", "Surface bâtie (classe « Bâti »)", builtup, "km²", AVAILABLE, method=(
                f"Classe(s) {', '.join(map(str, profile.builtup_classes))} (bâtiments, routes et surfaces "
                "artificialisées selon la définition du producteur). " + weighting),
            shares=shares(builtup), breakdown=(BreakdownItem("builtup", "Bâti", builtup, "km²",
                                                             100.0 * builtup / aoi_km2,
                                                             detail(profile.builtup_classes)),), **common),
    ], land


def _osm_indicators(frame, descriptor, aoi_path, aoi_id, aoi_sha, aoi_km2, land_km2, land_indicator, profile):
    total_km, categories, excluded_km, area_highways = road_lengths(frame, aoi_path, profile)
    count, footprint, straddling = building_figures(frame, aoi_path)
    reasons = ("Complétude OpenStreetMap non évaluée sur la zone d'étude : valeur minimale cartographiée.",)
    common = dict(aoi_id=aoi_id, aoi_sha256=aoi_sha, dates=_osm_dates(descriptor),
                  source=_source(descriptor, COLLABORATIVE), coverage_percent=None, reliability=NOT_ASSESSED,
                  reliability_reasons=reasons, profile_fingerprint=profile.fingerprint)
    road_limits = OSM_LIMITS + (
        "Longueur de tronçons cartographiés, pas longueur de réseau : une voie à chaussées séparées "
        "cartographiées par sens compte deux fois.",
        "Aucune information sur l'état, le revêtement ou la praticabilité des voies.",
    )
    if excluded_km:
        road_limits += (f"{excluded_km:.1f} km exclus (voies projetées, en construction ou abandonnées).",)
    if area_highways:
        road_limits += (f"{area_highways} objet(s) « highway » surfacique(s) non comptés en longueur.",)
    indicators = [
        TerritorialIndicator(
            "osm.roads_km", "Voies cartographiées (OSM)", total_km, "km", AVAILABLE, method=(
                "Somme des longueurs des chemins OSM « highway » découpés à la zone d'étude, en projection UTM "
                "locale ; regroupement des valeurs « highway » selon le profil d'indicateurs."),
            limits=road_limits, breakdown=tuple(
                BreakdownItem(label, label, km, "km", 100.0 * km / total_km if total_km else None, detail)
                for label, km, detail in categories), **common),
        TerritorialIndicator(
            "osm.buildings_count", "Bâtiments cartographiés (OSM)", float(count), "bâtiments", AVAILABLE, method=(
                "Nombre de bâtiments OSM (chemins et multipolygones valides) dont le point représentatif est dans "
                "la zone d'étude."), limits=OSM_LIMITS + (
                "Ce n'est pas le nombre total réel de bâtiments.",
                f"{straddling} bâtiment(s) à cheval sur la limite, attribué(s) selon leur point représentatif."),
            **common),
        TerritorialIndicator(
            "osm.buildings_footprint_km2", "Emprise au sol des bâtiments cartographiés (OSM)", footprint, "km²",
            AVAILABLE, method="Surface des polygones de bâtiments OSM découpés à la zone d'étude (UTM locale).",
            shares=(("zone d'étude", 100.0 * footprint / aoi_km2),), limits=OSM_LIMITS + (
                "Emprise des toits tracés, différente de la classe « Bâti » de WorldCover (routes, sols scellés).",),
            **common),
        TerritorialIndicator(
            "osm.buildings_density_aoi", "Densité de bâtiments cartographiés (zone d'étude)", count / aoi_km2,
            "bâtiments/km²", AVAILABLE, method="Nombre de bâtiments OSM / surface totale de la zone d'étude.",
            limits=OSM_LIMITS + ("Dénominateur incluant les surfaces en eau.",), **common),
    ]
    if land_km2 is None:
        indicators.append(_unavailable(
            "osm.buildings_density_land", "Densité de bâtiments cartographiés (surface terrestre)",
            "bâtiments/km²", aoi_id, aoi_sha, profile,
            "Surface terrestre indisponible (ESA WorldCover absent localement)."))
    else:
        indicators.append(TerritorialIndicator(
            "osm.buildings_density_land", "Densité de bâtiments cartographiés (surface terrestre)",
            count / land_km2 if land_km2 > 0 else None, "bâtiments/km²", AVAILABLE, method=(
                "Nombre de bâtiments OSM / surface terrestre WorldCover (zone d'étude hors eaux permanentes)."),
            limits=OSM_LIMITS + (
                f"Combine deux référentiels de dates différentes : OSM {descriptor.temporal.end_utc:%Y} et "
                f"surface terrestre {land_indicator.dates.observation_start_utc:%Y}.",), **common))
    return indicators


def build_indicators(*, aoi_id: str, aoi_path, worldcover=None, osm=None, aoi_label: str | None = None,
                     profile: IndicatorProfile = AFRICA_DEFAULT_V1, temporal_notes=()) -> TerritorialIndicators:
    """``worldcover``: [(path, descriptor)] of one product version; ``osm``: (GeoDataFrame, descriptor)."""
    from src.geo.change_analysis import load_equal_area_aoi

    aoi_path = Path(aoi_path)
    aoi_sha = hashlib.sha256(aoi_path.read_bytes()).hexdigest()
    aoi_km2 = load_equal_area_aoi(aoi_path).area_m2 / 1e6
    indicators = [TerritorialIndicator(
        "aoi.area_km2", "Surface de la zone d'étude", aoi_km2, "km²", AVAILABLE, aoi_id, aoi_sha,
        None, IndicatorSource("geonaf-aoi", "Zone d'étude GEONAF", aoi_sha[:12], AOI_NATURE, "GEONAF", aoi_sha),
        100.0, "Aire géodésique de la géométrie de la zone d'étude (aire équivalente EPSG:6933).", HIGH,
        ("Géométrie exacte du fichier de zone d'étude.",),
        ("Zone d'étude (carré d'acquisition), pas une limite administrative.",), profile.fingerprint)]
    missing, warnings = [], []
    land_km2 = land_indicator = None
    tiles = list(worldcover or [])
    if tiles:
        if any(d.source_id != profile.landcover_source for _, d in tiles):
            raise ValueError("Source d'occupation du sol incompatible avec le profil d'indicateurs.")
        if len({(d.source_id, d.version) for _, d in tiles}) != 1:
            raise ValueError("Tuiles d'occupation du sol de versions différentes : calcul refusé.")
        landcover, land_km2 = _landcover_indicators(tiles, aoi_path, aoi_id, aoi_sha, profile, warnings)
        land_indicator = landcover[0]
        indicators += landcover
    else:
        missing.append("ESA WorldCover")
        for indicator_id, label in (("landcover.land_km2", "Surface terrestre"),
                                    ("landcover.vegetation_km2", "Surface végétalisée"),
                                    ("landcover.builtup_km2", "Surface bâtie (classe « Bâti »)")):
            indicators.append(_unavailable(indicator_id, label, "km²", aoi_id, aoi_sha, profile,
                                           "ESA WorldCover absent localement pour cette zone d'étude."))
    if osm is not None:
        frame, descriptor = osm
        indicators += _osm_indicators(frame, descriptor, aoi_path, aoi_id, aoi_sha, aoi_km2, land_km2,
                                      land_indicator, profile)
    else:
        missing.append("OpenStreetMap")
        for indicator_id, label, unit in (
                ("osm.roads_km", "Voies cartographiées (OSM)", "km"),
                ("osm.buildings_count", "Bâtiments cartographiés (OSM)", "bâtiments"),
                ("osm.buildings_footprint_km2", "Emprise au sol des bâtiments cartographiés (OSM)", "km²"),
                ("osm.buildings_density_aoi", "Densité de bâtiments cartographiés (zone d'étude)", "bâtiments/km²"),
                ("osm.buildings_density_land", "Densité de bâtiments cartographiés (surface terrestre)",
                 "bâtiments/km²")):
            indicators.append(_unavailable(indicator_id, label, unit, aoi_id, aoi_sha, profile,
                                           "Instantané OpenStreetMap absent localement pour cette zone d'étude."))
    return TerritorialIndicators(aoi_id, aoi_label or aoi_id, aoi_sha, aoi_km2, profile, tuple(indicators),
                                 tuple(temporal_notes), tuple(missing), tuple(warnings))


# --- Temporal comparability ---------------------------------------------------------------------------


def _reference_instant(indicator):
    dates = indicator.dates
    return dates.reference_at_utc or dates.observation_end_utc


def assess_change(before: TerritorialIndicator, after: TerritorialIndicator) -> ChangeAssessment:
    """Difference ``after - before`` only when both values are strictly comparable; never inferred."""
    reasons = []
    if before.indicator_id != after.indicator_id or before.unit != after.unit:
        reasons.append("Indicateurs ou unités différents.")
    if UNAVAILABLE in (before.status, after.status) or None in (before.value, after.value):
        reasons.append("Valeur indisponible à l'une des dates.")
    if before.aoi_sha256 != after.aoi_sha256:
        reasons.append("Zones d'étude différentes.")
    if before.profile_fingerprint != after.profile_fingerprint:
        reasons.append("Profils d'indicateurs (regroupements) différents.")
    if before.source is None or after.source is None or before.dates is None or after.dates is None:
        reasons.append("Source ou dates non documentées.")
    else:
        if before.source.source_id != after.source.source_id:
            reasons.append("Sources différentes.")
        elif before.source.nature == CLASSIFICATION and before.source.version != after.source.version:
            reasons.append(f"Versions {before.source.version} et {after.source.version} produites par des "
                           "algorithmes différents : l'écart n'est pas un changement du territoire.")
        start, end = _reference_instant(before), _reference_instant(after)
        if before.source.fingerprint == after.source.fingerprint or start == end:
            reasons.append("Même référentiel aux deux dates : aucune évolution mesurable.")
        elif start is None or end is None or end < start:
            reasons.append("Ordre chronologique des référentiels non établi.")
    for item in (before, after):
        if item.coverage_percent is not None and item.coverage_percent < 99.0:
            reasons.append(f"Couverture insuffisante ({item.coverage_percent:.1f} %).")
    if reasons:
        return ChangeAssessment(after.indicator_id, False, "non comparable", None, after.unit, tuple(reasons))
    collaborative = after.source.nature == COLLABORATIVE
    kind = ("évolution de la cartographie OpenStreetMap (ni nouvelles constructions ni nouvelles voies)"
            if collaborative else "évolution entre deux référentiels comparables")
    notes = (("Un objet ajouté à la base peut exister sur le terrain depuis longtemps.",) if collaborative else ())
    return ChangeAssessment(after.indicator_id, True, kind, after.value - before.value, after.unit, notes)


# --- Offline inventory of stored layers ----------------------------------------------------------------


@dataclass(frozen=True)
class IndicatorInputs:
    data_dir: Path
    aoi_id: str
    aoi_path: Path
    bounds: tuple[float, float, float, float]
    worldcover: tuple      # StoredLayer of one version, closest validity first.
    osm: object | None     # Latest StoredLayer for the AOI bounds.
    temporal_notes: tuple[str, ...]
    key: tuple             # Cache key: AOI digest, layer fingerprints, profile fingerprint.


def indicator_inputs(data_dir, aoi_id: str, profile: IndicatorProfile = AFRICA_DEFAULT_V1) -> IndicatorInputs:
    """Stored layers for the study zone (verified digests) and the temporal availability per source."""
    import geopandas as gpd

    from src.services.external_layers import latest_osm, open_store, stored_layers

    aoi_path = (Path(data_dir) / "aoi" / f"{aoi_id}.geojson").resolve()
    bounds = tuple(float(v) for v in gpd.read_file(aoi_path).to_crs("EPSG:4326").total_bounds)
    _, repository = open_store(Path(data_dir))
    by_key = {}
    for layer in stored_layers(repository, profile.landcover_source, bounds):
        if layer.descriptor.provenance.get("aoi_id") == aoi_id:   # Adjacent zones overlap.
            by_key.setdefault(layer.storage_key, layer)
    worldcover = list(by_key.values())
    versions = sorted({layer.descriptor.version for layer in worldcover})
    if worldcover:
        latest = max(versions, key=lambda v: max(l.descriptor.temporal.end_utc for l in worldcover
                                                  if l.descriptor.version == v))
        worldcover = [layer for layer in worldcover if layer.descriptor.version == latest]
    osm = latest_osm(repository, bounds)
    snapshots = {layer.descriptor.temporal.end_utc for layer in stored_layers(repository, "openstreetmap", bounds)
                 if all(abs(a - b) < 1e-6 for a, b in zip(layer.descriptor.extent.wgs84_bounds, bounds))}
    notes = []
    if len(versions) < 2:
        notes.append("Occupation du sol : une seule version WorldCover disponible localement "
                     f"({', '.join(versions) or 'aucune'}) ; aucune évolution calculée. Les versions v100 (2020) "
                     "et v200 (2021) reposent sur des algorithmes différents et ne mesurent pas un changement.")
    else:
        notes.append(f"Occupation du sol : versions {', '.join(versions)} présentes mais non comparables "
                     "(algorithmes différents) ; aucune évolution calculée.")
    notes.append(f"OpenStreetMap : {len(snapshots)} instantané(s) local(aux) ; "
                 + ("aucune évolution calculée." if len(snapshots) < 2 else
                    "une différence entre instantanés décrirait l'évolution de la cartographie, pas des "
                    "constructions ; non affichée dans cette version."))
    key = (hashlib.sha256(aoi_path.read_bytes()).hexdigest(),
           tuple(layer.descriptor.fingerprint for layer in worldcover),
           osm.descriptor.fingerprint if osm else None, profile.fingerprint)
    return IndicatorInputs(Path(data_dir), aoi_id, aoi_path, bounds, tuple(worldcover), osm, tuple(notes), key)


def compute_territorial_indicators(inputs: IndicatorInputs, *, aoi_label: str | None = None,
                                   profile: IndicatorProfile = AFRICA_DEFAULT_V1) -> TerritorialIndicators:
    from src.data.openstreetmap import load_osm_snapshot
    from src.services.external_layers import open_store

    osm = None
    if inputs.osm is not None:
        store, _ = open_store(inputs.data_dir)
        osm = load_osm_snapshot(store, inputs.osm.storage_key, inputs.bounds)
    return build_indicators(aoi_id=inputs.aoi_id, aoi_path=inputs.aoi_path,
                            worldcover=[(layer.path, layer.descriptor) for layer in inputs.worldcover] or None,
                            osm=osm, aoi_label=aoi_label, profile=profile, temporal_notes=inputs.temporal_notes)
