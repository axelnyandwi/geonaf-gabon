"""Generate the SQL Editor scripts for Supabase (D2-a2); writes files only, never connects.

    python scripts/build_supabase_sql.py migration
        -> db/supabase/02_migration_0001_simulation.sql and 02_migration_0001_application.sql
    python scripts/build_supabase_sql.py import --data-dir <data> --source-label "registre local" --output-dir <dir>
        -> <dir>/05_import_lot_NN_simulation.sql, 05_import_lot_NN_application.sql, 06_verify_import.sql,
           manifest.json (SHA-256 of every file and the expected values)

The import files are written outside the repository (they hold data, not code). The file registry is
read and verified only (src.data.postgis_territory_registry.plan_import); nothing is written under the
data directory. See docs/supabase_registry.md for the execution order.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.postgis_territory_registry import TerritoryImportError, plan_import  # noqa: E402
from src.data.supabase_sql import (  # noqa: E402
    expected_intersecting, import_batches, migration_wrapper, verify_import_sql,
)
from src.data.territory_registry import TerritoryRegistry  # noqa: E402

SUPABASE_DIR = ROOT / "db" / "supabase"
MIGRATION_FILES = {"02_migration_0001_simulation.sql": False, "02_migration_0001_application.sql": True}


def write(path: Path, text: str) -> str:
    path.write_bytes(text.encode("utf-8"))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_version() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                               timeout=10, check=True).stdout.strip() or "inconnu"
    except (OSError, subprocess.SubprocessError):
        return "inconnu"


def build_migration() -> int:
    for name, apply in MIGRATION_FILES.items():
        print(f"{name} : {write(SUPABASE_DIR / name, migration_wrapper(apply=apply))}")
    return 0


def build_import(arguments) -> int:
    output = arguments.output_dir.resolve()
    if output == ROOT or ROOT in output.parents:
        print("Le répertoire de sortie doit être hors du dépôt (fichiers de données).", file=sys.stderr)
        return 2
    if any(mark in arguments.source_label for mark in ("/", "\\", ":")):
        print("Le libellé de source ne doit pas être un chemin.", file=sys.stderr)
        return 2
    try:
        plan = plan_import(TerritoryRegistry(arguments.data_dir))
    except (TerritoryImportError, ValueError) as error:
        print(f"Registre refusé : {error}", file=sys.stderr)
        return 1
    output.mkdir(parents=True, exist_ok=True)
    version = arguments.code_version or code_version()
    files = {}
    for apply, mode in ((False, "simulation"), (True, "application")):
        batches = import_batches(plan, source_label=arguments.source_label, code_version=version, apply=apply,
                                 batch_size=arguments.batch_size)
        for number, text in enumerate(batches, start=1):
            name = f"05_import_lot_{number:02d}_{mode}.sql"
            files[name] = write(output / name, text)
    files["06_verify_import.sql"] = write(output / "06_verify_import.sql", verify_import_sql(plan))
    manifest = {"records": len(plan.records), "geometries": len(plan.geometries), "releases": len(plan.releases),
                "licences": list(plan.licence_ids), "fingerprints_sha256": plan.fingerprints_sha256,
                "akanda_intersecting": expected_intersecting(plan), "source_label": arguments.source_label,
                "code_version": version, "files_sha256": files}
    write(output / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(f"Registre vérifié : {len(plan.records)} versions, {len(plan.geometries)} géométries, "
          f"{len(plan.releases)} versions de source.")
    print(f"Empreinte globale : {plan.fingerprints_sha256}")
    print(f"{len(files)} fichiers SQL écrits (lots de {arguments.batch_size} versions au plus), plus manifest.json.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migration", help="Écrire la migration 0001 pour le SQL Editor (dans db/supabase).")
    batch = commands.add_parser("import", help="Écrire les lots d'import et la vérification (hors du dépôt).")
    batch.add_argument("--data-dir", required=True, type=Path)
    batch.add_argument("--source-label", required=True)
    batch.add_argument("--output-dir", required=True, type=Path)
    batch.add_argument("--batch-size", type=int, default=25)
    batch.add_argument("--code-version", default=None)
    arguments = parser.parse_args(argv)
    return build_migration() if arguments.command == "migration" else build_import(arguments)


if __name__ == "__main__":
    sys.exit(main())
