"""Compare the CSV export of db/supabase/07_export_parity.sql with the file registry; offline.

    python scripts/verify_supabase_export.py --csv <export.csv> --data-dir <data>

Every database row is rebuilt from its record_document; its fingerprint, key columns and source release
must match, the record must equal the file registry's, and the SHA-256 of the WKB returned by PostGIS
must equal the one of the local MultiPolygon conversion. Exit code 0 only when nothing differs.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.postgis_territory_registry import plan_import  # noqa: E402
from src.data.supabase_sql import EXPORT_COLUMNS, check_export_rows  # noqa: E402
from src.data.territory_registry import TerritoryRegistry  # noqa: E402

csv.field_size_limit(16 * 1024 * 1024)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    with arguments.csv.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        missing = [c for c in EXPORT_COLUMNS if c not in (reader.fieldnames or ())]
        if missing:
            print(f"Colonnes absentes de l'export : {', '.join(missing)}", file=sys.stderr)
            return 2
        rows = list(reader)
    plan = plan_import(TerritoryRegistry(arguments.data_dir))
    problems = check_export_rows(rows, plan)
    print(f"Lignes exportées : {len(rows)} ; registre fichier : {len(plan.records)} versions.")
    for problem in problems:
        print(f"ÉCART : {problem}")
    print("Parité confirmée : base identique au registre fichier." if not problems
          else f"{len(problems)} écart(s) : parité non confirmée.")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
