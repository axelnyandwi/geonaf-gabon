"""Manual benchmark of the territorial key figures on stored layers (offline, cold, no cache).

Usage : python scripts/benchmark_territorial_indicators.py [akanda libreville owendo]
Prints the time of each step; reads local layers only, writes nothing.
"""

import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DATA_DIR  # noqa: E402
from src.data.openstreetmap import load_osm_snapshot  # noqa: E402
from src.services.external_layers import open_store  # noqa: E402
from src.services.territorial_indicators import (  # noqa: E402
    build_indicators, building_figures, indicator_inputs, landcover_areas, road_lengths,
)


def timed(label, function, *args, **kwargs):
    start = perf_counter()
    value = function(*args, **kwargs)
    print(f"  {label:<28} {perf_counter() - start:6.2f} s")
    return value


for aoi_id in sys.argv[1:] or ["akanda"]:
    print(f"== {aoi_id}")
    start = perf_counter()
    inputs = timed("inventaire (empreintes)", indicator_inputs, DATA_DIR, aoi_id)
    tiles = [(layer.path, layer.descriptor) for layer in inputs.worldcover]
    if tiles:
        timed("surfaces WorldCover", landcover_areas, tiles, inputs.aoi_path)
    osm = None
    if inputs.osm is not None:
        store, _ = open_store(DATA_DIR)
        osm = timed("lecture instantané OSM", load_osm_snapshot, store, inputs.osm.storage_key, inputs.bounds)
        timed("longueurs de voies", road_lengths, osm[0], inputs.aoi_path)
        timed("bâtiments", building_figures, osm[0], inputs.aoi_path)
    timed("assemblage complet", build_indicators, aoi_id=aoi_id, aoi_path=inputs.aoi_path, worldcover=tiles or None,
          osm=osm, temporal_notes=inputs.temporal_notes)
    print(f"  {'total (étapes répétées incl.)':<28} {perf_counter() - start:6.2f} s")
