"""SQL Editor scripts for Supabase (D2-a2): literals, migration wrapper, import batches, checks; offline.

No SQL is executed here (no database is reachable): these tests check what can be checked offline -
literals round-trip, every verified row reaches exactly one batch, the files stay in sync with the
canonical migration, read-only scripts hold no write, destructive scripts are guarded - and the parity
check of the CSV export against the file registry.
"""

import csv
import hashlib
import json
from pathlib import Path
import re
import sys

import pytest

from src.data import supabase_sql as s
from src.data.postgis_territory_registry import plan_import, version_row
from src.data.territory_registry import TerritoryRegistry, record_to_dict
from tests.test_postgis_territory_unit import real_registry_dir, registry  # noqa: F401  (registry is a fixture)

ROOT = Path(__file__).resolve().parents[1]
SUPABASE = ROOT / "db" / "supabase"
TRANSACTION_CONTROL = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK|START\s+TRANSACTION)\s*;", re.M | re.I)


def statements_outside_comments(text: str) -> str:
    return "\n".join(line.split("--")[0] for line in text.splitlines())


def code_only(text: str) -> str:
    """SQL without comments nor quoted literals (labels may name operations they only check)."""
    return re.sub(r"'(?:[^']|'')*'", "''", statements_outside_comments(text))


def dollar_quoted_documents(sql: str) -> list[dict]:
    return [json.loads(body) for _, body in re.findall(r"(\$g[0-9a-f]+\$)(.*?)\1::jsonb", sql, re.S)]


# --- Literals ------------------------------------------------------------------------------------


def test_literals_quote_and_round_trip():
    assert s.sql_text("Zone d'acquisition") == "'Zone d''acquisition'"
    with pytest.raises(ValueError):
        s.sql_text("a\x00b")
    assert s.sql_literal(None, "integer") == "NULL::integer"
    assert s.sql_literal(True, "boolean") == "true"
    with pytest.raises(TypeError):
        s.sql_literal(True, "integer")
    value = 2103.968624338096
    assert s.sql_literal(value, "float8") == "2103.968624338096::float8" and float(repr(value)) == value
    assert s.sql_literal(1e-07, "float8") == "1e-07::float8"
    with pytest.raises(ValueError):
        s.sql_literal(float("nan"), "float8")
    assert s.sql_literal([9.4, 0.4], "float8[]") == "ARRAY[9.4::float8, 0.4::float8]::float8[]"
    assert s.sql_literal([], "text[]") == "'{}'::text[]"
    assert s.sql_literal(["l'a"], "text[]") == "ARRAY['l''a']::text[]"


def test_dollar_quote_tag_never_occurs_in_the_content():
    content = "texte"
    first = s.dollar_quoted(content)
    tag = first[:first.index("$", 1) + 1]
    tricky = f"contient {tag} et $$ et '"
    quoted = s.dollar_quoted(tricky)
    other = quoted[:quoted.index("$", 1) + 1]
    assert other not in tricky and quoted == f"{other}{tricky}{other}"


def test_aggregate_fingerprint_matches_the_import_plan(registry):
    plan = plan_import(registry)
    assert s.fingerprints_sha256(r.fingerprint for r in plan.records) == plan.fingerprints_sha256


# --- Migration -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name, apply", [("02_migration_0001_simulation.sql", False),
                                         ("02_migration_0001_application.sql", True)])
def test_migration_files_are_in_sync_and_embed_the_canonical_body(name, apply):
    text = (SUPABASE / name).read_text(encoding="utf-8")
    assert text == s.migration_wrapper(apply=apply)          # Regenerate with: build_supabase_sql.py migration
    body = s.MIGRATION_0001.read_text(encoding="utf-8")
    assert body.rstrip() in text
    assert f"Body SHA-256: {hashlib.sha256(body.encode('utf-8')).hexdigest()}" in text
    assert not TRANSACTION_CONTROL.search(text)
    assert text.index(s.SEARCH_PATH) < text.index("CREATE SCHEMA geonaf")
    tail = text.rstrip().splitlines()[-1]
    assert tail.startswith("SELECT version") if apply else tail == "END $$;"
    assert ("SIMULATION DE MIGRATION REUSSIE" in text) is not apply


# --- Import batches ------------------------------------------------------------------------------


