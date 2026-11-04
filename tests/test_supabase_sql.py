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


def test_r5_search_path_never_reaches_public_and_lists_pg_temp_last():
    """R5: scripts that resolve PostGIS names use pg_catalog, extensions, then pg_temp (last, explicit)."""
    assert s.SEARCH_PATH == "SELECT set_config('search_path', 'pg_catalog, extensions, pg_temp', true);"
    for name in ("02_migration_0001_application.sql", "02_migration_0001_simulation.sql",
                 "07_export_parity.sql", "90_test_constraints.sql"):
        text = (SUPABASE / name).read_text(encoding="utf-8")
        assert s.SEARCH_PATH in text and "public, extensions" not in statements_outside_comments(text), name


def test_r5_generated_import_and_verification_set_the_search_path(registry):
    plan = plan_import(registry)
    batch = s.import_batches(plan, source_label="registre", code_version="t", apply=True)[0]
    assert batch.lstrip().startswith("--") and s.SEARCH_PATH in batch
    assert batch.index(s.SEARCH_PATH) < batch.index("CREATE TEMP TABLE")
    verify = s.verify_import_sql(plan)
    assert verify.index(s.SEARCH_PATH) < verify.index("SELECT verification")


# --- Non-regression: search path and PostGIS references (D2.7) -----------------------------------

BODY_0001_SHA256 = "719e7e847f28404255491c0c00923f130cbd35151cd63488ed1ba3391c0bf971"   # Reviewed body, D2-a.
POSTGIS_USE = re.compile(r"\b(ST_\w+|geometrytype)\s*\(|::\s*geometry\b|\bgeometry\s*\(", re.I)
SEARCH_PATH_SETTING = re.compile(r"set_config\(\s*'search_path'\s*,\s*'([^']*)'|SET\s+search_path\s*(?:=|TO)\s*('[^']*'|[^;\n]+)",
                                 re.I)


def test_migration_0001_body_is_the_reviewed_one():
    """Any change to the body of 0001 (beyond line endings) must be a new reviewed migration, not an edit."""
    body = s.MIGRATION_0001.read_text(encoding="utf-8")
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == BODY_0001_SHA256
    for name in ("02_migration_0001_application.sql", "02_migration_0001_simulation.sql"):
        text = (SUPABASE / name).read_text(encoding="utf-8")
        assert f"-- Body SHA-256: {BODY_0001_SHA256}" in text
        assert f"'0001_territorial_registry', '{BODY_0001_SHA256}'" in text


def without_documents(sql: str) -> str:
    """Drop the dollar-quoted JSON documents of the import batches: their apostrophes (« zone d'acquisition »)
    would otherwise unbalance the quoted-literal stripping of code_only and hide real code."""
    return re.sub(r"(\$g[0-9a-f]+\$).*?\1", "''", sql, flags=re.S)


def _supabase_requests(registry) -> dict[str, str]:
    """Every request pasted in the SQL Editor: static scripts and the generated import and verification."""
    plan = plan_import(registry)
    requests = {p.name: p.read_text(encoding="utf-8") for p in sorted(SUPABASE.glob("*.sql"))}
    for apply in (False, True):
        for number, batch in enumerate(s.import_batches(plan, source_label="registre", code_version="t",
                                                        apply=apply), 1):
            requests[f"05_lot_{number:02d}_{'application' if apply else 'simulation'}"] = batch
    requests["06_verify_import"] = s.verify_import_sql(plan)
    return requests


def test_every_request_using_postgis_sets_the_r5_path_first(registry):
    """A request that resolves a PostGIS name sets pg_catalog, extensions, pg_temp before the first use."""
    users = []
    for name, text in _supabase_requests(registry).items():
        code = code_only(without_documents(text))
        first = POSTGIS_USE.search(code)
        if first is None:
            continue
        users.append(name)
        assert s.SEARCH_PATH in text, name
        assert code.index(code_only(s.SEARCH_PATH)) < first.start(), name   # Literals blanked in both.
    assert {"02_migration_0001_application.sql", "07_export_parity.sql", "90_test_constraints.sql",
            "05_lot_01_application", "06_verify_import"} <= set(users)


