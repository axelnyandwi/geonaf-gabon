"""Explicit read-only live check; outside pytest, never downloads raster assets."""

from datetime import date
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DATA_DIR
from src.data.catalogue import CatalogueQuery, CatalogueError
from src.services.acquisition_catalogue import load_catalogue_aoi, search_catalogue, catalogue_rows
from src.data import ingestion
from scripts.check_stac_pagination_live import SearchObservation, _TrackedClient


def main():
    try:
        query = CatalogueQuery(load_catalogue_aoi(DATA_DIR, "akanda"), date(2025, 1, 1), date(2025, 1, 31))
        observation = SearchObservation()
        original_open = ingestion.Client.open
        def tracked_open(*args, **kwargs):
            return _TrackedClient(original_open(*args, **kwargs), observation)
        # Reuse the pre-existing read-only diagnostic instrumentation. Small
        # pages exercise real next links while the production search stays complete.
        with patch.object(ingestion, "STAC_PAGE_SIZE", 2), patch.object(ingestion.Client, "open", tracked_open):
            result = search_catalogue(query, DATA_DIR)
        print(json.dumps({"complete": result.complete, "raw_count": result.raw_count,
                          "pages_observed": observation.page_count,
                          "records": catalogue_rows(result), "asset_downloads": 0}, ensure_ascii=True, indent=2))
        return 0
    except CatalogueError as error:
        print(json.dumps({"error": str(error), "asset_downloads": 0}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