def test_every_verified_version_reaches_exactly_one_batch_in_order(registry):
    plan = plan_import(registry)
    for apply in (False, True):
        batches = s.import_batches(plan, source_label="registre de test", code_version="t", apply=apply, batch_size=2)
        found = []
        for text in batches:
            assert not TRANSACTION_CONTROL.search(text) and "\u2014" not in text
            documents = dollar_quoted_documents(text)
            found += [(d["territory_id"], d["version"]) for d in documents]
            ids = [d["territory_id"] for d in documents]
            assert ids == sorted(ids)
        assert found == [(r.territory_id, r.version) for r in plan.records]
        assert len(batches) == 3          # akanda (2 versions together), then pairs of single versions.


def test_batch_rows_round_trip_documents_and_geometries(registry):
    plan = plan_import(registry)
    text = s.import_batches(plan, source_label="registre", code_version="t", apply=True)[0]
    documents = dollar_quoted_documents(text)
    assert documents == [record_to_dict(r) for r in plan.records]
    for record, document in zip(plan.records, documents):
        assert hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest() == record.fingerprint
    hexes = re.findall(r"decode\('([0-9a-f]+)', 'hex'\)", text)
    assert sorted(bytes.fromhex(h) for h in hexes) == sorted(g.wkb for g in plan.geometries.values())
    assert list(s.VERSION_TYPES) == list(version_row(plan.records[0]))
    assert plan.fingerprints_sha256 in text


def test_simulation_and_application_differ_only_by_their_ending(registry):
    plan = plan_import(registry)
    simulation = s.import_batches(plan, source_label="r", code_version="t", apply=False)[0]
    application = s.import_batches(plan, source_label="r", code_version="t", apply=True)[0]
    body = "INSERT INTO geonaf.registry_imports (source_label, code_version)"
    assert simulation.split(body)[1].split("-- Simulation")[0] == application.split(body)[1].split("SELECT import_id")[0]
    assert simulation.rstrip().endswith("END $$;") and "SIMULATION REUSSIE, AUCUNE ECRITURE" in simulation
    assert "SIMULATION" not in application.split(body)[1]
    with pytest.raises(ValueError, match="chemin"):
        s.import_batches(plan, source_label="C:/data", code_version="t", apply=True)


@pytest.mark.skipif(real_registry_dir() is None, reason="Registre du paquet de démonstration absent.")
def test_real_registry_batches_carry_the_97_records_exactly():
    plan = plan_import(TerritoryRegistry(real_registry_dir()))
    batches = s.import_batches(plan, source_label="paquet v3", code_version="t", apply=True)
    documents = [d for text in batches for d in dollar_quoted_documents(text)]
    assert documents == [record_to_dict(r) for r in plan.records] and len(documents) == 97
    assert len(batches) == 4 and max(len(t.encode("utf-8")) for t in batches) < 300_000
    assert s.expected_intersecting(plan) == ["gab-gbhumanitarian-adm2-komo-mondah", "gab-gbopen-adm2-komo-mondah",
                                             "gab-gbopen-adm2-libreville"]


def test_verification_query_holds_the_expected_values(registry):
    plan = plan_import(registry)
    text = s.verify_import_sql(plan)
    assert text.count("UNION ALL") == 9 and plan.fingerprints_sha256 in text
    assert "'6'" in text and "'3'" in text                  # Versions, geometries / releases.
    assert s.expected_intersecting(plan, (9.39, 0.39, 9.41, 0.41)) == ["gab-gbhumanitarian-adm2-a",
                                                                        "gab-gbopen-adm2-a"]
    writes = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|GRANT|TRUNCATE)\b", re.I)
    assert not writes.search(code_only(text))


# --- Parity of the CSV export --------------------------------------------------------------------


def export_rows(plan):
    """Rows as the SQL Editor's CSV would give them (text everywhere, NULL as empty)."""
    rows = []
    for record in plan.records:
        row, source = version_row(record), record.source
        stored = plan.geometries[record.geometry_sha256]
        values = {**{c: row[c] for c in ("territory_id", "version", "kind", "label", "legal_status",
                                         "geometry_sha256", "file_sha256", "storage_key", "supersedes",
                                         "record_fingerprint", "retrieved_at_utc")},
                  "record_document": json.dumps(row["record_document"], ensure_ascii=False),
                  "source_id": source.source_id, "product": source.product, "source_version": source.version,
                  "licence_id": source.license_spdx, "attribution": source.attribution, "url": source.url,
                  "share_alike": "true" if source.share_alike else "false", "reference_year": source.reference_year,
                  "source_geometry_type": stored.source_geometry_type,
                  "wkb_sha256": hashlib.sha256(stored.wkb).hexdigest()}
        rows.append({k: "" if v is None else str(v) for k, v in values.items()})
    return rows