def test_no_request_ever_searches_public_or_puts_pg_temp_first(registry):
    """Only two search paths exist in the scripts: the R5 path, and the empty path of GEONAF functions."""
    for name, text in _supabase_requests(registry).items():
        for match in SEARCH_PATH_SETTING.finditer(statements_outside_comments(without_documents(text))):
            value = (match.group(1) if match.group(1) is not None else match.group(2)).strip().strip("'")
            assert value in ("pg_catalog, extensions, pg_temp", ""), (name, value)


def test_python_postgis_calls_are_inventoried_until_r5_bis():
    """R5 bis is not implemented: the Python layer runs these PostGIS calls with the connection's default
    search path (which holds public on Supabase). A new call must update this list and the R5 bis note."""
    source = (ROOT / "src" / "data" / "postgis_territory_registry.py").read_text(encoding="utf-8")
    assert set(re.findall(r"\b(ST_\w+)\(", source)) == {"ST_Intersects", "ST_MakeEnvelope", "ST_AsBinary",
                                                         "ST_GeomFromWKB"}
    assert "search_path" not in source      # Turns red when R5 bis lands: then replace this test by its own.


def test_static_scripts_are_single_requests_and_read_only_ones_never_write():
    scripts = sorted(SUPABASE.glob("*.sql"))
    assert [p.name for p in scripts] == [
        "00_preflight.sql", "01_roles.sql", "02_migration_0001_application.sql", "02_migration_0001_simulation.sql",
        "03_mark_environment_dev.sql", "03_mark_environment_test.sql", "04_verify_schema.sql",
        "07_export_parity.sql", "08_inspect_legacy_schema.sql", "09_legacy_schema_fingerprint.sql",
        "09_rename_legacy_schema_application.sql", "09_rename_legacy_schema_rollback.sql",
        "09_rename_legacy_schema_simulation.sql", "10_security_precheck.sql", "10_verify_legacy_schema_rename.sql",
        "90_test_constraints.sql", "99_drop_schema_test.sql"]
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        assert not TRANSACTION_CONTROL.search(text) and "\u2014" not in text
        assert not re.search(r"PASSWORD", statements_outside_comments(text), re.I)
    writes = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|GRANT|REVOKE|TRUNCATE|COMMENT|LOCK|SET\s+ROLE)\b",
                        re.I)
    for name in ("00_preflight.sql", "04_verify_schema.sql", "07_export_parity.sql", "08_inspect_legacy_schema.sql",
                 "09_legacy_schema_fingerprint.sql", "10_security_precheck.sql", "10_verify_legacy_schema_rename.sql"):
        assert not writes.search(code_only((SUPABASE / name).read_text(encoding="utf-8"))), name


READ_ONLY_GUARD = "SELECT set_config('transaction_read_only', 'on', true);"


IDENTITY_SCRIPTS = ["08_inspect_legacy_schema.sql", "10_security_precheck.sql"]


@pytest.mark.parametrize("name", IDENTITY_SCRIPTS)
def test_d2_2_diagnostics_enforce_read_only(name):
    """Read only is enforced by PostgreSQL (first statement), not only by review."""
    statements = [s.strip() for s in statements_outside_comments((SUPABASE / name).read_text(encoding="utf-8"))
                  .split(";\n") if s.strip()]
    assert statements[0] + ";" == READ_ONLY_GUARD
    text = (SUPABASE / name).read_text(encoding="utf-8")
    assert re.search(r"'transaction en lecture seule'( AS verification)?, 'on'", text)


def test_no_script_presents_the_postgresql_system_identifier_as_a_project_identity():
    """D2.4: geonaf-test and geonaf-dev returned the same system identifier (32f20b4465de, 2026-11-04) while
    their instances had started 41 minutes apart: Supabase projects share it. It must never come back as an
    identity, hashed or not."""
    for path in sorted(SUPABASE.glob("*.sql")):
        code = code_only(path.read_text(encoding="utf-8"))
        assert not re.search(r"pg_control_system|system_identifier", code, re.I), path.name
        assert not re.search(r"md5\(", code, re.I), path.name


