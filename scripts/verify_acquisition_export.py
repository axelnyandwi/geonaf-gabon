"""Compare the CSV export of db/supabase/14_export_acquisitions_parity.sql with the local manifests; offline.

    python scripts/verify_acquisition_export.py --csv <export.csv> --data-dir <data>

Every database manifest is rebuilt from manifest_document and must equal a strict local manifest: digest,
columns, acquisition and ingestion dates, grid footprint (SRID 4326, WKB digest), number of ingestions.
Exit code 0 only when nothing differs, 1 on a difference, 2 on an unusable export or local data.
Nothing is written; no network access; no database driver.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.acquisition_export import EXPORT_COLUMNS, check_acquisition_export  # noqa: E402
from src.data.observations import FileAcquisitionCatalog  # noqa: E402

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
    scan = FileAcquisitionCatalog(arguments.data_dir).scan()
    if scan.rejected:
        print(f"{len(scan.rejected)} manifeste(s) local(aux) refusé(s) : corriger les données locales avant "
              "de conclure.", file=sys.stderr)
        return 2
    problems = check_acquisition_export(rows, scan.records)
    print(f"Lignes exportées : {len(rows)} ; manifestes locaux : {len(scan.records)}.")
    for problem in problems:
        print(f"ÉCART : {problem}")
    print("Parité confirmée : acquisitions et ingestions de la base identiques aux manifestes locaux."
          if not problems else f"{len(problems)} écart(s) : parité non confirmée (écart de copie, "
          "pas un changement territorial).")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
