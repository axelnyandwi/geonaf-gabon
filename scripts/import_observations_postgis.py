"""Import acquisitions and indicator results into PostGIS (migration 0002, D3); dry run by default.

Manual tool, see docs/postgis_storage_d3_d4.md. The data directory is only read and verified (manifest
validation and fingerprints, indicator result fingerprints); nothing is written under it.

    python scripts/import_observations_postgis.py --data-dir <data> --source-label "poste local" --scope all
    python scripts/import_observations_postgis.py --data-dir <data> --source-label "poste local" --scope all --apply

* without a database URL (or with ``--offline``): verification and counts only;
* with ``GEONAF_DATABASE_URL`` and without ``--apply``: each import runs in a transaction that is rolled
  back, and the report gives what would be inserted;
* with ``--apply``: committed; a second run inserts nothing (idempotent).

With a URL, ``GEONAF_DB_ENV`` (dev or test) is mandatory: the target is validated (remote host:
``verify-full`` and ``GEONAF_DB_SSLROOTCERT``) and each import checks the schema mark inside the
transaction before any write. ``--scope all`` is atomic: acquisitions and indicators are committed
together or not at all, and nothing is reported as imported before that single commit.

Indicator results need their geometries in the database: import the territory registry first
(scripts/import_territories_postgis.py). A rejected manifest stops the run: nothing partial is imported.
The URL and local paths are never printed.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.import_territories_postgis import code_version  # noqa: E402
from src.data.observations import FileAcquisitionCatalog, IndicatorResultStore  # noqa: E402
from src.data.database import (  # noqa: E402
    URL_VARIABLE, DatabaseConfigError, EnvironmentIdentityError, load_target,
)

SCOPES = ("acquisitions", "indicators", "all")


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", required=True, type=Path, help="Répertoire de données.")
    parser.add_argument("--source-label", required=True, help="Libellé de la source (jamais un chemin).")
    parser.add_argument("--scope", choices=SCOPES, default="all")
    parser.add_argument("--apply", action="store_true", help="Valider les transactions (sinon annulées).")
    parser.add_argument("--offline", action="store_true", help="Vérifier sans se connecter.")
    parser.add_argument("--code-version", default=None, help="Commit enregistré dans le journal d'import.")
    return parser.parse_args(argv)


def run_imports(connection, batches: dict, importers: dict, *, source_label: str, code_version: str,
                environment: str, apply: bool) -> list:
    """Every scope in ONE enclosing transaction (each import's own block becomes a savepoint).

    An error in any scope rolls back all of them; reports are returned only after the single commit.
    """
    reports = []
    with connection.transaction():
        for scope, items in batches.items():
            reports.append((scope, importers[scope](connection, items, source_label=source_label,
                                                    code_version=code_version, expected_environment=environment,
                                                    apply=apply)))
    return reports


def main(argv=None) -> int:
    arguments = parse_arguments(argv)
    if any(mark in arguments.source_label for mark in ("/", "\\", ":")):
        print("Le libellé de source ne doit pas être un chemin.", file=sys.stderr)
        return 2
    if not arguments.data_dir.is_dir():
        print("Répertoire de données introuvable.", file=sys.stderr)
        return 2
    batches = {}
    try:
        if arguments.scope in ("acquisitions", "all"):
            scan = FileAcquisitionCatalog(arguments.data_dir).scan()
            if scan.rejected:
                print("Manifestes refusés (aucun import partiel) : " + ", ".join(scan.rejected), file=sys.stderr)
                return 1
            batches["acquisitions"] = scan.records
            print(f"Acquisitions vérifiées : {len(scan.records)} manifestes, "
                  f"{len({r.identity for r in scan.records})} produits.")
        if arguments.scope in ("indicators", "all"):
            results = IndicatorResultStore(arguments.data_dir).results()
            batches["indicators"] = results
            print(f"Résultats d'indicateurs vérifiés : {len(results)} résultats, "
                  f"{len({r.definition_sha256 for r in results})} définitions, "
                  f"{len({r.geometry_sha256 for r in results})} géométries.")
    except (ValueError, KeyError) as error:
        print(f"Données refusées : {error}", file=sys.stderr)
        return 1
    database = bool(os.environ.get(URL_VARIABLE, "").strip()) and not arguments.offline
    if not database:
        if arguments.apply:
            print(f"--apply exige {URL_VARIABLE} (et pas --offline).", file=sys.stderr)
            return 2
        print("Mode vérification seule : aucune connexion à une base.")
        return 0
    try:
        target = load_target()
    except DatabaseConfigError as error:
        print(f"Configuration refusée : {error}", file=sys.stderr)
        return 2
    from src.data.postgis_observations import (
        ObservationImportError, apply_acquisition_import, apply_indicator_import,
    )
    from src.data.postgis_session import connect_target, driver_error_message

    importers = {"acquisitions": apply_acquisition_import, "indicators": apply_indicator_import}
    try:
        with connect_target(target) as connection:
            reports = run_imports(connection, batches, importers, source_label=arguments.source_label,
                                  code_version=arguments.code_version or code_version(),
                                  environment=target.environment, apply=arguments.apply)
    except (ObservationImportError, EnvironmentIdentityError, DatabaseConfigError) as error:
        print(f"Import refusé, aucun périmètre validé : {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - driver errors name the host: SQLSTATE class only.
        message = driver_error_message(error) if hasattr(error, "sqlstate") else type(error).__name__
        print(f"Import refusé, aucun périmètre validé : {message}", file=sys.stderr)
        return 1
    for scope, report in reports:
        state = f"journal {report.import_id}" if report.applied else "transaction annulée (simulation)"
        print(f"{scope} : {report.seen} vus, {report.inserted} insérés, {report.existing} déjà présents ; "
              f"empreinte {report.fingerprints_sha256[:16]}… ; {state}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
