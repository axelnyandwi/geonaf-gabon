"""Formatting is presentation-only: exact input values remain untouched."""

import numpy as np
import pytest

from src.components.formatting import format_pixels, format_ndvi, format_surface, format_percent


@pytest.mark.parametrize("value,expected", [(1234567, "1\u202f234\u202f567"), (0, "0"), (None, "—")])
def test_pixel_counts(value, expected):
    assert format_pixels(value) == expected


@pytest.mark.parametrize("formatter,value,expected", [
    (format_ndvi, -0.03284583459, "-0,0328"),
    (format_surface, 8271.514976, "8\u202f271,51"),
    (format_percent, 57.40123, "57,40"),
    (format_ndvi, 0, "0,0000"),
])
def test_fixed_decimals(formatter, value, expected):
    assert formatter(value) == expected


@pytest.mark.parametrize("formatter", [format_pixels, format_ndvi, format_surface, format_percent])
@pytest.mark.parametrize("value", [None, np.nan, np.inf])
def test_missing_values(formatter, value):
    assert formatter(value) == "—"


def test_formatting_does_not_change_scientific_values():
    values = np.array([-0.03284583459, 0.123456789], dtype=np.float64)
    original = values.copy()
    for value in values:
        format_ndvi(value)
    np.testing.assert_array_equal(values, original)
