"""Proof behind the reduced NDVI cache key (C4b, P2): the NDVI of an acquisition does not depend on
the change threshold nor on the minimum comparable coverage of the profile.

Compared without tolerance: every array (values, NaN positions, dtype, shape), every scalar and
every metadata field of ``NDVIObservation``, except ``scientific_profile`` itself, which the
multisource page rebinds to the effective profile. A change of SCL policy must change the result:
the comparison is able to detect a difference.
"""

from dataclasses import fields, is_dataclass, replace
import math
import os
from pathlib import Path

import numpy as np
import pytest

from src.geo.scientific_profile import ADVANCED_SCL_POLICIES, advanced_configuration, reference_profile
from src.services.local_preview import (
    analyze_local_group, group_local_observations, list_local_observations, local_group_key,
    local_group_ndvi_key,
)
from tests.test_local_preview import local_data  # noqa: F401  (fixture)

PROFILE = "observation.scientific_profile"
PACK = Path(os.environ.get("GEONAF_DEMO_PACK_DIR")
            or Path(__file__).resolve().parents[1] / "data" / "demo_pack" / "geonaf-demo-akanda-2025-01-v3")


def _differences(left, right, path="observation"):
    """List of field paths whose values differ (exact comparison)."""
    if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
        same = (isinstance(left, np.ndarray) and isinstance(right, np.ndarray) and left.dtype == right.dtype
                and left.shape == right.shape
                and np.array_equal(left, right, equal_nan=left.dtype.kind in "fc"))
        return [] if same else [path]
    if is_dataclass(left) and type(left) is type(right):
        return [d for f in fields(left) for d in _differences(getattr(left, f.name), getattr(right, f.name),
                                                               f"{path}.{f.name}")]
    if isinstance(left, (tuple, list)) and type(left) is type(right) and len(left) == len(right):
        return [d for i, (a, b) in enumerate(zip(left, right)) for d in _differences(a, b, f"{path}[{i}]")]
    if isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys():
        return [d for k in left for d in _differences(left[k], right[k], f"{path}[{k!r}]")]
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
        return []
    return [] if left == right else [path]


def _profiles():
    reference = reference_profile("akanda")
    other = advanced_configuration("akanda", change_threshold=0.25, min_comparable_coverage_percent=40.0).effective
    alternatives = [policy for policy in ADVANCED_SCL_POLICIES if policy != reference.scl_policy]
    scl = advanced_configuration("akanda", scl_policy=alternatives[0]).effective if alternatives else None
    assert (other.change_threshold, other.min_comparable_coverage_percent) != (
        reference.change_threshold, reference.min_comparable_coverage_percent)
    assert other.scl_policy == reference.scl_policy and other.fingerprint != reference.fingerprint
    return reference, other, scl


def _check_independence(data_dir):
    reference, other, scl = _profiles()
    group = group_local_observations(list_local_observations(data_dir))[0]
    first = analyze_local_group(group, data_dir, scientific_profile=reference)
    second = analyze_local_group(group, data_dir, scientific_profile=other)
    differences = _differences(first, second)
    assert differences and all(d.startswith(PROFILE) for d in differences)  # Only the profile object differs.
    assert _differences(replace(first, scientific_profile=other), second) == []
    # Same reduced key, different full keys: the cache may share the arrays, never across SCL policies.
    assert local_group_ndvi_key(group, data_dir, reference) == local_group_ndvi_key(group, data_dir, other)
    assert local_group_key(group, data_dir, reference) != local_group_key(group, data_dir, other)
    if scl is not None:
        assert local_group_ndvi_key(group, data_dir, scl) != local_group_ndvi_key(group, data_dir, reference)
        changed = analyze_local_group(group, data_dir, scientific_profile=scl)
        assert [d for d in _differences(first, changed) if not d.startswith(PROFILE)]  # The test sees a change.


def test_ndvi_of_an_acquisition_ignores_threshold_and_coverage_on_synthetic_data(local_data):
    _check_independence(local_data)


@pytest.mark.skipif(not (PACK / "PACK_MANIFEST.json").is_file(), reason="Paquet de démonstration absent.")
def test_ndvi_of_an_acquisition_ignores_threshold_and_coverage_on_pack_v3():
    _check_independence(PACK)


def test_reduced_key_keeps_every_ndvi_dependency(local_data):
    """AOI content, file revisions, SCL policy and seasonal rules all stay in the reduced key."""
    reference, _, _ = _profiles()
    group = group_local_observations(list_local_observations(local_data))[0]
    key = local_group_ndvi_key(group, local_data, reference)
    assert reference.scl_policy in key and reference.aoi_id in key and reference.seasonal_rules in key
    aoi = local_data / "aoi" / f"{reference.aoi_id}.geojson"
    aoi.write_bytes(aoi.read_bytes() + b"\n")                 # Same geometry file, new content digest.
    assert local_group_ndvi_key(group, local_data, reference) != key
