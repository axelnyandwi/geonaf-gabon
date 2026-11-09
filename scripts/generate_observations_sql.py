"""Generate a reviewable Supabase SQL Editor import of the acquisitions (D3), without connecting to a database.

Limited to acquisitions: indicator results need a separate, geometry-aware implementation.
Same conventions as the territory batches (src/data/supabase_sql.py, scripts/build_supabase_sql.py):
* one request, no BEGIN/COMMIT/ROLLBACK: any error cancels the whole request;
* SIMULATION by default: the import block itself ends with a deliberate error, so even a partial
  selection of that block writes nothing; --apply-sql removes that error but never executes anything;
* R5 search path first; environment mark, migration 0002 and PostGIS checked before any write;
* every value is a single-quoted literal (NUL refused) inside a dollar-quoted block whose tag is
  verified absent from the content: no value can end the block and inject SQL.

No database driver is imported, even indirectly (tests/test_generate_observations_sql.py). The output
must lie outside the repository (it contains the manifest documents) and is never overwritten.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.observations import FileAcquisitionCatalog  # noqa: E402  (pure module, no driver)

SEARCH_PATH = "SELECT set_config('search_path', 'pg_catalog, extensions, pg_temp', true);"
_HEX64 = re.compile(r"[a-f0-9]{64}\Z")


def aggregate_sha256(fingerprints: Iterable[str]) -> str:
    """Same formula as src.data.postgis_observations.aggregate_sha256 (parity test), without its driver."""
    return hashlib.sha256("\n".join(sorted(fingerprints)).encode("ascii")).hexdigest()


def code_version() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                                timeout=10, check=True)
        return result.stdout.strip() or "inconnu"
    except (OSError, subprocess.SubprocessError):
        return "inconnu"


def sql_literal(value: Any) -> str:
    """PostgreSQL string literal (standard_conforming_strings), NULL for None; NUL characters refused."""
    if value is None:
        return "NULL"
    text = str(value)
    if "\x00" in text:
        raise ValueError("Caractère nul interdit dans un littéral SQL")
    return "'" + text.replace("'", "''") + "'"


def timestamp(value: Any) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Horodatage sans fuseau horaire")
    return parsed.isoformat()


def finite_number(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("Résolution de grille non valide")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Résolution de grille non valide")
    return repr(number)


def fingerprint(value: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError("Empreinte SHA-256 non valide")
    return value


def block_tag(body: str) -> str:
    """Dollar-quote tag absent from the body (as supabase_sql.dollar_quoted)."""
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    for length in range(8, 65, 8):
        tag = f"$gi{digest[:length]}$"
        if tag not in body:
            return tag
    raise ValueError("Aucune balise de citation disponible")


def _record_lines(record) -> list[str]:
    manifest = fingerprint(record.manifest_sha256)
    doc = json.dumps(record.document, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    wkb = record.footprint_wkb
    if wkb is not None and (not isinstance(wkb, bytes) or not wkb):
        raise ValueError("Empreinte géographique WKB invalide")
    footprint = "NULL" if wkb is None else f"extensions.ST_GeomFromWKB(pg_catalog.decode('{wkb.hex()}', 'hex'), 4326)"
    acquired = timestamp(record.acquired_at_utc)
    ingested = timestamp(record.ingested_at_utc)
    if datetime.fromisoformat(ingested) < datetime.fromisoformat(acquired):
        raise ValueError("Date d'ingestion antérieure à l'acquisition")
    collection, product = sql_literal(record.collection), sql_literal(record.product_id)
    return [
        "  INSERT INTO geonaf.acquisitions (collection, product_id, acquired_at)",
        f"    VALUES ({collection}, {product}, {sql_literal(acquired)}::timestamptz)",
        "    ON CONFLICT (collection, product_id) DO NOTHING;",
        f"  SELECT acquired_at INTO v_existing FROM geonaf.acquisitions WHERE collection = {collection}"
        f" AND product_id = {product};",
        f"  IF v_existing IS DISTINCT FROM {sql_literal(acquired)}::timestamptz THEN",
        "    RAISE EXCEPTION 'GEONAF: date d''acquisition divergente, import refuse';",
        "  END IF;",
        f"  SELECT manifest_document INTO v_doc FROM geonaf.acquisition_manifests WHERE manifest_sha256 = {sql_literal(manifest)};",
        "  IF FOUND THEN",
        f"    IF v_doc IS DISTINCT FROM {sql_literal(doc)}::jsonb THEN",
        "      RAISE EXCEPTION 'GEONAF: document de manifeste divergent, import refuse';",
        "    END IF;",
        "  ELSE",
        "    INSERT INTO geonaf.acquisition_manifests",
        "      (manifest_sha256, collection, product_id, schema_version, acquired_at, ingested_at,",
        "       stac_source, grid_crs, grid_resolution_m, footprint, integrity_mode, manifest_document, import_id)",
        f"    VALUES ({sql_literal(manifest)}, {collection}, {product},",
        f"      {sql_literal(record.document['schema_version'])}, {sql_literal(acquired)}::timestamptz,"
        f" {sql_literal(ingested)}::timestamptz,",
        f"      {sql_literal(record.stac_source)}, {sql_literal(record.grid_crs)}, {finite_number(record.grid_resolution_m)},",
        f"      {footprint},",
        f"      {sql_literal(record.integrity_mode)}, {sql_literal(doc)}::jsonb, v_import_id);",
        "    v_inserted := v_inserted + 1;",
        "  END IF;",
        "  v_seen := v_seen + 1;",
    ]


def generate_sql(records: Iterable[Any], *, label: str, version: str,
                 environment: str = "test", apply_sql: bool = False) -> str:
    """One request: guards, idempotent inserts with divergence refusals, closed journal entry."""
    if environment not in ("test", "dev"):
        raise ValueError("Environnement invalide")
    if not isinstance(label, str) or not label.strip() or any(char in label for char in ("/", "\\", ":")):
        raise ValueError("Libellé de source invalide")
    if not isinstance(version, str) or not version.strip() or len(version) > 255:
        raise ValueError("Version de code invalide")

    rows = sorted(records, key=lambda r: r.manifest_sha256)
    if not rows:
        raise ValueError("Aucun manifeste à importer")
    hashes = [fingerprint(r.manifest_sha256) for r in rows]
    if len(set(hashes)) != len(hashes):
        raise ValueError("Manifeste présent deux fois dans le lot")
    aggregate = aggregate_sha256(hashes)
    body = [
        "DECLARE",
        "  v_import_id bigint;",
        "  v_existing timestamptz;",
        "  v_doc jsonb;",
        "  v_inserted integer := 0;",
        "  v_seen integer := 0;",
        "BEGIN",
        "  IF pg_catalog.to_regnamespace('geonaf') IS NULL",
        "     OR pg_catalog.obj_description(pg_catalog.to_regnamespace('geonaf'), 'pg_namespace')",
        f"        IS DISTINCT FROM {sql_literal('geonaf-environment:' + environment)} THEN",
        "    RAISE EXCEPTION 'GEONAF: environnement incorrect, import refuse';",
        "  END IF;",
        "  IF pg_catalog.to_regclass('geonaf.acquisition_manifests') IS NULL",
        "     OR pg_catalog.to_regclass('geonaf.data_imports') IS NULL THEN",
        "    RAISE EXCEPTION 'GEONAF: migration 0002 absente, import refuse';",
        "  END IF;",
        "  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n",
        "                 ON n.oid = e.extnamespace WHERE e.extname = 'postgis' AND n.nspname = 'extensions') THEN",
        "    RAISE EXCEPTION 'GEONAF: PostGIS absent du schema extensions, import refuse';",
        "  END IF;",
        "  PERFORM pg_catalog.set_config('search_path', 'pg_catalog, extensions, pg_temp', true);",
        "  PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('geonaf.observation_import'));",
        "  INSERT INTO geonaf.data_imports (scope, source_label, code_version)",
        f"    VALUES ('acquisitions', {sql_literal(label)}, {sql_literal(version)}) RETURNING import_id INTO v_import_id;",
    ]
    for record in rows:
        body += _record_lines(record)
    body += [
        "  UPDATE geonaf.data_imports SET finished_at = pg_catalog.now(), records_seen = v_seen,",
        "    records_inserted = v_inserted, records_existing = v_seen - v_inserted,",
        f"    fingerprints_sha256 = '{aggregate}' WHERE import_id = v_import_id;",
    ]
    if apply_sql:
        body.append("  RAISE NOTICE 'GEONAF acquisitions : % vus, % inseres, % existants', v_seen, v_inserted,"
                    " v_seen - v_inserted;")
    else:
        body.append("  RAISE EXCEPTION 'GEONAF SIMULATION REUSSIE, AUCUNE ECRITURE : % vus, % a inserer, % deja presents',"
                    " v_seen, v_inserted, v_seen - v_inserted;")
    body.append("END")
    text = "\n".join(body)
    tag = block_tag(text)
    mode = "APPLICATION" if apply_sql else "SIMULATION"
    return "\n".join([
        f"-- GEONAF - Supabase SQL Editor - import des acquisitions D3 ({mode}), genere localement ; relire avant execution.",
        f"-- {len(rows)} manifestes ; empreinte agregee SHA-256 : {aggregate}",
        "-- Aucun identifiant ni mot de passe. Une seule requete, sans BEGIN/COMMIT : toute erreur annule tout.",
        "-- " + ("Ecrit en cas de succes." if apply_sql else
                 "N'ecrit jamais : le bloc se termine par une erreur volontaire qui annule la requete."),
        SEARCH_PATH,
        "SELECT set_config('statement_timeout', '60s', true);",
        "SELECT set_config('lock_timeout', '10s', true);",
        f"DO {tag}",
        text,
        f"{tag};",
        "",
    ])


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="Hors du dépôt ; jamais écrasé.")
    parser.add_argument("--source-label", default="poste local")
    parser.add_argument("--environment", choices=("test", "dev"), default="test")
    parser.add_argument("--code-version")
    parser.add_argument("--apply-sql", action="store_true", help="Génère la variante APPLICATION (n'exécute rien)")
    args = parser.parse_args(argv)
    try:
        if not args.data_dir.is_dir():
            raise ValueError("Dossier source absent")
        if _inside(args.output, ROOT):
            raise ValueError("Fichier SQL dans le dépôt refusé : il contient les documents de manifeste")
        scan = FileAcquisitionCatalog(args.data_dir).scan()
        if scan.rejected:
            raise ValueError(f"Manifestes refusés : {len(scan.rejected)}")
        sql = generate_sql(scan.records, label=args.source_label,
                           version=args.code_version or code_version(),
                           environment=args.environment, apply_sql=args.apply_sql)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create: an existing SQL export is never overwritten, even in a race.
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(sql)
    except FileExistsError:
        print("Génération refusée : fichier SQL déjà existant (pas d'écrasement)", file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"Génération refusée : {error}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    print(f"SQL généré : {len(scan.records)} manifestes ; SHA-256 {digest}")
    print("Mode : APPLICATION" if args.apply_sql else "Mode : SIMULATION (aucune écriture)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
