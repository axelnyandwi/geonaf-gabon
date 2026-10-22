"""Multisource demonstrator engine (Sentinel-2 × WorldCover × OSM, Sentinel-1 apart); no UI.

Three kinds of information are kept distinct in the result:

* ``Observation mesurée`` — Sentinel-2 NDVI of one acquisition (and Sentinel-1
  backscatter, a different physical quantity shown separately);
* ``Classification externe`` / ``Base collaborative`` — WorldCover classes
  (model output, 2021) and OpenStreetMap objects (contributed, snapshot date);
* ``Interprétation`` — rule-generated sentences, always phrased as hypotheses.

External layers are aligned onto the Sentinel-2 10 m grid (nearest
neighbour for classes, rasterization for vectors); Sentinel-2 values are not
resampled. Every source carries its date or validity, the offset with the
Sentinel-2 acquisition, its resolution and its comparability limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from src.data.external_sources import ExternalLayerDescriptor, assess_temporal_join
from src.geo.zonal import ZoneStatistics, rasterize_categories, zonal_statistics
from src.services.multisource import LayerJoinResult, join_categorical_raster_with_zones
from src.services.ndvi_observation import NDVIObservation
from src.services.sentinel1_layer import RadarLayer

MEASURED, CLASSIFICATION, COLLABORATIVE, INTERPRETATION = (
    "Observation mesurée", "Classification externe", "Base collaborative", "Interprétation")
OSM_TOLERANCE_DAYS = 90.0
WORLDCOVER_TOLERANCE_DAYS = 365.0


@dataclass(frozen=True)
class SourceSummary:
    source: str
    nature: str
    product: str
    temporal_label: str
    offset_days: float | None       # Absolute offset with the S2 acquisition; None for the reference.
    relation: str                   # within / before_layer / after_layer / reference
    resolution: str
    crs: str
    attribution: str
    limits: tuple[str, ...]


@dataclass(frozen=True)
class InfrastructureSummary:
    group: str
    features: int                   # Objects intersecting the AOI.
    area_ha: float | None           # Polygon area clipped to the AOI (UTM).
    length_km: float | None         # Line length clipped to the AOI (UTM).
    ndvi: ZoneStatistics            # NDVI over cells rasterized for this group (non-exclusive groups).


@dataclass(frozen=True)
class MultisourceResult:
    observation: NDVIObservation
    worldcover: LayerJoinResult | None
    worldcover_codes: np.ndarray | None
    worldcover_valid: np.ndarray | None
    osm_descriptor: ExternalLayerDescriptor | None
    osm_frame: object | None        # GeoDataFrame EPSG:4326 limited to objects intersecting the AOI.
    infrastructure: tuple[InfrastructureSummary, ...]
    osm_masks: dict                 # group → bool cells on the S2 grid (pixel centre), reused for display.
    radar: tuple[RadarLayer, ...]
    sources: tuple[SourceSummary, ...]
    interpretations: tuple[str, ...]
    warnings: tuple[str, ...] = field(default=())
    scope: str = ("Démonstrateur descriptif : croisements exploratoires, aucune relation causale ni "
                  "changement environnemental validé sans vérification indépendante.")


def _temporal_label(descriptor):
    t = descriptor.temporal
    if t.kind == "snapshot":
        return f"Instantané base OSM {t.start_utc:%d/%m/%Y %H:%M} UTC"
    if t.kind == "interval":
        return f"Validité {t.start_utc:%d/%m/%Y} → {t.end_utc:%d/%m/%Y}"
    return f"{t.start_utc:%d/%m/%Y %H:%M} UTC"


def _infrastructure(observation, frame, aoi_path):
    import geopandas as gpd

    grid = observation.grid
    aoi = gpd.read_file(aoi_path).to_crs(grid.crs).geometry.union_all()
    projected = frame.to_crs(grid.crs)
    inside = projected[projected.intersects(aoi)]
    clipped = inside.geometry.intersection(aoi)
    output, masks = [], {}
    for group in sorted(inside["group"].unique()):
        mask = (inside["group"] == group).to_numpy()
        shapes = clipped[mask]
        polygons = shapes[shapes.geom_type.isin(["Polygon", "MultiPolygon"])]
        lines = shapes[shapes.geom_type.isin(["LineString", "MultiLineString"])]
        zones = rasterize_categories(inside[mask].assign(category=group), grid, {group: 1})
        masks[group] = zones == 1
        stats = zonal_statistics(observation.ndvi, observation.valid, zones, {1: group},
                                 pixel_area_weights=observation.pixel_area_weights)[0]
        output.append(InfrastructureSummary(
            group, int(mask.sum()), float(polygons.area.sum()) / 1e4 if len(polygons) else None,
            float(lines.length.sum()) / 1e3 if len(lines) else None, stats))
    return tuple(output), frame.loc[inside.index], masks


def _interpretations(worldcover, radar):
    sentences = []
    if worldcover is not None:
        stats = {zone.code: zone for zone in worldcover.zones if zone.pixel_count >= 100}
        if 10 in stats and 50 in stats and stats[10].median is not None and stats[50].median is not None:
            relation = "supérieur" if stats[10].median > stats[50].median else "non supérieur"
            sentences.append(
                f"NDVI médian « Couvert arboré » ({stats[10].median:.2f}) {relation} à celui du « Bâti » "
                f"({stats[50].median:.2f}) : {'cohérent' if relation == 'supérieur' else 'incohérent'} avec la "
                "nomenclature attendue ; ce constat ne valide ni la classification ni le NDVI.")
        if 95 in stats and stats[95].median is not None:
            sentences.append(
                f"Mangroves : NDVI médian {stats[95].median:.2f}. Les marées et l'eau sous couvert peuvent "
                "abaisser le NDVI ; hypothèse non vérifiée sur le terrain.")
    for layer in radar:
        classes = {zone.code: zone for zone in layer.per_class if zone.pixel_count >= 100}
        if 80 in classes and 10 in classes and classes[80].median is not None and classes[10].median is not None:
            sentences.append(
                f"{layer.polarization} : rétrodiffusion médiane plus faible sur « Eaux permanentes » "
                f"({classes[80].median:.1f} dB) que sur « Couvert arboré » ({classes[10].median:.1f} dB), "
                "cohérent avec une réflexion spéculaire de l'eau calme ; le vent ou les vagues peuvent l'augmenter.")
    return tuple(sentences)


def analyze_multisource(observation: NDVIObservation, *, aoi_path: str | Path,
                        worldcover: tuple[Path, ExternalLayerDescriptor] | None = None,
                        osm: tuple[object, ExternalLayerDescriptor] | None = None,
                        radar: tuple[RadarLayer, ...] = ()) -> MultisourceResult:
    observed_at = observation.acquired_at_utc
    sources = [SourceSummary(
        "Sentinel-2 L2A", MEASURED, observation.product_id, f"Acquisition {observed_at:%d/%m/%Y %H:%M} UTC",
        None, "reference", "10 m (B04/B08) ; SCL 20 m", observation.grid.crs.to_string(),
        "Contains modified Copernicus Sentinel data [{}]".format(observed_at.year),
        ("NDVI de réflectance BOA, une seule date ; pixels masqués selon le profil SCL.",
         "Indicateur de verdeur, pas une mesure de biomasse ni d'occupation du sol."))]
    warnings = []
    join = codes = layer_valid = None
    if worldcover is not None:
        path, descriptor = worldcover
        join, codes, layer_valid = join_categorical_raster_with_zones(
            observation.grid, observation.ndvi, observation.valid, observed_at=observed_at, layer_path=path,
            descriptor=descriptor, aoi_path=aoi_path, tolerance_days=WORLDCOVER_TOLERANCE_DAYS,
            pixel_area_weights=observation.pixel_area_weights)
        sources.append(SourceSummary(
            "ESA WorldCover", CLASSIFICATION, descriptor.product, _temporal_label(descriptor),
            join.temporal.offset_days, join.temporal.relation, "10 m (EPSG:4326, 1/12 000°)",
            descriptor.crs, descriptor.license.attribution,
            ("Carte issue d'un modèle de classification (exactitude déclarée, non vérifiée localement).",
             "Alignée au plus proche voisin sur la grille Sentinel-2 : décalage possible d'une demi-cellule.",
             "Nomenclature mondiale non adaptée aux écosystèmes côtiers gabonais.")))
        if not join.temporal.within_tolerance:
            warnings.append(f"WorldCover : {join.temporal.message}")
    frame = None
    infrastructure, masks = (), {}
    osm_descriptor = None
    if osm is not None:
        frame, osm_descriptor = osm
        assessment = assess_temporal_join(osm_descriptor.temporal, observed_at, OSM_TOLERANCE_DAYS)
        infrastructure, frame, masks = _infrastructure(observation, frame, aoi_path)
        sources.append(SourceSummary(
            "OpenStreetMap", COLLABORATIVE, osm_descriptor.product, _temporal_label(osm_descriptor),
            assessment.offset_days, assessment.relation, "Vecteur (rasterisé à 10 m pour les statistiques)",
            "EPSG:4326", osm_descriptor.license.attribution,
            ("Complétude inconnue ; absence d'objet ≠ absence sur le terrain.",
             "Date de création des objets inconnue : un objet peut être postérieur à l'image.",
             "Routes rasterisées en cellules de 10 m : cellules mixtes route/végétation.")))
        if not assessment.within_tolerance:
            warnings.append(f"OpenStreetMap : {assessment.message}")
    for layer in radar:
        assessment = assess_temporal_join(layer.descriptor.temporal, observed_at, 31.0)
        sources.append(SourceSummary(
            f"Sentinel-1 {layer.polarization}", MEASURED, layer.descriptor.product,
            _temporal_label(layer.descriptor), assessment.offset_days, assessment.relation,
            f"{layer.descriptor.resolution_m:g} m (grille native, non ré-échantillonnée)", layer.descriptor.crs,
            layer.descriptor.license.attribution,
            ("Grandeur physique différente du NDVI : aucune comparaison directe.",
             "Composite mensuel pondéré : pas de date d'acquisition unique.",
             f"Unité : {layer.unit} ; échelle non confirmée par le producteur.")))
        if not assessment.within_tolerance:
            warnings.append(f"Sentinel-1 {layer.polarization} : {assessment.message}")
    return MultisourceResult(observation, join, codes, layer_valid, osm_descriptor, frame, infrastructure, masks,
                             tuple(radar), tuple(sources), _interpretations(join, radar), tuple(warnings))
