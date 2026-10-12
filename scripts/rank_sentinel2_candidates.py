"""Test du classement Sentinel-2 par qualité réelle sur une AOI."""

from pathlib import Path
from src.geo.aoi import get_aoi_bbox
from src.data.ingestion import (
    create_cdse_s3_client,
    search_sentinel2,
)
from src.data.quality import select_best_product_for_aoi


PROJECT_ROOT = Path(__file__).resolve().parents[1]

AOI_PATH = (
    PROJECT_ROOT
    / "data"
    / "aoi"
    / "libreville.geojson"
)

RAW_DIR = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "sentinel2_quality"
)

INTERIM_DIR = (
    PROJECT_ROOT
    / "data"
    / "interim"
    / "sentinel2_quality"
)

bbox = get_aoi_bbox(AOI_PATH)

print(f"AOI : {AOI_PATH.stem}")
print(f"BBOX : {bbox}")

# 1. Recherche STAC
items = search_sentinel2(
    bbox=bbox,
    start_date="2025-01-01",
    end_date="2025-01-31",
)

print(f"Produits trouvés : {len(items)}")


# 2. Connexion CDSE
s3_client = create_cdse_s3_client()


# 3. Sélection par qualité AOI
selection = select_best_product_for_aoi(
    items=items,
    aoi_path=AOI_PATH,
    s3_client=s3_client,
    raw_dir=RAW_DIR,
    interim_dir=INTERIM_DIR,
)


# 4. Affichage du classement
print("\n==============================")
print("CLASSEMENT GEONAF - LIBREVILLE")
print("==============================")

for rank, result in enumerate(
    selection["ranking"],
    start=1,
):
    print(
        f"{rank}. {result['product_id']}"
    )

    print(
        "   STAC cloud cover : "
        f"{result['cloud_cover']}"
    )

    print(
        "   Terrain exploitable : "
        f"{result['usable_land_percent']:.2f} %"
    )


print("\nProduit sélectionné :")
print(selection["best_product_id"])