@pytest.mark.parametrize("name", IDENTITY_SCRIPTS)
def test_identity_lines_state_their_limits_and_carry_the_instance_evidence(name):
    """The project identity is read in the dashboard; SQL only gives the running instance and the run time."""
    text = (SUPABASE / name).read_text(encoding="utf-8")
    identity = re.search(r"SELECT -4, '([^']|'')*'", text).group(0)
    assert "aucune valeur SQL fiable" in identity and "URL du tableau de bord" in identity
    assert "'a relever manuellement'" in text
    start = re.search(r"SELECT -3, '([^']|'')*'", text).group(0)
    assert "change a chaque redemarrage" in start and "ne prouve pas l''identite" in start
    assert "pg_postmaster_start_time() AT TIME ZONE 'UTC'" in text           # Comparable across exports.
    assert "transaction_timestamp() AT TIME ZONE 'UTC'" in text               # Needed by criterion I1.
    # Identity lines are information only: they can never turn a check into conforme = true.
    for order in (-4, -3, -2, -1):
        line = re.search(rf"SELECT {order}, '(?:[^']|'')*',\s*'([^']*)'", text)
        assert line and line.group(1) == "information", (name, order)


@pytest.mark.parametrize("name", ["00_preflight.sql", "08_inspect_legacy_schema.sql", "10_security_precheck.sql"])
def test_information_lines_are_never_counted_as_validations(name):
    """conforme is NULL (neither true nor false) for every line whose expected value is 'information'."""
    code = re.sub(r"\s+", " ", statements_outside_comments((SUPABASE / name).read_text(encoding="utf-8")))
    assert code.count("CASE WHEN attendu = 'information' THEN NULL ELSE attendu = obtenu END AS conforme") == 1
    assert "AS conforme" in code and code.count("AS conforme") == 1    # No second, unguarded verdict column.


def test_security_precheck_reports_its_phase_for_an_early_read_only_run():
    text = (SUPABASE / "10_security_precheck.sql").read_text(encoding="utf-8")
    for phase in ("avant renommage (09 non applique) : lignes 2 et 3 attendues fausses",
                  "apres renommage, avant 0001", "0001 appliquee, marque absente", "0001 appliquee et marquee",
                  "roles GEONAF absents (01 non applique) : lignes 20 et 21 attendues fausses"):
        assert phase in text, phase


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


# --- 09: legacy schema rename, simulation, rollback and fingerprint (D2.9) ----------------------------

RENAME_SCRIPTS = ("09_rename_legacy_schema_simulation.sql", "09_rename_legacy_schema_application.sql",
                  "09_rename_legacy_schema_rollback.sql")
# Text that would write if a user selected it alone and pressed Run. The scripts are scanned as raw text,
# comments and literals included: any contiguous selection is a substring of the file, so a pattern absent
# from the whole file is absent from every selection. Writes are only built at run time by format('... %I').
RUNNABLE_WRITES = {
    "rename": re.compile(r"\bALTER\s+SCHEMA\s+(?!%I\b)", re.I),
    "comment": re.compile(r"\bCOMMENT\s+ON\s+(?!SCHEMA\s+%I\b)", re.I),
    "select into (CREATE TABLE AS outside PL/pgSQL)": re.compile(r"\bSELECT\b[^;']*?\bINTO\b", re.I),
    "other write": re.compile(
        r"\b(CREATE|DROP)\s+(OR\s+REPLACE\s+)?(TABLE|SCHEMA|VIEW|FUNCTION|PROCEDURE|INDEX|ROLE|USER|TEMP|TEMPORARY"
        r"|UNLOGGED|SEQUENCE|TRIGGER|EXTENSION|POLICY|TYPE|DOMAIN|MATERIALIZED|EVENT|RULE|PUBLICATION)\b"
        r"|\bINSERT\s+INTO\b|\bUPDATE\s+[\w.\"]+\s+SET\b|\bDELETE\s+FROM\b|\bTRUNCATE\b"
        r"|\b(GRANT|REVOKE)\s+\w+[^;]*?\b(ON|TO|FROM)\b|\bCOMMIT\b|\bSET\s+ROLE\b", re.I),
}
# The integration test (tests/test_supabase_legacy_rename_integration.py) is the stronger proof: it runs
# every contiguous line range of these files on a real PostgreSQL and checks that none changes the state.
CONFIRMATION = "confirmation constant text := 'AAAA-MM-JJ HH:MM:SS.UUUUUU';"
START_FORMAT = "to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')"


