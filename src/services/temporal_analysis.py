"""Application use case for a traceable two-date Sentinel-2 NDVI analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Literal, Mapping, Protocol

import numpy as np
import rasterio

from src.data.acquisition import (
    BOACalibration,
    RasterAsset,
    SeasonalContext,
    Sentinel2Acquisition,
    create_sentinel2_acquisition,
)
from src.geo.change_analysis import (
    DEFAULT_CHANGE_THRESHOLD,
    NDVIChangeAnalysis,
    analyze_ndvi_changes,
)
from src.geo.pair_selection import TemporalPair
from src.geo.temporal import (
    RasterGrid,
    TemporalComparison,
    build_common_grid,
    harmonize_ndvi_pair,
)
from src.geo.vegetation_indices import calculate_ndvi


@dataclass(frozen=True)
class SeasonalComparabilityAssessment:
    """Decision from an injected, region/vegetation-aware seasonal policy.

    ``status`` is descriptive. Even ``not_comparable`` produces a warning;
    it never suppresses the NDVI analysis automatically.
    """

    status: Literal["not_assessed", "comparable", "conditional", "not_comparable"]
    method: str
    rationale: str
    interseasonal: bool | None
    warnings: tuple[str, ...] = ()
    season_relation: Literal["unknown", "different", "same"] = "unknown"


class SeasonalComparabilityEvaluator(Protocol):
    """Seam for a validated policy configured by region and vegetation."""

    def assess(
        self,
        earlier: SeasonalContext | None,
        later: SeasonalContext | None,
    ) -> SeasonalComparabilityAssessment:
        """Assess season metadata without changing the processing decision."""


@dataclass(frozen=True)
class AcquisitionProvenance:
    """Inputs and calibration that produced one date's NDVI."""

    product_id: str
    acquired_at_source: datetime
    acquired_at_utc: datetime
    asset_paths: Mapping[str, str]
    asset_metadata: Mapping[str, RasterAsset]
    asset_product_ids: Mapping[str, str]
    calibration: BOACalibration
    quality: Mapping[str, object]
    seasonal_context: SeasonalContext | None


