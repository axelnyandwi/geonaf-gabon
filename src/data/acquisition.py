"""Validated local Sentinel-2 acquisition metadata for analysis workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import xml.etree.ElementTree as ET

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS

from src.data.radiometric_calibration import read_sentinel2_calibration


ASSET_ROLES = frozenset({"b04", "b08", "scl", "calibration_xml"})


@dataclass(frozen=True)
class SeasonalContext:
    """Externally sourced season label; this type does not classify climate."""

    region: str
    vegetation_type: str
    season_id: str
    source: str
    version: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                self.region,
                self.vegetation_type,
                self.season_id,
                self.source,
                self.version,
            )
        ):
            raise ValueError("Le contexte saisonnier doit identifier région, végétation, saison et source.")


@dataclass(frozen=True)
class RasterAsset:
    """One single-band raster and metadata read from that file."""

    path: Path
    product_id: str
    crs: CRS
    resolution: tuple[float, float]
    nodata: float | int | None
    width: int
    height: int
    transform: Affine


@dataclass(frozen=True)
class BOACalibration:
    """Product-specific B04/B08 calibration required by the NDVI formula."""

    quantification_value: float
    red_offset: float
    nir_offset: float


@dataclass(frozen=True)
class Sentinel2Acquisition:
    """Validated inputs for one Sentinel-2 L2A acquisition.

    ``asset_product_ids`` must come from the STAC item or an ingestion
    manifest. It is explicit provenance; product identity is never guessed
    from a filename. Embedded product identifiers, when supplied by a raster
    or XML file, are checked against it as an additional consistency check.
    """

    product_id: str
    acquired_at: datetime
    b04: RasterAsset
    b08: RasterAsset
    scl: RasterAsset
    calibration_xml: Path
    calibration: BOACalibration
    asset_product_ids: Mapping[str, str]
    quality: Mapping[str, object]
    seasonal_context: SeasonalContext | None = None

    @property
    def crs(self) -> CRS:
        """Canonical acquisition CRS, defined by the aligned B04/B08 grid."""
        return self.b04.crs

    @property
    def resolution(self) -> tuple[float, float]:
        """Canonical acquisition resolution, defined by the B04/B08 grid."""
        return self.b04.resolution


def _normalized_product_id(value: str) -> str:
    return value.removesuffix(".SAFE").strip()


def _embedded_product_ids(path: Path, *, raster: bool) -> tuple[str, ...]:
    """Read product identifiers from content metadata when present."""
    if raster:
        try:
            with rasterio.open(path) as dataset:
                tags = dataset.tags()
        except (OSError, rasterio.errors.RasterioError):
            raise ValueError("Lecture des métadonnées raster impossible.") from None
        values = [
            value
            for key, value in tags.items()
            if key.casefold() in {"product_id", "product_uri"} and value.strip()
        ]
        return tuple(values)

    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        raise ValueError("Lecture des métadonnées XML impossible.") from None
    return tuple(
        element.text.strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1].casefold() in {"product_id", "product_uri"}
        and element.text
        and element.text.strip()
    )


def _validate_embedded_identity(path: Path, product_id: str, *, raster: bool) -> None:
    for embedded_id in _embedded_product_ids(path, raster=raster):
        if _normalized_product_id(embedded_id) != product_id:
            raise ValueError("L'identifiant produit embarqué ne correspond pas à l'acquisition.")


def _read_raster_asset(path: Path, product_id: str) -> RasterAsset:
    try:
        with rasterio.open(path) as dataset:
            if dataset.crs is None:
                raise ValueError("CRS manquant sur un raster d'acquisition.")
            if dataset.count != 1:
                raise ValueError("Chaque actif raster doit contenir une seule bande.")
            transform = dataset.transform
            resolution = (abs(float(transform.a)), abs(float(transform.e)))
            if not all(np.isfinite(value) and value > 0 for value in resolution):
                raise ValueError("Résolution raster invalide.")
            metadata = RasterAsset(
                path=path,
                product_id=product_id,
                crs=dataset.crs,
                resolution=resolution,
                nodata=dataset.nodata,
                width=dataset.width,
                height=dataset.height,
                transform=transform,
            )
    except (OSError, rasterio.errors.RasterioError):
        raise ValueError("Lecture des métadonnées raster impossible.") from None
    _validate_embedded_identity(path, product_id, raster=True)
    return metadata


def create_sentinel2_acquisition(
    *,
    product_id: str,
    acquired_at: datetime,
    b04_path: str | Path,
    b08_path: str | Path,
    scl_path: str | Path,
    calibration_xml_path: str | Path,
    asset_product_ids: Mapping[str, str],
    quality: Mapping[str, object] | None = None,
    seasonal_context: SeasonalContext | None = None,
) -> Sentinel2Acquisition:
    """Validate explicit asset provenance, raster grids, and BOA metadata.

    ``asset_product_ids`` is keyed by ``b04``, ``b08``, ``scl``, and
    ``calibration_xml``. These identifiers must originate in acquisition
    metadata (for example STAC/ingestion output), not be inferred from paths.
    SCL may have a different resolution or CRS because the temporal
    harmonizer aligns categorical masks to the B04/B08 grid.
    """
    if not isinstance(product_id, str) or not product_id.strip():
        raise ValueError("Identifiant produit absent.")
    product_id = product_id.strip()
    if not isinstance(acquired_at, datetime) or acquired_at.tzinfo is None or acquired_at.utcoffset() is None:
        raise ValueError("La date d'acquisition doit inclure un fuseau horaire.")
    if not isinstance(asset_product_ids, Mapping) or set(asset_product_ids) != ASSET_ROLES:
        raise ValueError("La provenance explicite doit couvrir B04, B08, SCL et XML.")
    if any(
        not isinstance(value, str)
        or _normalized_product_id(value) != product_id
        for value in asset_product_ids.values()
    ):
        raise ValueError("Les actifs ne sont pas tous associés au même produit.")
    if seasonal_context is not None and not isinstance(seasonal_context, SeasonalContext):
        raise TypeError("seasonal_context doit être un SeasonalContext explicite.")

    paths = {
        "b04": Path(b04_path),
        "b08": Path(b08_path),
        "scl": Path(scl_path),
        "calibration_xml": Path(calibration_xml_path),
    }
    if any(not path.is_file() for path in paths.values()):
        raise ValueError("Acquisition incomplète : un ou plusieurs actifs sont absents.")

    b04 = _read_raster_asset(paths["b04"], product_id)
    b08 = _read_raster_asset(paths["b08"], product_id)
    scl = _read_raster_asset(paths["scl"], product_id)
    if (
        b04.crs != b08.crs
        or b04.transform != b08.transform
        or (b04.width, b04.height) != (b08.width, b08.height)
    ):
        raise ValueError("B04 et B08 ne partagent pas la même grille.")

    _validate_embedded_identity(paths["calibration_xml"], product_id, raster=False)
    try:
        calibration_values = read_sentinel2_calibration(paths["calibration_xml"])
        calibration = BOACalibration(
            quantification_value=float(calibration_values["quantification_value"]),
            red_offset=float(calibration_values["offsets"]["B04"]),
            nir_offset=float(calibration_values["offsets"]["B08"]),
        )
    except (KeyError, TypeError, ValueError, FileNotFoundError, OverflowError):
        raise ValueError("Calibration BOA B04/B08 absente ou invalide.") from None
    if (
        not np.isfinite(calibration.quantification_value)
        or calibration.quantification_value <= 0
        or not np.isfinite(calibration.red_offset)
        or not np.isfinite(calibration.nir_offset)
    ):
        raise ValueError("Calibration BOA B04/B08 absente ou invalide.")

    quality_snapshot = MappingProxyType(dict(quality or {}))
    provenance_snapshot = MappingProxyType(
        {role: _normalized_product_id(value) for role, value in asset_product_ids.items()}
    )
    return Sentinel2Acquisition(
        product_id=product_id,
        acquired_at=acquired_at,
        b04=b04,
        b08=b08,
        scl=scl,
        calibration_xml=paths["calibration_xml"],
        calibration=calibration,
        asset_product_ids=provenance_snapshot,
        quality=quality_snapshot,
        seasonal_context=seasonal_context,
    )