def test_export_parity_detects_any_difference(registry):
    plan = plan_import(registry)
    assert s.check_export_rows(export_rows(plan), plan) == []
    rows = export_rows(plan)
    rows[0]["record_document"] = rows[0]["record_document"].replace("Zone d'acquisition", "Zone modifiée")
    rows[1]["wkb_sha256"] = "0" * 64
    rows[2]["source_geometry_type"] = "MultiPolygon" if rows[2]["source_geometry_type"] == "Polygon" else "Polygon"
    del rows[3]
    problems = s.check_export_rows(rows, plan)
    assert len(problems) == 4 and any("refusée" in p for p in problems) and any("WKB" in p for p in problems)
    assert any("type géométrique" in p for p in problems) and any("absent de la base" in p for p in problems)


def test_verify_script_reads_the_csv_export(registry, tmp_path, capsys):
    plan = plan_import(registry)
    path = tmp_path / "export.csv"
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=s.EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(export_rows(plan))
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import verify_supabase_export as script
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    assert script.main(["--csv", str(path), "--data-dir", str(registry.data_dir)]) == 0
    assert "Parité confirmée" in capsys.readouterr().out


# --- Static scripts ------------------------------------------------------------------------------


def test_static_scripts_are_single_requests_and_read_only_ones_never_write():
    scripts = sorted(SUPABASE.glob("*.sql"))
    assert [p.name for p in scripts] == [
        "00_preflight.sql", "01_roles.sql", "02_migration_0001_application.sql", "02_migration_0001_simulation.sql",
        "03_mark_environment_dev.sql", "03_mark_environment_test.sql", "04_verify_schema.sql",
        "07_export_parity.sql", "08_inspect_legacy_schema.sql", "09_rename_legacy_schema_application.sql",
        "09_rename_legacy_schema_simulation.sql", "10_security_precheck.sql", "90_test_constraints.sql",
        "99_drop_schema_test.sql"]
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        assert not TRANSACTION_CONTROL.search(text) and "\u2014" not in text
        assert not re.search(r"PASSWORD", statements_outside_comments(text), re.I)
    writes = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|GRANT|REVOKE|TRUNCATE|COMMENT|LOCK|SET\s+ROLE)\b",
                        re.I)
    for name in ("00_preflight.sql", "04_verify_schema.sql", "07_export_parity.sql", "08_inspect_legacy_schema.sql",
                 "10_security_precheck.sql"):
        assert not writes.search(code_only((SUPABASE / name).read_text(encoding="utf-8"))), name


READ_ONLY_GUARD = "SELECT set_config('transaction_read_only', 'on', true);"


@pytest.mark.parametrize("name", ["08_inspect_legacy_schema.sql", "10_security_precheck.sql"])
def test_d2_2_diagnostics_enforce_read_only_and_identify_the_server(name):
    """Read only is enforced by PostgreSQL (first statement), not only by review; the server is identified."""
    statements = [s.strip() for s in statements_outside_comments((SUPABASE / name).read_text(encoding="utf-8"))
                  .split(";\n") if s.strip()]
    assert statements[0] + ";" == READ_ONLY_GUARD
    text = (SUPABASE / name).read_text(encoding="utf-8")
    assert re.search(r"'transaction en lecture seule'( AS verification)?, 'on'", text)
    assert "pg_control_system()" in text and "md5(" in text    # Hashed server identifier, never the raw value.
    assert "has_function_privilege('pg_control_system()', 'EXECUTE')" in text


def test_no_script_reads_a_setting_that_may_hold_a_secret():
    """Role and database settings are printed only for search_path and pgrst.db_schemas (pgrst.jwt_secret...)."""
    for path in sorted(SUPABASE.glob("*.sql")):
        code = statements_outside_comments(path.read_text(encoding="utf-8"))
        prefixes = set(re.findall(r"strpos\(s\.setting, '([^']+)'\) = 1", code))
        assert prefixes <= {"search_path=", "pgrst.db_schemas="}, (path.name, prefixes)
        assert not re.search(r"jwt|app\.settings|vault\.|decrypted_secrets|current_setting\('pgrst", code, re.I), path.name