def _region(text: str, title: str) -> str:
    start = text.index(f"-- ===== {title}")
    return text[start:text.index("-- ===== End of", start)]


def _statements(text: str) -> list[str]:
    """Top-level statements (comments removed); a DO body counts as one statement."""
    code, parts, depth_dollar = statements_outside_comments(text), [], False
    current = ""
    for chunk in re.split(r"(\$\$|;)", code):
        current += chunk
        if chunk == "$$":
            depth_dollar = not depth_dollar
        elif chunk == ";" and not depth_dollar:
            parts.append(current.strip())
            current = ""
    assert not current.strip(), "text after the last statement"
    return [p for p in parts if p != ";"]


@pytest.mark.parametrize("name", RENAME_SCRIPTS)
def test_no_selection_of_a_09_script_holds_a_runnable_write(name):
    """Partial selection in the SQL Editor (D2.9): no substring of the file is a write statement."""
    text = (SUPABASE / name).read_text(encoding="utf-8")
    for label, pattern in RUNNABLE_WRITES.items():
        assert not pattern.search(text), (name, label, pattern.search(text))


def test_09_scripts_are_one_do_statement_plus_at_most_a_read_only_select():
    simulation = _statements((SUPABASE / RENAME_SCRIPTS[0]).read_text(encoding="utf-8"))
    assert len(simulation) == 1 and simulation[0].startswith("DO $$") and simulation[0].endswith("END $$;")
    for name in RENAME_SCRIPTS[1:]:
        statements = _statements((SUPABASE / name).read_text(encoding="utf-8"))
        assert len(statements) == 2 and statements[0].startswith("DO $$"), name
        assert statements[1].startswith("SELECT ") and not re.search(r"\bINTO\b|set_config", statements[1]), name


def test_simulation_and_application_share_the_guard_character_for_character():
    simulation, application, rollback = ((SUPABASE / n).read_text(encoding="utf-8") for n in RENAME_SCRIPTS)
    for title in ("Constants", "Guard variables", "Guard (identical"):
        assert _region(simulation, title) == _region(application, title), title
    assert _region(rollback, "Constants") == _region(simulation, "Constants")
    guard = _region(application, "Guard (identical")
    for check in ("found_tables IS DISTINCT FROM expected", "initial_comment IS NOT NULL", "n_rows > 0",
                  "cardinality(blockers) > 0", "to_regnamespace(new_name) IS NOT NULL",
                  "has_database_privilege(current_database(), 'CREATE')", "IN SHARE ROW EXCLUSIVE MODE NOWAIT"):
        assert check in guard, check
    # The table list is read again under the locks (a table created in between is seen).
    assert guard.count("found_tables IS DISTINCT FROM expected") == 2
    assert guard.index("NOWAIT") < guard.rindex("found_tables IS DISTINCT FROM expected")
    for name, text in zip(RENAME_SCRIPTS, (simulation, application, rollback)):
        assert "MEMBER" not in code_only(text) and "geonaf-environment:" not in text, name
    assert f"'{LEGACY_TABLES}'" in simulation
    assert f"'{LEGACY_TABLES}'" in (SUPABASE / "08_inspect_legacy_schema.sql").read_text(encoding="utf-8")


def test_simulation_rolls_back_its_rename_twice_and_catches_only_its_own_signal():
    text = (SUPABASE / RENAME_SCRIPTS[0]).read_text(encoding="utf-8")
    action = text[text.index("-- ===== Simulation:"):]
    assert action.index("BEGIN") < action.index("format('ALTER SCHEMA %I RENAME TO %I'")
    assert action.index("RAISE EXCEPTION USING ERRCODE = 'GN009'") < action.index("EXCEPTION WHEN SQLSTATE 'GN009' THEN")
    assert action.count("EXCEPTION WHEN") == 1 and "WHEN OTHERS" not in text
    # The flag is set right before the internal signal and checked after the handler: a GN009 raised
    # elsewhere (an event trigger) can never read as a successful simulation.
    assert action.index("rename_tested := true;") < action.index("RAISE EXCEPTION USING ERRCODE = 'GN009'")
    assert action.index("format('COMMENT ON SCHEMA %I IS %L'") < action.index("rename_tested := true;")
    assert action.index("EXCEPTION WHEN SQLSTATE 'GN009'") < action.index("IF NOT rename_tested THEN")
    assert "rename_tested := true" not in _region(text, "Guard (identical")
    assert "ETAT NON RESTAURE" in action                         # Checked after the subtransaction rollback.
    assert action.rstrip().endswith("END $$;")
    assert "RAISE EXCEPTION 'GEONAF SIMULATION DE RENOMMAGE REUSSIE, AUCUNE ECRITURE'" in action
    assert CONFIRMATION not in text                               # Nothing to edit in the simulation.


