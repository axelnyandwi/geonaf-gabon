"""Import acquisitions and indicator results into PostGIS (migration 0002, D3); dry run by default.

Manual tool, see docs/postgis_storage_d3_d4.md. The data directory is only read and verified (manifest
validation and fingerprints, indicator result fingerprints); nothing is written under it.

    python scripts/import_observations_postgis.py --data-dir <data> --source-label "poste local" --scope all
    python scripts/import_observations_postgis.py --data-dir <data> --source-label "poste local" --scope all --apply

* without a database URL (or with ``--offline``): verification and counts only;
* with ``GEONAF_DATABASE_URL`` and without ``--apply``: each import runs in a transaction that is rolled
  back, and the report gives what would be inserted;
* with ``--apply``: committed; a second run inserts nothing (idempotent).

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
from src.data.postgis_territory_registry import URL_VARIABLE, connect_from_env  # noqa: E402

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
    from src.data.postgis_observations import (
        ObservationImportError, apply_acquisition_import, apply_indicator_import,
    )

    version = arguments.code_version or code_version()
    importers = {"acquisitions": apply_acquisition_import, "indicators": apply_indicator_import}
    try:
        with connect_from_env()() as connection:
            for scope, items in batches.items():
                report = importers[scope](connection, items, source_label=arguments.source_label,
                                          code_version=version, apply=arguments.apply)
                state = f"journal {report.import_id}" if report.applied else "transaction annulée (simulation)"
                print(f"{scope} : {report.seen} vus, {report.inserted} insérés, {report.existing} déjà présents ; "
                      f"empreinte {report.fingerprints_sha256[:16]}… ; {state}.")
    except ObservationImportError as error:
        print(f"Import refusé : {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - driver errors may contain the URL: type only.
        print(f"Échec de la connexion ou de l'import ({type(error).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
