"""Time the storage reads used by the Territoires page, in file mode and (optionally) PostGIS mode; read only.

Manual diagnostic, not a test (it reads a real data directory and may reach a database):

    python scripts/benchmark_storage_queries.py --data-dir <data> [--repeat 5]

* file mode: always measured on ``--data-dir`` (registry, manifests, indicator results);
* PostGIS mode: measured only when ``GEONAF_DATABASE_URL`` is set (local database advised); the three
  spatial queries are also run under ``EXPLAIN (ANALYZE, BUFFERS)`` inside a read-only transaction with
  the R5 bis search path, and the plan nodes are printed (index or sequential scan).

Run with ``GEONAF_DEMO_READ_ONLY=1`` to make any accidental write impossible. Paths and URLs are never
printed; times are medians in milliseconds.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

AKANDA = (9.38, 0.50, 9.50, 0.62)        # Around the Akanda acquisition square.


def median_ms(function, repeat: int):
    times, value = [], None
    for _ in range(repeat):
        start = time.perf_counter()
        value = function()
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times), value


def measure(label: str, store, data_dir: Path, repeat: int) -> None:
    from src.services import territory_catalog as catalog

    rows = []
    elapsed, territories = median_ms(lambda: store.territories.territories(), repeat)
    rows.append(("territoires (dernières versions)", elapsed, len(territories)))
    elapsed, options = median_ms(lambda: catalog.admin_territories(data_dir, "gbHumanitarian", "ADM2",
                                                                   registry=store.territories), repeat)
    rows.append(("catalogue gbHumanitarian ADM2", elapsed, len(options)))
    if options:
        elapsed, _ = median_ms(lambda: [store.territories.geometry(o.record) for o in options], max(1, repeat // 2))
        rows.append(("géométries vérifiées (toutes)", elapsed, len(options)))
    elapsed, found = median_ms(lambda: store.territories.intersecting(AKANDA), repeat)
    rows.append(("territoires recoupant Akanda", elapsed, len(found)))
    elapsed, latest = median_ms(lambda: store.acquisitions.latest(), repeat)
    rows.append(("acquisitions (dernière ingestion)", elapsed, len(latest)))
    elapsed, results = median_ms(lambda: store.indicators.results(), repeat)
    rows.append(("résultats d'indicateurs", elapsed, len(results)))
    print(f"\n{label}")
    for name, milliseconds, count in rows:
        print(f"  {name:<38} {milliseconds:9.1f} ms  ({count})")


def explain(repeat: int) -> None:
    from src.data import postgis_observations as pgo
    from src.data import postgis_territory_registry as pg
    from src.data.postgis_session import plain_read_transaction

    west, south, east, north = AKANDA
    params = {"west": west, "south": south, "east": east, "north": north, "kind": None, "geometry_sha256": None}
    queries = {"territoires recoupant": pg.QUERIES["latest_intersecting"],
               "acquisitions recoupant": pgo.QUERIES["manifests_intersecting"],
               "résultats": pgo.QUERIES["results"]}
    print("\nPlans PostGIS (EXPLAIN ANALYZE, transaction en lecture seule)")
    with plain_read_transaction(pg.connect_from_env()) as connection:
        for name, query in queries.items():
            lines = [row[0] for row in connection.execute("EXPLAIN (ANALYZE, BUFFERS) " + query, params).fetchall()]
            nodes = [line.strip() for line in lines if "Scan" in line or "Execution Time" in line]
            print(f"  {name}:")
            for node in nodes[:6]:
                print(f"    {node}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--repeat", type=int, default=5)
    arguments = parser.parse_args(argv)
    if not arguments.data_dir.is_dir():
        print("Répertoire de données introuvable.", file=sys.stderr)
        return 2
    from src.data import storage

    measure("Mode fichiers", storage.file_store(arguments.data_dir), arguments.data_dir, arguments.repeat)
    if os.environ.get("GEONAF_DATABASE_URL", "").strip():
        try:
            measure("Mode PostGIS", storage.postgis_store(arguments.data_dir, {
                **os.environ, "GEONAF_DB_BACKEND": "postgres"}), arguments.data_dir, arguments.repeat)
            explain(arguments.repeat)
        except Exception as error:  # noqa: BLE001 - the driver message may hold the URL: type only.
            print(f"Mode PostGIS non mesuré ({type(error).__name__}).", file=sys.stderr)
            return 1
    else:
        print("\nMode PostGIS non mesuré : GEONAF_DATABASE_URL non défini.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
