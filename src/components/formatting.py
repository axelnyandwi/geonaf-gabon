"""French display strings only; never feed these back into scientific calculations."""

from math import isfinite


def _number(value, decimals):
    if value is None or not isfinite(value):
        return "—"
    return f"{value:,.{decimals}f}".replace(",", "\u202f").replace(".", ",")


def format_decimal(value, decimals):
    """French decimal comma for any displayed number (thresholds, dB…)."""
    return _number(value, decimals)


def format_date(instant):
    """French day/month/year for an ISO date string or a date/datetime."""
    from datetime import date, datetime

    if isinstance(instant, str):
        instant = datetime.fromisoformat(instant.replace("Z", "+00:00")[:10]) if len(instant) >= 10 else None
    if isinstance(instant, (date, datetime)):
        return instant.strftime("%d/%m/%Y")
    return "—"


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


def french_table(frame, decimals: dict, na_rep="—"):
    """Styler for ``st.dataframe``: decimal comma and narrow no-break space for thousands on ``decimals``
    columns (name -> digits). Display only: the frame keeps its numbers, so sorting stays numeric."""
    styler = frame.style
    for column, digits in decimals.items():
        if column in frame.columns:
            styler = styler.format(subset=[column], precision=digits, decimal=",", thousands=" ",
                                   na_rep=na_rep)
    return styler


def format_hectares_from_km2(value):
    """Secondary display of a km² surface in hectares (1 km² = 100 ha); whole hectares from 10 ha."""
    if value is None or not isfinite(value):
        return "—"
    hectares = value * 100.0
    return _number(hectares, 0 if abs(hectares) >= 10 else 2)
