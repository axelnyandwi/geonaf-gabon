"""French display strings only; never feed these back into scientific calculations."""

from math import isfinite


def _number(value, decimals):
    if value is None or not isfinite(value):
        return "—"
    return f"{value:,.{decimals}f}".replace(",", "\u202f").replace(".", ",")


def format_pixels(value):
    if value is None or not isfinite(value):
        return "—"
    return f"{int(value):,}".replace(",", "\u202f")


def format_ndvi(value):
    return _number(value, 4)


def format_surface(value):
    return _number(value, 2)


def format_percent(value):
    return _number(value, 2)


def format_hectares_from_km2(value):
    """Secondary display of a km² surface in hectares (1 km² = 100 ha); whole hectares from 10 ha."""
    if value is None or not isfinite(value):
        return "—"
    hectares = value * 100.0
    return _number(hectares, 0 if abs(hectares) >= 10 else 2)
