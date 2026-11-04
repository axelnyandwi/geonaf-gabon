"""Import the file territory registry into PostGIS (D2-a); dry run by default.

Manual tool, see docs/postgis_registry.md. The file registry is only read and verified (record
fingerprints, geometry file digests, geometry fingerprints, version chains); nothing is written
under the data directory.

    python scripts/import_territories_postgis.py --data-dir <data> --source-label "registre local"
    python scripts/import_territories_postgis.py --data-dir <data> --source-label "registre local" --apply

* without a database URL (or with ``--offline``): verification and plan only;
* with ``GEONAF_DATABASE_URL`` and without ``--apply``: the import runs in a transaction that is
  rolled back, and the report gives what would be inserted;
* with ``--apply``: the transaction is committed; a second run inserts nothing.

The URL and local paths are never printed.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.postgis_territory_registry import (  # noqa: E402
    URL_VARIABLE, TerritoryImportError, apply_import, connect_from_env, plan_import,
)
from src.data.territory_registry import TerritoryRegistry  # noqa: E402


def code_version() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                                timeout=10, check=True)
        return result.stdout.strip() or "inconnu"
    except (OSError, subprocess.SubprocessError):
        return "inconnu"


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", required=True, type=Path, help="Répertoire de données contenant territories/.")
    parser.add_argument("--source-label", required=True, help="Libellé de la source (jamais un chemin).")
    parser.add_argument("--apply", action="store_true", help="Valider la transaction (sinon annulée).")
    parser.add_argument("--offline", action="store_true", help="Vérifier et planifier sans se connecter.")
    parser.add_argument("--code-version", default=None, help="Commit enregistré dans le journal d'import.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    arguments = parse_arguments(argv)
    if any(mark in arguments.source_label for mark in ("/", "\\", ":")):
        print("Le libellé de source ne doit pas être un chemin.", file=sys.stderr)
        return 2
    if not (arguments.data_dir / "territories").is_dir():
        print("Registre de territoires introuvable dans le répertoire de données indiqué.", file=sys.stderr)
        return 2
    try:
        plan = plan_import(TerritoryRegistry(arguments.data_dir))
    except (TerritoryImportError, ValueError) as error:
        print(f"Registre refusé : {error}", file=sys.stderr)
        return 1
    print(f"Registre vérifié : {len(plan.records)} versions, {len(plan.geometries)} géométries, "
          f"{len(plan.releases)} versions de source, licences {', '.join(plan.licence_ids) or 'aucune'}.")
    print(f"Empreinte globale des enregistrements : {plan.fingerprints_sha256}")
    database = bool(os.environ.get(URL_VARIABLE, "").strip()) and not arguments.offline
    if not database:
        if arguments.apply:
            print(f"--apply exige {URL_VARIABLE} (et pas --offline).", file=sys.stderr)
            return 2
        print("Mode vérification seule : aucune connexion à une base.")
        return 0
    try:
        with connect_from_env()() as connection:
            report = apply_import(connection, plan, source_label=arguments.source_label,
                                  code_version=arguments.code_version or code_version(), apply=arguments.apply)
    except TerritoryImportError as error:
        print(f"Import refusé, base inchangée : {error}", file=sys.stderr)
        return 1
    verb = "insérées" if report.applied else "à insérer (transaction annulée)"
    print(f"Versions {verb} : {report.inserted} ; déjà présentes et identiques : {report.existing}.")
    if report.applied:
        print(f"Journal d'import : {report.import_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
