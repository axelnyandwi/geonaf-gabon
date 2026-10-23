"""Sentinel-1 V1: CDSE monthly IW backscatter mosaics (catalogue, validation, bounded window).

Product choice (studied 22/10/2026 on the CDSE STAC): ``sentinel-1-global-mosaics``,
type ``S1SAR_L3_IW_MCM``. Each item is one calendar month on one MGRS tile
(≈100 km, UTM CRS), 20 m, VV and VH as float32 COGs (nodata −32768). The
CDSE algorithm calibrates to β⁰, removes thermal noise, applies radiometric
terrain correction (area integration, Copernicus DEM 30 m outside Europe) and
Range-Doppler terrain correction, excludes radar shadow, then builds a
weighted monthly composite of both orbit directions. It is therefore already
calibrated and terrain corrected, unlike GRD (≈1 GB per scene, slant geometry
corrections still required).

Radiometry (decision of 22/10/2026, see ``RADIOMETRY`` and docs/sentinel1_radiometry.md):
pixels are terrain-flattened gamma nought (γ⁰_T) in linear power units, so
dB = 10·log10(value). Evidence comes from the producer (Sinergise) on CDSE,
not from value plausibility. The STAC asset titles still say "Amplitude": this
inconsistency is recorded and shown. Values are a monthly composite: there is
no single acquisition instant, incidence angle or orbit direction per pixel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import math

import rasterio

from src.data.external_sources import (
    ExternalLayerDescriptor, LayerLicense, LayerQuality, SpatialExtent, TemporalValidity,
)

COLLECTION = "sentinel-1-global-mosaics"
PRODUCT_TYPE = "S1SAR_L3_IW_MCM"
POLARIZATIONS = ("VV", "VH")
KEY_PREFIX = "Global-Mosaics/Sentinel-1/S1SAR_L3_IW_MCM/"
NODATA = -32768.0
MAX_SEARCH_MONTHS = 24
LICENSE_URL = "https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice"
SCALE_CONVENTIONS = {"amplitude": 20.0, "intensity": 10.0}
RADIOMETRY = {
    "version": "s1-mosaic-radiometry-1",
    "quantity": "gamma0_terrain_flattened",
    "scale": "linear_power",
    "convention": "intensity",          # Key of SCALE_CONVENTIONS used for dB.
    "db_formula": "10·log10(γ⁰_T)",
    "nodata": "−32768 (STAC) ; NaN pour l'ombre radar persistante",
    "scale_factor": "1, offset 0, sans unité dans les balises GeoTIFF (vérifié sur 2025-01 32NNF)",
    "status": "documentée par le producteur ; non vérifiée par calibration indépendante",
    "evidence": (
        "Annonce CDSE (Sinergise, 06/08/2024) : « terrain-flattened gamma nought backscatter », "
        "échelle linéaire, intensités — https://dataspace.copernicus.eu/node/1621",
        "Forum CDSE (G. Milčinski, Sinergise, 28/05/2025) : « Values are in linear units », conversion "
        "dB par 10·log10 — https://forum.dataspace.copernicus.eu/t/sentinel-1-monthly-mosaics-linear-or-db/3757",
        "Algorithme CDSE : calibration β⁰, RTC par intégration d'aires, Range-Doppler, ombres exclues — "
        "https://documentation.dataspace.copernicus.eu/Data/Others/Sentinel1_Mosaic_Algorithm.html",
    ),
    "inconsistency": "Les titres des actifs STAC indiquent « Amplitude » ; contredit par le producteur, "
                     "non retenu. Les valeurs ne sont pas en dB (minimum strictement positif observé).",
}


@dataclass(frozen=True)
class Sentinel1Asset:
    polarization: str
    s3_key: str
    size_bytes: int
    nodata: float


@dataclass(frozen=True)
class Sentinel1Mosaic:
    item_id: str
    start_utc: datetime
    end_utc: datetime
    grid_code: str
    crs: str
    gsd_m: float
    instrument_mode: str
    assets: dict
    processing_facility: str
    published_utc: str
    wgs84_bbox: tuple[float, float, float, float]

    @property
    def month(self) -> str:
        return self.start_utc.strftime("%Y-%m")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def search_body(wgs84_bounds, start: date, end: date, *, limit: int = 30) -> dict:
    left, bottom, right, top = (float(v) for v in wgs84_bounds)
    if not (-180 <= left < right <= 180 and -90 <= bottom < top <= 90) or right - left > 1 or top - bottom > 1:
        raise ValueError("Emprise Sentinel-1 invalide ou trop grande (≤ 1°).")
    if start > end or (end.year - start.year) * 12 + end.month - start.month >= MAX_SEARCH_MONTHS:
        raise ValueError(f"Période invalide ou supérieure à {MAX_SEARCH_MONTHS} mois.")
    return {"collections": [COLLECTION], "bbox": [left, bottom, right, top], "limit": int(limit),
            "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
            "sortby": [{"field": "properties.datetime", "direction": "desc"}]}


def parse_mosaic_item(item: dict) -> Sentinel1Mosaic:
    """Validate one STAC item against the expected product; reject anything else."""
    properties = item.get("properties") or {}
    if item.get("collection") != COLLECTION or properties.get("product:type") != PRODUCT_TYPE:
        raise ValueError("Item Sentinel-1 hors du produit mosaïque IW attendu.")
    if properties.get("sar:instrument_mode") != "IW" or not set(POLARIZATIONS) <= set(
            properties.get("sar:polarizations") or ()):
        raise ValueError("Mosaïque sans mode IW ou sans VV/VH.")
    assets, crs = {}, None
    for polarization in POLARIZATIONS:
        asset = (item.get("assets") or {}).get(polarization) or {}
        href, size = asset.get("href", ""), asset.get("file:size")
        if (not href.startswith("s3://eodata/" + KEY_PREFIX) or ".." in href
                or isinstance(size, bool) or not isinstance(size, int) or size <= 0):
            raise ValueError("Actif Sentinel-1 invalide.")
        if "cloud-optimized" not in asset.get("type", ""):
            raise ValueError("Actif Sentinel-1 non COG.")
        crs = asset.get("proj:code") or crs
        assets[polarization] = Sentinel1Asset(polarization, href.removeprefix("s3://eodata/"), size,
                                              float(asset.get("nodata", NODATA)))
    if not crs:
        raise ValueError("CRS de la mosaïque absent.")
    gsd = properties.get("gsd")
    if isinstance(gsd, bool) or not isinstance(gsd, (int, float)) or not math.isfinite(gsd) or gsd <= 0:
        raise ValueError("Résolution de la mosaïque absente.")
    return Sentinel1Mosaic(
        item_id=str(item["id"]), start_utc=_instant(properties["start_datetime"]),
        end_utc=_instant(properties["end_datetime"]), grid_code=str(properties.get("grid:code", "")),
        crs=crs, gsd_m=float(gsd), instrument_mode="IW", assets=assets,
        processing_facility=str(properties.get("processing:facility", "")),
        published_utc=str(properties.get("published", "")), wgs84_bbox=tuple(item["bbox"]))


def search_mosaics(stac_io, wgs84_bounds, start: date, end: date):
    """(mosaics, rejected_count, truncated) through ``SecureStacIO`` (POST /search)."""
    import json

    body = search_body(wgs84_bounds, start, end)
    payload = json.loads(stac_io.request(f"{stac_io.settings.stac_endpoint}/search", "POST", parameters=body))
    mosaics, rejected = [], 0
    for item in payload.get("features", []):
        try:
            mosaics.append(parse_mosaic_item(item))
        except (KeyError, TypeError, ValueError):
            rejected += 1
    truncated = any(link.get("rel") == "next" for link in payload.get("links", []))
    return tuple(sorted(mosaics, key=lambda m: m.start_utc)), rejected, truncated


def license_for(year: int) -> LayerLicense:
    return LayerLicense("LicenseRef-Copernicus-Sentinel-Data",
                        f"Contains modified Copernicus Sentinel data [{year}] — Sentinel-1 Global Mosaics, "
                        "Copernicus Data Space Ecosystem", LICENSE_URL, share_alike=False)


def describe_window(path, mosaic: Sentinel1Mosaic, polarization: str, *, retrieved_at: datetime,
                    extra_provenance: dict | None = None) -> ExternalLayerDescriptor:
    from rasterio.warp import transform_bounds
    from src.data.layer_store import sha256_file

    with rasterio.open(path) as source:
        if source.count != 1 or source.dtypes[0] != "float32" or source.crs is None:
            raise ValueError("Fenêtre Sentinel-1 attendue : une bande float32 avec CRS.")
        crs, bounds = source.crs.to_string(), tuple(source.bounds)
        wgs84 = transform_bounds(source.crs, "EPSG:4326", *bounds)
    provenance = {"stac_collection": COLLECTION, "stac_item": mosaic.item_id, "polarization": polarization,
                  "s3_key": mosaic.assets[polarization].s3_key, "grid_code": mosaic.grid_code,
                  "processing_facility": mosaic.processing_facility, "published_utc": mosaic.published_utc,
                  "retrieved_at_utc": retrieved_at.astimezone(timezone.utc).isoformat()}
    provenance.update({key: str(value) for key, value in (extra_provenance or {}).items()})
    return ExternalLayerDescriptor(
        source_id="sentinel-1-mosaic", product=f"Sentinel-1 IW mosaïque mensuelle {polarization}",
        version=mosaic.item_id, kind="continuous_raster",
        temporal=TemporalValidity("interval", mosaic.start_utc, mosaic.end_utc), resolution_m=mosaic.gsd_m,
        crs=crs, extent=SpatialExtent(crs, bounds, tuple(wgs84)), license=license_for(mosaic.start_utc.year),
        provenance=provenance,
        quality=LayerQuality("declared", (
            "Rétrodiffusion calibrée β⁰ puis corrigée du relief (RTC, Copernicus DEM 30 m) par le producteur.",
            "Composite mensuel pondéré des deux orbites : pas d'instant, d'angle d'incidence ni d'orbite uniques.",
            "γ⁰ aplani du relief en puissance linéaire (producteur Sinergise/CDSE) ; dB = 10·log10. "
            "Les titres STAC « Amplitude » sont incohérents avec la documentation du producteur.",
            "Masque qualité : NoData/NaN uniquement (ombres radar exclues par le producteur) ; "
            "repliement (layover) non signalé.",
            "Chatoiement atténué par le composite mais présent ; pas de filtrage GEONAF."),
            sha256_file(Path(path))))


def estimate_window_bytes(s3_client, mosaic: Sentinel1Mosaic, polarization: str, wgs84_bounds, *,
                          header_budget: int = 1024 * 1024):
    """Exact compressed bytes of the tiles covering the AOI, reading only the COG header."""
    from src.data.remote_window import ByteBudget, RangeFile, S3RangeSource, estimate_window

    asset = mosaic.assets[polarization]
    remote = RangeFile(S3RangeSource(s3_client, asset.s3_key), ByteBudget(header_budget), label=asset.s3_key)
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
        with rasterio.open(asset.s3_key, opener=remote.view) as source:
            estimate = estimate_window(source, wgs84_bounds, "EPSG:4326")
    return estimate, remote.received


def window_key(item_id: str, polarization: str, aoi_id: str, aoi_geometry_sha256: str | None = None) -> str:
    """Historical key without fingerprint; ``-<fingerprint[:12]>`` binds a window to one zone geometry."""
    suffix = "" if aoi_geometry_sha256 is None else f"-{aoi_geometry_sha256[:12]}"
    return f"sentinel1/{item_id}_{polarization}_{aoi_id}{suffix}.tif"


def _covers(path, wgs84_bounds) -> bool:
    """The stored window covers ``wgs84_bounds`` within one cell."""
    from rasterio.warp import transform_bounds

    with rasterio.open(path) as source:
        b = transform_bounds(source.crs, "EPSG:4326", *source.bounds, densify_pts=21)
        tolerance = max(source.res) / 111_320.0
    left, bottom, right, top = wgs84_bounds
    return (b[0] <= left + tolerance and b[1] <= bottom + tolerance and b[2] >= right - tolerance
            and b[3] >= top - tolerance)


def reusable_window(store, repository, item_id: str, polarization: str, *, aoi_id: str, wgs84_bounds,
                    aoi_geometry_sha256: str | None = None):
    """(path, descriptor, key) of a recorded window covering the bounds, never one of another geometry."""
    keys = {window_key(item_id, polarization, aoi_id),
            window_key(item_id, polarization, aoi_id, aoi_geometry_sha256)}
    for descriptor, stored_key in repository.find_with_storage("sentinel-1-mosaic", wgs84_bounds):
        recorded = descriptor.provenance.get("aoi_geometry_sha256")
        if aoi_geometry_sha256 is not None and recorded is not None and recorded != aoi_geometry_sha256:
            continue
        if stored_key in keys and store.exists(stored_key) and _covers(store.path(stored_key), wgs84_bounds):
            return store.path(stored_key), descriptor, stored_key
    return None


def fetch_sentinel1_window(s3_client, store, repository, mosaic: Sentinel1Mosaic, polarization: str, *,
                           aoi_id: str, wgs84_bounds, max_bytes: int = 16 * 1024 * 1024,
                           now: datetime | None = None, aoi_geometry_sha256: str | None = None):
    """Return (path, descriptor, report); reuse a stored window covering the zone, else read within ``max_bytes``.

    A stored window is reused only if it covers the bounds and was not recorded
    for another geometry (``aoi_geometry_sha256``, see ``src.geo.aoi_identity``).
    """
    from src.data.remote_window import ByteBudget, RangeFile, S3RangeSource, read_remote_window

    if polarization not in POLARIZATIONS:
        raise ValueError("Polarisation non prise en charge.")
    if not aoi_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Identifiant d'AOI invalide.")
    key = window_key(mosaic.item_id, polarization, aoi_id, aoi_geometry_sha256)
    report = {"storage_key": key, "network_bytes": 0, "reused": True}
    reused = reusable_window(store, repository, mosaic.item_id, polarization, aoi_id=aoi_id,
                             wgs84_bounds=wgs84_bounds, aoi_geometry_sha256=aoi_geometry_sha256)
    if reused is not None:
        return reused[0], reused[1], {**report, "storage_key": reused[2]}
    if store.exists(key):
        raise ValueError("Fenêtre Sentinel-1 présente sans enregistrement de provenance ou ne couvrant pas "
                         "l'AOI : non réutilisée.")
    asset = mosaic.assets[polarization]
    remote = RangeFile(S3RangeSource(s3_client, asset.s3_key), ByteBudget(max_bytes), label=asset.s3_key)
    data, profile, estimate = read_remote_window(remote, wgs84_bounds, "EPSG:4326")
    profile.update(nodata=asset.nodata)
    temporary = store.temporary(key)
    try:
        with rasterio.open(temporary, "w", **profile) as destination:
            destination.write(data, 1)
        path = store.publish(temporary, key)
    finally:
        temporary.unlink(missing_ok=True)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    descriptor = describe_window(path, mosaic, polarization, retrieved_at=now, extra_provenance={
        "aoi_id": aoi_id, **({"aoi_geometry_sha256": aoi_geometry_sha256} if aoi_geometry_sha256 else {}),
        "range_bytes": remote.received, "remote_size_bytes": remote.size,
        "estimated_tile_bytes": estimate.compressed_bytes, "max_bytes": max_bytes,
        "window": f"col={estimate.window.col_off},row={estimate.window.row_off},"
                  f"w={estimate.width},h={estimate.height}"})
    repository.save(descriptor, key)
    report.update(network_bytes=remote.received, reused=False, estimated_tile_bytes=estimate.compressed_bytes,
                  remote_size_bytes=remote.size)
    return path, descriptor, report
