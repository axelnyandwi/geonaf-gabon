"""Frozen pre-optimization surface algorithm, used only by tests/benchmarks.

Snapshot taken before the tiled implementation. Keep independent of production
area helpers so numerical and geometry regressions remain detectable.
"""
import numpy as np
from pyproj import Transformer
from shapely import (area as geometry_area, bounds as geometry_bounds, covers,
                     intersection, polygons, prepare as prepare_geometry,
                     relate_pattern, transform as transform_geometry)
from src.geo.temporal import RasterGrid
from src.geo.change_analysis import ChangeClass

EQUAL_AREA_CRS = "EPSG:6933"
MAX_AREA_CELLS_PER_CHUNK = 100_000

def reference_pixel_area_chunks(grid: RasterGrid, aoi_geometry=None):
    """Yield equal-area cell weights in bounded chunks.

    Cells are classified as outside, fully covered, or partial. Fully covered
    cells use their transformed cell area; exact intersections are reserved
    for partial cells. Without an AOI, full cell areas are returned.
    """
    transformer = Transformer.from_crs(
        grid.crs,
        EQUAL_AREA_CRS,
        always_xy=True,
    )
    width = grid.width
    rows_per_chunk = max(1, min(128, MAX_AREA_CELLS_PER_CHUNK // width))
    if aoi_geometry is not None:
        # Prepared GEOS state accelerates repeated predicates against this AOI
        # across all cells and row chunks without changing geometry or areas.
        prepare_geometry(aoi_geometry)

    for row_start in range(0, grid.height, rows_per_chunk):
        row_end = min(row_start + rows_per_chunk, grid.height)
        rows, cols = np.meshgrid(
            np.arange(row_start, row_end, dtype=np.float64),
            np.arange(width, dtype=np.float64),
            indexing="ij",
        )
        corner_coordinates = []
        for col_offset, row_offset in (
            (0, 0),
            (1, 0),
            (1, 1),
            (0, 1),
            (0, 0),
        ):
            pixel_cols = cols + col_offset
            pixel_rows = rows + row_offset
            # Include all six affine terms; the legacy path omitted rotation
            # and shear (b, d), which shifted cell footprints on such grids.
            x = (
                grid.transform.a * pixel_cols
                + grid.transform.b * pixel_rows
                + grid.transform.c
            )
            y = (
                grid.transform.d * pixel_cols
                + grid.transform.e * pixel_rows
                + grid.transform.f
            )
            corner_coordinates.append(np.stack((x, y), axis=-1))

        cell_coordinates = np.stack(corner_coordinates, axis=2).reshape(-1, 5, 2)
        cell_polygons = polygons(cell_coordinates)
        equal_area_cells = transform_geometry(
            cell_polygons,
            transformer.transform,
            interleaved=False,
        )
        if aoi_geometry is None:
            pixel_areas = geometry_area(equal_area_cells)
        else:
            flat_cells = equal_area_cells.reshape(-1)
            flat_areas = np.zeros(flat_cells.shape, dtype=np.float64)
            aoi_min_x, aoi_min_y, aoi_max_x, aoi_max_y = aoi_geometry.bounds
            cell_bounds = geometry_bounds(flat_cells)
            candidate_mask = (
                (cell_bounds[:, 0] <= aoi_max_x)
                & (cell_bounds[:, 2] >= aoi_min_x)
                & (cell_bounds[:, 1] <= aoi_max_y)
                & (cell_bounds[:, 3] >= aoi_min_y)
            )
            candidate_indexes = np.flatnonzero(candidate_mask)
            if candidate_indexes.size:
                candidate_cells = flat_cells[candidate_indexes]
                fully_covered = np.asarray(
                    covers(aoi_geometry, candidate_cells), dtype=bool
                )
                interior_indexes = candidate_indexes[fully_covered]
                if interior_indexes.size:
                    flat_areas[interior_indexes] = geometry_area(
                        flat_cells[interior_indexes]
                    )

                edge_indexes = candidate_indexes[~fully_covered]
                if edge_indexes.size:
                    edge_cells = flat_cells[edge_indexes]
                    positive_area_overlaps = np.asarray(
                        relate_pattern(
                            aoi_geometry, edge_cells, "T********"
                        ),
                        dtype=bool,
                    )
                    partial_indexes = edge_indexes[positive_area_overlaps]
                    if partial_indexes.size:
                        partial_intersections = intersection(
                            flat_cells[partial_indexes], aoi_geometry
                        )
                        flat_areas[partial_indexes] = geometry_area(
                            partial_intersections
                        )
            pixel_areas = flat_areas.reshape(row_end - row_start, width)

        pixel_areas = pixel_areas.reshape(row_end - row_start, width)
        yield row_start, row_end, pixel_areas


def reference_cell_area_sums(
    grid: RasterGrid,
    comparable_mask: np.ndarray,
    class_map: np.ndarray,
    aoi_geometry=None,
) -> tuple[float, float, dict[str, float]]:
    """Measure grid, comparable, and per-class areas in equal-area metres."""
    total_grid_area = 0.0
    comparable_area = 0.0
    class_areas = {"decrease": 0.0, "stable": 0.0, "increase": 0.0}

    for row_start, row_end, pixel_areas in reference_pixel_area_chunks(grid, aoi_geometry):
        total_grid_area += float(pixel_areas.sum())
        comparable_chunk = comparable_mask[row_start:row_end]
        class_chunk = class_map[row_start:row_end]
        comparable_area += float(pixel_areas[comparable_chunk].sum())
        for class_name, class_value in (
            ("decrease", ChangeClass.DECREASE),
            ("stable", ChangeClass.STABLE),
            ("increase", ChangeClass.INCREASE),
        ):
            class_areas[class_name] += float(
                pixel_areas[class_chunk == class_value].sum()
            )

    return total_grid_area, comparable_area, class_areas
