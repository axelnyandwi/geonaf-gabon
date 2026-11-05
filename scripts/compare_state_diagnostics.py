"""Compare two CSV exports of db/supabase/11_state_diagnostic.sql (geonaf-test and geonaf-dev); offline.

    python scripts/compare_state_diagnostics.py <export test.csv> <export dev.csv>

Only rows marked ``comparable = true`` are compared, by (rubrique, element). Instance-specific rows
(dates, OIDs, row estimates, environment marks) are ignored. Output: the recognised state of each
project, then every difference and every row present on one side only. Exit code 0 when the comparable
rows are identical, 1 otherwise, 2 when an export cannot be read. Paths and values of ignored rows are
never printed.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

COLUMNS = ("ordre", "rubrique", "element", "valeur", "comparable")


class ExportError(ValueError):
    """The file is not an export of 11_state_diagnostic.sql."""


def read_export(path: Path) -> dict[tuple[str, str], tuple[str, bool]]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != COLUMNS:
                raise ExportError("colonnes inattendues (export de 11_state_diagnostic.sql attendu)")
            rows = {}
            for row in reader:
                key = (row["rubrique"], row["element"])
                if key in rows:
                    raise ExportError(f"ligne en double : {key[0]} / {key[1]}")
                rows[key] = (row["valeur"], row["comparable"].strip().lower() in ("true", "t", "1"))
    except OSError:
        raise ExportError("fichier illisible") from None
    if ("etat", "etat reconnu") not in rows:
        raise ExportError("ligne « etat reconnu » absente")
    return rows


def compare(a: dict, b: dict) -> list[str]:
    """Human-readable differences between the comparable rows of two exports (empty: identical)."""
    differences = []
    for key in sorted(set(a) | set(b)):
        left, right = a.get(key), b.get(key)
        if left is not None and right is not None:
            if left[1] and right[1] and left[0] != right[0]:
                differences.append(f"{key[0]} / {key[1]} : « {left[0]} » ≠ « {right[0]} »")
        elif (left or right)[1]:
            side = "premier" if right is None else "second"
            differences.append(f"{key[0]} / {key[1]} : seulement dans le {side} export")
    return differences


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    arguments = parser.parse_args(argv)
    try:
        first, second = read_export(arguments.first), read_export(arguments.second)
    except ExportError as error:
        print(f"Export refusé : {error}.", file=sys.stderr)
        return 2
    print(f"Premier export : {first[('etat', 'etat reconnu')][0]}")
    print(f"Second export : {second[('etat', 'etat reconnu')][0]}")
    differences = compare(first, second)
    if not differences:
        print("Lignes comparables identiques.")
        return 0
    print(f"{len(differences)} écart(s) :")
    for line in differences:
        print(f"- {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