@dataclass(frozen=True)
class TemporalVegetationChangeResult:
    """Scientific comparison plus enough provenance to reproduce it.

    NDVI change classes describe vegetation-index variation. They do not,
    on their own, establish deforestation or its cause.
    """

    earlier: AcquisitionProvenance
    later: AcquisitionProvenance
    selected_pair: TemporalPair | None
    seasonal_assessment: SeasonalComparabilityAssessment
    scl_policy: str
    threshold: float
    common_grid: RasterGrid
    comparison: TemporalComparison
    analysis: NDVIChangeAnalysis
    aoi_path: str | None
    interpretation_scope: str = (
        "Variation du NDVI ; ne permet pas à elle seule de conclure à une déforestation."
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("La date d'acquisition doit inclure un fuseau horaire.")
    return value.astimezone(timezone.utc)


def _revalidate(acquisition: Sentinel2Acquisition) -> Sentinel2Acquisition:
    """Recheck a contract at use time to catch forged or stale instances."""
    if not isinstance(acquisition, Sentinel2Acquisition):
        raise TypeError("Les entrées doivent être des Sentinel2Acquisition validées.")
    return create_sentinel2_acquisition(
        product_id=acquisition.product_id,
        acquired_at=acquisition.acquired_at,
        b04_path=acquisition.b04.path,
        b08_path=acquisition.b08.path,
        scl_path=acquisition.scl.path,
        calibration_xml_path=acquisition.calibration_xml,
        asset_product_ids=acquisition.asset_product_ids,
        quality=acquisition.quality,
        seasonal_context=acquisition.seasonal_context,
    )


def _validate_selected_pair(
    earlier: Sentinel2Acquisition,
    later: Sentinel2Acquisition,
    selected_pair: TemporalPair | None,
) -> None:
    if earlier.product_id == later.product_id:
        raise ValueError("Deux acquisitions distinctes sont requises.")
    earlier_at = _utc(earlier.acquired_at)
    later_at = _utc(later.acquired_at)
    if earlier_at == later_at:
        raise ValueError("Les acquisitions ne peuvent pas avoir une date identique.")
    if earlier_at > later_at:
        raise ValueError("Les acquisitions doivent être fournies dans l'ordre chronologique.")
    if selected_pair is None:
        return
    if not isinstance(selected_pair, TemporalPair):
        raise TypeError("selected_pair doit provenir du sélecteur temporel.")
    if (
        selected_pair.earlier_product_id != earlier.product_id
        or selected_pair.later_product_id != later.product_id
        or _utc(selected_pair.earlier_datetime) != earlier_at
        or _utc(selected_pair.later_datetime) != later_at
    ):
        raise ValueError("La paire sélectionnée ne correspond pas aux acquisitions fournies.")
    expected_interval = (later_at - earlier_at).total_seconds() / 86400
    if not np.isclose(selected_pair.interval_days, expected_interval, rtol=0, atol=1e-9):
        raise ValueError("L'intervalle de la paire sélectionnée est incohérent.")


def _provenance(acquisition: Sentinel2Acquisition) -> AcquisitionProvenance:
    paths = MappingProxyType(
        {
            "b04": str(acquisition.b04.path),
            "b08": str(acquisition.b08.path),
            "scl": str(acquisition.scl.path),
            "calibration_xml": str(acquisition.calibration_xml),
        }
    )
    return AcquisitionProvenance(
        product_id=acquisition.product_id,
        acquired_at_source=acquisition.acquired_at,
        acquired_at_utc=_utc(acquisition.acquired_at),
        asset_paths=paths,
        asset_metadata=MappingProxyType(
            {
                "b04": acquisition.b04,
                "b08": acquisition.b08,
                "scl": acquisition.scl,
            }
        ),
        asset_product_ids=MappingProxyType(dict(acquisition.asset_product_ids)),
        calibration=acquisition.calibration,
        quality=MappingProxyType(dict(acquisition.quality)),
        seasonal_context=acquisition.seasonal_context,
    )


def _default_seasonal_assessment(
    earlier: SeasonalContext | None,
    later: SeasonalContext | None,
) -> SeasonalComparabilityAssessment:
    if earlier is None or later is None:
        return SeasonalComparabilityAssessment(
            status="not_assessed",
            method="none",
            rationale="Les métadonnées saisonnières des deux acquisitions sont requises.",
            interseasonal=None,
            warnings=("Comparabilité saisonnière non évaluée : contexte incomplet.",),
            season_relation="unknown",
        )

    same_region_and_vegetation = (
        earlier.region == later.region
        and earlier.vegetation_type == later.vegetation_type
    )
    if not same_region_and_vegetation:
        return SeasonalComparabilityAssessment(
            status="not_assessed",
            method="metadata_only",
            rationale="Les contextes régionaux ou les types de végétation diffèrent.",
            interseasonal=None,
            warnings=(
                "Comparabilité saisonnière non évaluée : région ou végétation différente.",
            ),
            season_relation="unknown",
        )

    if earlier.season_id == later.season_id:
        return SeasonalComparabilityAssessment(
            status="not_assessed",
            method="metadata_only",
            rationale="Les deux acquisitions portent le même identifiant de saison.",
            interseasonal=False,
            warnings=(
                "Même saison déclarée : cela ne garantit pas la comparabilité des observations.",
            ),
            season_relation="same",
        )
    return SeasonalComparabilityAssessment(
        status="not_assessed",
        method="metadata_only",
        rationale="Les identifiants de saison fournis sont différents.",
        interseasonal=True,
        warnings=(
            "Comparaison intersaisonnière : une variation saisonnière peut contribuer au delta NDVI.",
        ),
        season_relation="different",
    )


def _assess_seasonality(
    earlier: SeasonalContext | None,
    later: SeasonalContext | None,
    evaluator: SeasonalComparabilityEvaluator | None,
) -> SeasonalComparabilityAssessment:
    baseline = _default_seasonal_assessment(earlier, later)
    if evaluator is None:
        return baseline

    assessment = evaluator.assess(earlier, later)
    if not isinstance(assessment, SeasonalComparabilityAssessment):
        raise TypeError("L'évaluateur saisonnier doit retourner une SeasonalComparabilityAssessment.")
    warnings = list(assessment.warnings)
    if baseline.warnings:
        warning = baseline.warnings[0]
        if warning not in warnings:
            warnings.append(warning)
    return SeasonalComparabilityAssessment(
        status=assessment.status,
        method=assessment.method,
        rationale=assessment.rationale,
        interseasonal=baseline.interseasonal,
        warnings=tuple(warnings),
        season_relation=baseline.season_relation,
    )


def _write_temporary_ndvi(path: Path, values: np.ndarray, profile: dict) -> Path:
    output_profile = profile.copy()
    output_profile.update(
        driver="GTiff",
        dtype="float32",
        count=1,
        nodata=np.nan,
        compress="deflate",
    )
    with rasterio.open(path, "w", **output_profile) as destination:
        destination.write(np.asarray(values, dtype=np.float32), 1)
    return path


def analyze_temporal_vegetation_change(
    earlier: Sentinel2Acquisition,
    later: Sentinel2Acquisition,
    *,
    aoi_path: str | Path | None = None,
    scl_policy: str = "land_surface",
    threshold: float = DEFAULT_CHANGE_THRESHOLD,
    selected_pair: TemporalPair | None = None,
    seasonal_evaluator: SeasonalComparabilityEvaluator | None = None,
) -> TemporalVegetationChangeResult:
    """Calibrate, harmonize, and analyze one chronologically ordered pair.

    Temporary NDVI rasters bridge the existing path-based harmonizer. They
    are created outside the workspace and removed after the comparison; this
    use case does not export deliverables or perform network operations.
    """
    earlier = _revalidate(earlier)
    later = _revalidate(later)
    _validate_selected_pair(earlier, later, selected_pair)
    seasonal_assessment = _assess_seasonality(
        earlier.seasonal_context,
        later.seasonal_context,
        seasonal_evaluator,
    )

    # Fail before reading band pixels when source footprints cannot form a
    # supported comparison grid. The harmonizer receives this same grid.
    common_grid = build_common_grid(earlier.b04.path, later.b04.path)

    with TemporaryDirectory(prefix="geonaf-temporal-analysis-") as scratch:
        scratch_dir = Path(scratch)
        ndvi_earlier, profile_earlier = calculate_ndvi(
            earlier.b04.path,
            earlier.b08.path,
            None,
            earlier.calibration.red_offset,
            earlier.calibration.nir_offset,
            earlier.calibration.quantification_value,
        )
        ndvi_later, profile_later = calculate_ndvi(
            later.b04.path,
            later.b08.path,
            None,
            later.calibration.red_offset,
            later.calibration.nir_offset,
            later.calibration.quantification_value,
        )
        earlier_path = _write_temporary_ndvi(
            scratch_dir / "earlier-ndvi.tif", ndvi_earlier, profile_earlier
        )
        later_path = _write_temporary_ndvi(
            scratch_dir / "later-ndvi.tif", ndvi_later, profile_later
        )
        comparison = harmonize_ndvi_pair(
            earlier_path,
            later_path,
            earlier.scl.path,
            later.scl.path,
            grid=common_grid,
            scl_policy=scl_policy,
        )

    analysis = analyze_ndvi_changes(
        comparison,
        threshold=threshold,
        aoi_path=aoi_path,
    )
    return TemporalVegetationChangeResult(
        earlier=_provenance(earlier),
        later=_provenance(later),
        selected_pair=selected_pair,
        seasonal_assessment=seasonal_assessment,
        scl_policy=scl_policy,
        threshold=analysis.threshold,
        common_grid=comparison.grid,
        comparison=comparison,
        analysis=analysis,
        aoi_path=str(aoi_path) if aoi_path is not None else None,
    )
