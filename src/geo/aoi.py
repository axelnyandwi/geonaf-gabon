from pathlib import Path
import json
import geopandas as gpd

from src.diagnostics import safe_call


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AOI_DIR = PROJECT_ROOT / "data" / "aoi"


def load_aoi(name: str) -> dict:
    """
    Charge une AOI GeoJSON depuis data/aoi/.

    Exemple :
        load_aoi("libreville")
        load_aoi("akanda")
        load_aoi("owendo")
    """

    aoi_path = AOI_DIR / f"{name.lower()}.geojson"

    if not aoi_path.exists():
        raise FileNotFoundError(
            "AOI introuvable."
        )

    with safe_call(open, aoi_path, "r", encoding="utf-8", paths=(aoi_path,)) as file:
        return json.load(file)

def get_aoi_bbox(aoi_path: Path) -> list[float]:
    """Return the bounding box of an AOI in EPSG:4326."""

    import numpy as np
    from shapely import get_coordinates
    from shapely.validation import explain_validity

    aoi = safe_call(gpd.read_file, aoi_path, paths=(aoi_path,))

    if aoi.empty:
        raise ValueError("AOI vide.")

    if aoi.crs is None:
        raise ValueError(
            "L'AOI ne possède pas de CRS."
        )

    for index, geometry in aoi.geometry.items():
        if geometry is None or geometry.is_empty:
            raise ValueError(
                f"AOI invalide : géométrie vide (élément {index})."
            )

        if not geometry.is_valid:
            reason = explain_validity(geometry)
            raise ValueError(
                f"AOI invalide : géométrie invalide (élément {index}) : "
                f"{reason}"
            )

    source_coordinates = get_coordinates(aoi.geometry.array)
    if not np.isfinite(source_coordinates).all():
        raise ValueError(
            "Coordonnées non finies dans l'AOI."
        )

    # Check geographic input before reprojection: a transformation may
    # otherwise wrap or normalize out-of-range longitude values.
    if aoi.crs.is_geographic:
        source_longitudes = source_coordinates[:, 0]
        source_latitudes = source_coordinates[:, 1]
        if (
            (source_longitudes < -180).any()
            or (source_longitudes > 180).any()
            or (source_latitudes < -90).any()
            or (source_latitudes > 90).any()
        ):
            raise ValueError(
                "L'AOI contient des coordonnées géographiques hors plage WGS84 "
                "(longitude [-180, 180], latitude [-90, 90])."
            )

    aoi_wgs84 = aoi.to_crs("EPSG:4326")
    wgs84_coordinates = get_coordinates(aoi_wgs84.geometry.array)
    if not np.isfinite(wgs84_coordinates).all():
        raise ValueError(
            "La reprojection vers EPSG:4326 produit des coordonnées non finies."
        )

    longitudes = wgs84_coordinates[:, 0]
    latitudes = wgs84_coordinates[:, 1]
    if (
        (longitudes < -180).any()
        or (longitudes > 180).any()
        or (latitudes < -90).any()
        or (latitudes > 90).any()
    ):
        raise ValueError(
            "L'AOI contient des coordonnées géographiques hors plage WGS84 après reprojection "
            "(longitude [-180, 180], latitude [-90, 90])."
        )

    minx, miny, maxx, maxy = aoi_wgs84.total_bounds

    return [
        float(minx),
        float(miny),
        float(maxx),
        float(maxy),
    ]