@pytest.mark.parametrize("name", RENAME_SCRIPTS[1:])
def test_application_and_rollback_refuse_to_run_until_the_instance_is_confirmed(name):
    """The committed file always stops first: the operator types the start time read in 08 line -3."""
    text = (SUPABASE / name).read_text(encoding="utf-8")
    assert text.count(CONFIRMATION) == 1
    body = text[text.index("BEGIN\n"):]
    assert body.index(f"IF confirmation IS DISTINCT FROM {START_FORMAT} THEN") < body.index("RAISE EXCEPTION")
    assert body.index("REFUSE") < body.index("format('ALTER SCHEMA")
    # Same value and format as the line -3 of 08 (and of the fingerprint), so it can be copied as is.
    for reference in ("08_inspect_legacy_schema.sql", "09_legacy_schema_fingerprint.sql"):
        assert START_FORMAT in (SUPABASE / reference).read_text(encoding="utf-8"), reference


def test_application_mark_keeps_the_exact_initial_comment_and_the_rollback_restores_it():
    simulation, application, rollback = ((SUPABASE / n).read_text(encoding="utf-8") for n in RENAME_SCRIPTS)
    mark = "mark := mark_prefix || initial_separator || coalesce(to_jsonb(initial_comment)::text, 'null');"
    assert mark in _region(application, "Guard (identical") and mark in simulation
    assert "mark_prefix constant text := 'geonaf-legacy: " in rollback     # 10 l.3 checks this prefix.
    for step in ("substr(current_mark, length(mark_prefix || initial_separator) + 1)::jsonb",
                 "jsonb_typeof(initial_json) NOT IN ('null', 'string')", "initial_comment := initial_json #>> '{}'",
                 "format('COMMENT ON SCHEMA %I IS NULL', old_name)", "format('COMMENT ON SCHEMA %I IS %L', old_name, initial_comment)",
                 "format('ALTER SCHEMA %I RENAME TO %I', new_name, old_name)",
                 "to_regnamespace(old_name) IS NOT NULL"):    # Refuses once 0001 has created a new geonaf.
        assert step in rollback, step
    assert rollback.index("to_regnamespace(old_name) IS NOT NULL") < rollback.index("format('ALTER SCHEMA")


def test_legacy_fingerprint_is_read_only_deterministic_and_never_hashes_with_md5():
    text = (SUPABASE / "09_legacy_schema_fingerprint.sql").read_text(encoding="utf-8")
    statements = _statements(text)
    assert statements[0] == READ_ONLY_GUARD and statements[1] == "SELECT set_config('search_path', '', true);"
    assert len(statements) == 3 and "sha256(" in statements[2] and "md5" not in statements[2].lower()
    for column in ("oid_geonaf", "oid_geonaf_legacy_draft", "commentaire_geonaf_json", "empreinte_sha256"):
        assert f"AS {column}" in statements[2], column
    for part in ("'schema'", "'relation'", "'column'", "'constraint'", "'index'", "'policy'", "'trigger'"):
        assert part in statements[2], part


def test_09_docs_never_show_a_runnable_rename_back():
    """The old one-line reverse kept the legacy mark on geonaf: it must not be copied from the docs again."""
    for doc in ("supabase_legacy_schema_plan.md", "supabase_security_d2_2.md"):
        text = (ROOT / "docs" / doc).read_text(encoding="utf-8")
        assert not re.search(r"ALTER\s+SCHEMA\s+geonaf_legacy_draft\s+RENAME", text, re.I), doc
        assert "09_rename_legacy_schema_rollback.sql" in text, doc


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
