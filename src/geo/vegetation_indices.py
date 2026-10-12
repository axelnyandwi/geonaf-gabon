
import numpy as np
import rasterio


def calculate_ndvi(
    red_path,
    nir_path,
    valid_mask,
    red_offset,
    nir_offset,
    quantification_value,
):
    """Calculate calibrated NDVI from aligned red and NIR raster bands.

    Offsets apply to stored Sentinel-2 DN values before division by the BOA
    quantification value. Pass zero offsets and a unit quantification value
    when inputs already contain reflectance, so calibration is not repeated.
    """

    if (
        not np.isfinite(quantification_value)
        or quantification_value <= 0
        or not np.isfinite(red_offset)
        or not np.isfinite(nir_offset)
    ):
        raise ValueError("Paramètres de calibration invalides.")

    with rasterio.open(red_path) as red_src, \
         rasterio.open(nir_path) as nir_src:

        if red_src.crs is None or nir_src.crs is None:
            raise ValueError("CRS manquant sur une bande B04 ou B08.")

        if (
            red_src.crs != nir_src.crs
            or red_src.transform != nir_src.transform
            or red_src.shape != nir_src.shape
        ):
            raise ValueError("Bandes non alignées.")

        red = red_src.read(1, masked=True)
        nir = nir_src.read(1, masked=True)

        if valid_mask is None:
            valid_mask = np.ones(red.shape, dtype=bool)
        else:
            valid_mask = np.asarray(valid_mask, dtype=bool)
            if valid_mask.shape != red.shape:
                raise ValueError("Dimensions du masque incorrectes.")

        red_dn = red.data.astype(np.float32)
        nir_dn = nir.data.astype(np.float32)

        valid = (
            valid_mask
            & ~np.ma.getmaskarray(red)
            & ~np.ma.getmaskarray(nir)
            & (red_dn > 0)
            & (nir_dn > 0)
        )

        red_reflectance = (
            red_dn + red_offset
        ) / quantification_value

        nir_reflectance = (
            nir_dn + nir_offset
        ) / quantification_value

        denominator = red_reflectance + nir_reflectance

        valid &= (
            np.isfinite(denominator)
            & (denominator > 0)
        )

        ndvi = np.full(
            red.shape,
            np.nan,
            dtype=np.float32,
        )

        np.divide(
            nir_reflectance - red_reflectance,
            denominator,
            out=ndvi,
            where=valid,
        )

        return ndvi, red_src.profile.copy()
