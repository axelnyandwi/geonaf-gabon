"""Manual read-only diagnostic of the PostgreSQL/PostGIS target (network access; not a pytest test).

Reads GEONAF_DATABASE_URL and GEONAF_DB_ENV (environment or the project's .env), then checks in one
read-only transaction: connection, geonaf schema, environment mark, expected tables, PostGIS.
Nothing is written. The output shows the target without password. Exit code: 0 all checks passed,
1 a check failed, 2 configuration error. Requires requirements-db.txt. See docs/database_connection.md.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from dotenv import load_dotenv

    from src.data.database import DatabaseConfigError, load_target
    from src.data.database_diagnostic import diagnose

    load_dotenv(ROOT / ".env")
    try:
        target = load_target()
        report = diagnose(target)
    except DatabaseConfigError as error:
        print(f"Configuration invalide : {error}")
        return 2
    print(f"Cible : {report.target}")
    for check in report.checks:
        print(f"[{check.status}] {check.name} : {check.detail}")
    print("Diagnostic conforme." if report.ok else "Diagnostic non conforme.")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
