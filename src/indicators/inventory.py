"""Offline inventory of the stored layers usable for a zone (verified digests), and their temporal availability."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.indicators.profiles import AFRICA_DEFAULT_V1, IndicatorProfile


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
    """Stored layers for the GEONAF study zone ``data_dir/aoi/<aoi_id>.geojson``; see :func:`inputs_for_zone`."""
    return inputs_for_zone(data_dir, aoi_id, Path(data_dir) / "aoi" / f"{aoi_id}.geojson", profile)


def inputs_for_zone(data_dir, aoi_id: str, aoi_path, profile: IndicatorProfile = AFRICA_DEFAULT_V1) -> IndicatorInputs:
    """Stored layers for any polygonal zone file (verified digests) and the temporal availability per source.

    The zone is validated strictly (``load_aoi_identity``); land cover windows
    retrieved under another identifier, or recorded for another geometry of the
    same identifier, are never used. OSM snapshots are matched on the zone's
    exact WGS84 bounds. A source without a matching layer shows as missing.
    """
    from src.geo.aoi_identity import load_aoi_identity
    from src.services.external_layers import latest_osm, layers_for_aoi, open_store, stored_layers

    aoi_path = Path(aoi_path).resolve()
    identity = load_aoi_identity(aoi_path, aoi_id)
    bounds = identity.wgs84_bounds
    _, repository = open_store(Path(data_dir))
    worldcover = list(layers_for_aoi(stored_layers(repository, profile.landcover_source, bounds), identity))
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
    key = (identity.file_sha256, identity.geometry_sha256,
           tuple(layer.descriptor.fingerprint for layer in worldcover),
           osm.descriptor.fingerprint if osm else None, profile.fingerprint)
    return IndicatorInputs(Path(data_dir), aoi_id, aoi_path, bounds, tuple(worldcover), osm, tuple(notes), key)
