from pathlib import Path
import rasterio
import numpy as np
from src.data.preparation import (
    clip_raster_to_aoi,
    calculate_ndvi,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

RAW_DIR = PROJECT_ROOT / "data" / "raw" / "sentinel2"
AOI_PATH = PROJECT_ROOT / "data" / "aoi" / "libreville.geojson"

OUTPUT_DIR = (
    PROJECT_ROOT
    / "data"
    / "interim"
    / "sentinel2"
    / "ndvi_test"
    / "libreville"
)


b04_files = [
    path
    for path in RAW_DIR.rglob("*B04_10m.jp2")
    if (path.parent / path.name.replace("B04_10m", "B08_10m")).is_file()
    and (path.parent / "MTD_MSIL2A.xml").is_file()
]

if not b04_files:
    raise FileNotFoundError("Paire B04/B08 avec métadonnées de calibration introuvable.")

b04_file = b04_files[0]
b08_file = b04_file.with_name(
    b04_file.name.replace("B04_10m", "B08_10m")
)
calibration_xml = b04_file.parent / "MTD_MSIL2A.xml"


b04_output = OUTPUT_DIR / "libreville_B04_10m.tif"
b08_output = OUTPUT_DIR / "libreville_B08_10m.tif"
ndvi_output = OUTPUT_DIR / "libreville_NDVI.tif"


clip_raster_to_aoi(
    b04_file,
    b04_output,
    AOI_PATH,
)

clip_raster_to_aoi(
    b08_file,
    b08_output,
    AOI_PATH,
)

calculate_ndvi(
    red_path=b04_output,
    nir_path=b08_output,
    output_path=ndvi_output,
    calibration_xml=calibration_xml,
)


with rasterio.open(ndvi_output) as src:
    ndvi = src.read(1)

    print("NDVI créé :", ndvi_output)
    print("Dimensions :", ndvi.shape)
    print("Minimum :", float(np.nanmin(ndvi)))
    print("Maximum :", float(np.nanmax(ndvi)))
    print("Moyenne :", float(np.nanmean(ndvi)))

    valid_pixels = np.count_nonzero(~np.isnan(ndvi))
    total_pixels = ndvi.size

    print(
        "Pixels valides :",
        valid_pixels,
        "/",
        total_pixels,
    )

    print(
        "Part valide :",
        round(valid_pixels / total_pixels * 100, 2),
        "%",
    )
