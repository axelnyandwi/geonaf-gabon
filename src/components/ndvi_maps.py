"""Matplotlib presentation of service results, independent of Streamlit."""

import numpy as np
from io import BytesIO
from matplotlib.figure import Figure
from matplotlib.patches import Patch


def ndvi_maps(result):
    """Shared NDVI limits; fixed symmetric delta limits; grey means unavailable."""
    figure = Figure(figsize=(15, 5), layout="constrained")
    grid = result.common_grid
    left, bottom, right, top = grid.bounds
    extent = (left, right, bottom, top)
    panels = (
        (result.comparison.ndvi_date_1, result.earlier.acquired_at_utc.strftime("NDVI · %d/%m/%Y"), "RdYlGn", -1, 1, "NDVI"),
        (result.comparison.ndvi_date_2, result.later.acquired_at_utc.strftime("NDVI · %d/%m/%Y"), "RdYlGn", -1, 1, "NDVI"),
        (result.analysis.delta_ndvi, "ΔNDVI · date 2 − date 1", "RdYlGn", -2, 2, "ΔNDVI"),
    )
    for index, (values, title, palette, low, high, label) in enumerate(panels, 1):
        axis = figure.add_subplot(1, 3, index)
        image = axis.imshow(np.ma.masked_invalid(values), extent=extent,
                            vmin=low, vmax=high, cmap=palette, interpolation="nearest")
        axis.set_facecolor("#d1d5db")
        axis.set_title(title)
        axis.set_xlabel("Est (m)")
        if index == 1:
            axis.set_ylabel("Nord (m)")
        figure.colorbar(image, ax=axis, shrink=0.65, label=label)
    figure.legend(handles=[Patch(facecolor="#d1d5db", label="Sans observation exploitable / non comparable")],
                  loc="outside lower center")
    return figure


def render_ndvi_png(result):
    """Encode a 1500×500 display image, leaving rasters and statistics intact."""
    figure = ndvi_maps(result)
    output = BytesIO()
    figure.savefig(output, format="png", dpi=100)
    figure.clear()
    return output.getvalue()