def test_d2_2_ownership_and_rls_checks_are_exact():
    """Owner rights need inheritance (USAGE, not MEMBER); FORCE ROW LEVEL SECURITY binds the owner too."""
    for name in ("08_inspect_legacy_schema.sql", "09_rename_legacy_schema_application.sql",
                 "09_rename_legacy_schema_simulation.sql"):
        code = code_only((SUPABASE / name).read_text(encoding="utf-8"))
        assert "MEMBER" not in code, name
        assert "relforcerowsecurity" in code, name
    statements = statements_outside_comments((SUPABASE / "09_rename_legacy_schema_application.sql")
                                             .read_text(encoding="utf-8"))
    assert statements.index("IN SHARE ROW EXCLUSIVE MODE NOWAIT") < statements.index("ALTER SCHEMA")


LEGACY_TABLES = ("acquisitions,analysis_acquisitions,analysis_runs,indicator_definitions,indicator_results,sources,"
                 "territories,territory_versions")


def test_legacy_rename_is_guarded_never_drops_and_its_simulation_writes_nothing():
    """09 renames only the exact legacy schema seen on 2026-11-03; the simulation differs only by its ending."""
    application = (SUPABASE / "09_rename_legacy_schema_application.sql").read_text(encoding="utf-8")
    simulation = (SUPABASE / "09_rename_legacy_schema_simulation.sql").read_text(encoding="utf-8")
    inventory = (SUPABASE / "08_inspect_legacy_schema.sql").read_text(encoding="utf-8")
    code = code_only(application)
    assert not re.search(r"\b(DROP|DELETE|TRUNCATE|INSERT|UPDATE|GRANT|REVOKE)\b", code, re.I)
    assert re.findall(r"\bALTER\b[^;]*;", code) == ["ALTER SCHEMA geonaf RENAME TO geonaf_legacy_draft;"]
    statements = statements_outside_comments(application)
    guard_end = statements.index("END $$;")
    for check in ("IS DISTINCT FROM expected", "obj_description(legacy, 'pg_namespace') IS NOT NULL",
                  "row_count > 0", "cardinality(blockers) > 0", "to_regnamespace('geonaf_legacy_draft') IS NOT NULL"):
        assert statements.index(check) < guard_end < statements.index("ALTER SCHEMA"), check
    assert f"'{LEGACY_TABLES}'" in application and f"'{LEGACY_TABLES}'" in inventory
    marker = "-- ===== Result ====="
    assert application.split(marker)[0].splitlines()[1:] == simulation.split(marker)[0].splitlines()[1:]
    ending = simulation.split(marker)[1]
    assert "RAISE EXCEPTION 'GEONAF SIMULATION DE RENOMMAGE REUSSIE, AUCUNE ECRITURE'" in ending
    assert simulation.rstrip().endswith("END $$;")
    # The renamed schema must never pass the D2.1 environment guard.
    assert "COMMENT ON SCHEMA geonaf_legacy_draft IS 'geonaf-legacy:" in application
    assert "geonaf-environment:" not in code_only(application)


def test_destructive_scripts_are_guarded_and_tests_always_cancel():
    guard = "'geonaf-environment:test'"
    for name, first_write in (("90_test_constraints.sql", "INSERT INTO"), ("99_drop_schema_test.sql", "DROP SCHEMA")):
        text = statements_outside_comments((SUPABASE / name).read_text(encoding="utf-8"))
        assert text.index(guard) < text.index(first_write), name
    tests = (SUPABASE / "90_test_constraints.sql").read_text(encoding="utf-8").rstrip()
    final = tests[tests.rindex("DO $$"):]
    assert final.count("RAISE EXCEPTION") == 2 and final.endswith("END $$;")
    assert len(re.findall(r"SELECT pg_temp\.expect\(", tests)) == 21
    marks = {(SUPABASE / f"03_mark_environment_{e}.sql").read_text(encoding="utf-8") for e in ("dev", "test")}
    assert all("COMMENT ON SCHEMA geonaf" in m for m in marks)


def test_build_script_writes_import_files_outside_the_repository_only(registry, tmp_path, capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import build_supabase_sql as script
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    base = ["import", "--data-dir", str(registry.data_dir), "--source-label", "registre de test"]
    assert script.main(base + ["--output-dir", str(ROOT / "db" / "generated")]) == 2
    assert not (ROOT / "db" / "generated").exists()
    out = tmp_path / "supabase"
    assert script.main(base + ["--output-dir", str(out), "--code-version", "t"]) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["records"] == 6 and manifest["fingerprints_sha256"] == plan_import(registry).fingerprints_sha256
    for name, digest in manifest["files_sha256"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert sorted(manifest["files_sha256"]) == ["05_import_lot_01_application.sql", "05_import_lot_01_simulation.sql",
                                                "06_verify_import.sql"]
    assert str(registry.data_dir) not in capsys.readouterr().out
