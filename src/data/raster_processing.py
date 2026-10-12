
from pathlib import Path

import rasterio
from rasterio.mask import mask
from rasterio.warp import transform_geom
from src.diagnostics import safe_call


def clip_raster(
    raster_path: str | Path,
    geometry: dict,
    output_path: str | Path,
    geometry_crs: str = "EPSG:4326",
) -> Path:
    """Découpe un raster avec gestion explicite du nodata."""

    raster_path = Path(raster_path)
    output_path = Path(output_path)

    if not raster_path.is_file():
        raise FileNotFoundError("Raster source introuvable.")

    with safe_call(rasterio.open, raster_path, paths=(raster_path,)) as src:
        if src.crs is None:
            raise ValueError("CRS du raster manquant.")

        projected_geometry = transform_geom(
            geometry_crs,
            src.crs,
            geometry,
        )

        # Conserver le masque géographique
        clipped, transform = mask(
            src,
            [projected_geometry],
            crop=True,
            filled=False,
        )

        # Sentinel-2 L2A utilise généralement 0
        # comme valeur NO_DATA.
        nodata_value = (
            src.nodata if src.nodata is not None else 0
        )

        metadata = src.meta.copy()
        metadata.update(
            driver="GTiff",
            height=clipped.shape[1],
            width=clipped.shape[2],
            transform=transform,
            nodata=nodata_value,
            compress="deflate",
        )

        safe_call(
            output_path.parent.mkdir,
            parents=True,
            exist_ok=True,
            paths=(output_path.parent,),
        )

        with safe_call(rasterio.open, output_path, "w", paths=(output_path,), **metadata) as dst:
            dst.write(clipped.filled(nodata_value))

    return output_path
