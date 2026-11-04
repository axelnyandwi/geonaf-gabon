"""Non-regression for PostgreSQL error 42725 (operator is not unique) on catalog columns of type "char".

``text || "char"`` and ``unknown || "char"`` are ambiguous in PostgreSQL (``text || text``, ``text || anynonarray``
and ``anynonarray || text`` all apply): the whole request fails at parse time. 10_security_precheck.sql line 32
(``d.defaclobjtype || ':'``) failed this way. Every "char" operand of ``||`` must be cast (``::text``);
``concat_ws``, ``format`` and comparisons (``relkind = 'r'``) are not affected.

- Static check, always run: every SQL text tracked by Git (scripts, docs, SQL held in Python or PowerShell).
- Local database (marker ``postgis``, ``GEONAF_TEST_DATABASE_URL``): every query of the read-only catalog
  scripts goes through EXPLAIN (parse, type resolution and planning, no execution), and 10_security_precheck.sql
  is run for real. Never against Supabase.
"""

import os
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Every catalog column of type "char" (pg_attribute.atttypid = '"char"'::regtype in pg_catalog, PostgreSQL 16).
CHAR_COLUMNS = (
    "aggfinalmodify aggkind aggmfinalmodify amoppurpose amtype attalign attcompression attgenerated attidentity "
    "attstorage castcontext castmethod collprovider confdeltype confmatchtype confupdtype contype datlocprovider "
    "defaclobjtype deptype ev_enabled ev_type evtenabled oprkind partstrat polcmd privtype prokind proparallel "
    "provolatile relkind relpersistence relreplident srsubstate substream subtwophasestate tgenabled typalign "
    "typcategory typdelim typstorage typtype").split()
_COLUMN = r"(?:\w+\.)?(?:" + "|".join(CHAR_COLUMNS) + r")\b"
UNCAST_CHAR_CONCAT = re.compile(rf"\|\|\s*{_COLUMN}(?!\s*::)|(?<![\w.]){_COLUMN}\s*\|\|", re.I)
READ_ONLY_CATALOG_SCRIPTS = (
    "db/supabase/00_preflight.sql", "db/supabase/04_verify_schema.sql", "db/supabase/08_inspect_legacy_schema.sql",
    "db/supabase/09_legacy_schema_fingerprint.sql", "db/supabase/10_security_precheck.sql",
    "db/supabase/10_verify_legacy_schema_rename.sql", "scripts/db_backup/legacy_structure.sql",
    "docs/sql/inspect_geonaf_schema.sql")


def tracked(*patterns: str) -> list[Path]:
    names = subprocess.run(["git", "ls-files", *patterns], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / name for name in names.split()]


def test_the_pattern_catches_the_42725_forms_and_spares_the_safe_ones():
    for bad in ("d.defaclobjtype || ':'", "':' || d.defaclobjtype", "' (' || attidentity || ')'", "k.contype || ' '",
                "x ||\n   r.relkind"):
        assert UNCAST_CHAR_CONCAT.search(bad), bad
    for good in ("d.defaclobjtype::text || ':'", "':' || d.defaclobjtype::text", "k.contype::text || ' '",
                 "concat_ws('|', r.relkind)", "relkind = 'r'", "'rls=' || r.relrowsecurity::text", "x.relkindx || 'a'"):
        assert not UNCAST_CHAR_CONCAT.search(good), good


@pytest.mark.parametrize("path", tracked("*.sql", "*.py", "*.ps1"), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_char_column_is_concatenated_without_a_cast(path):
    if path == Path(__file__).resolve():
        return                                     # This file quotes the bad forms on purpose.
    text = path.read_text(encoding="utf-8", errors="replace")
    found = [(text.count("\n", 0, m.start()) + 1, m.group(0)) for m in UNCAST_CHAR_CONCAT.finditer(text)]
    assert found == [], found


def test_security_precheck_line_32_casts_defaclobjtype():
    text = (ROOT / "db" / "supabase" / "10_security_precheck.sql").read_text(encoding="utf-8")
    assert "d.defaclrole::regrole::text || ':' || d.defaclobjtype::text || ':'" in text


# --- Local database: EXPLAIN every query, run 10_security_precheck.sql ------------------------------------------

TEST_URL = os.environ.get("GEONAF_TEST_DATABASE_URL", "").strip()
needs_db = pytest.mark.skipif(not TEST_URL, reason="GEONAF_TEST_DATABASE_URL non défini : base locale requise.")


def statements(sql: str) -> list[str]:
    """Top-level statements, respecting quotes, dollar quotes and comments."""
    out, current, i, quote, dollar = [], [], 0, False, None
    while i < len(sql):
        ch = sql[i]
        if dollar:
            if sql.startswith(dollar, i):
                current.append(dollar); i += len(dollar); dollar = None; continue
        elif quote:
            if ch == "'":
                quote = False
        elif sql.startswith("--", i):
            i = sql.find("\n", i) if "\n" in sql[i:] else len(sql); continue
        elif ch == "'":
            quote = True
        elif ch == "$":
            m = re.match(r"\$\w*\$", sql[i:])
            if m:
                dollar = m.group(0); current.append(dollar); i += len(dollar); continue
        elif ch == ";":
            out.append("".join(current).strip()); current = []; i += 1; continue
        current.append(ch)
        i += 1
    if "".join(current).strip():
        out.append("".join(current).strip())
    return [s for s in out if s]


def conninfo() -> str:
    return "postgresql://" + TEST_URL.split("://", 1)[1] if TEST_URL.startswith("postgresql+psycopg://") else TEST_URL


@pytest.fixture
def local():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(conninfo())
    if not str(info.get("dbname", "")).endswith("_test") or info.get("host") not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("Base locale dont le nom se termine par « _test » exigée.")
    with psycopg.connect(conninfo()) as connection:
        yield connection
        connection.rollback()


@pytest.mark.postgis
@needs_db
@pytest.mark.parametrize("name", READ_ONLY_CATALOG_SCRIPTS)
def test_every_query_of_the_read_only_scripts_resolves_its_types(local, name):
    """EXPLAIN parses and plans without executing: a 42725 shows here, nothing is read or written."""
    import psycopg

    queries = [s for s in statements((ROOT / name).read_text(encoding="utf-8")) if re.match(r"(SELECT|WITH)\b", s, re.I)]
    assert queries, name
    local.execute("SET LOCAL transaction_read_only = on")
    for query in queries:
        try:
            local.execute(("EXPLAIN (COSTS OFF) " + query).encode("utf-8"))
        except psycopg.errors.AmbiguousFunction as error:          # SQLSTATE 42725.
            pytest.fail(f"{name}: 42725 {error.diag.message_primary}")
        except psycopg.errors.UndefinedTable:                     # A schema of the deployment absent locally.
            local.rollback()
            local.execute("SET LOCAL transaction_read_only = on")


@pytest.mark.postgis
@needs_db
def test_security_precheck_runs_as_one_read_only_request(local):
    """The whole file, as the SQL Editor sends it: one implicit transaction made read-only by its first line."""
    local.rollback()
    local.autocommit = True
    cursor = local.execute((ROOT / "db" / "supabase" / "10_security_precheck.sql").read_text(encoding="utf-8").encode("utf-8"))
    while cursor.nextset():
        pass
    rows = cursor.fetchall()
    columns = [d.name for d in cursor.description]
    assert columns[:4] == ["ordre", "verification", "attendu", "obtenu"] or "verification" in columns
    by_label = {r[columns.index("verification")]: r for r in rows}
    assert by_label["transaction en lecture seule"][columns.index("obtenu")] == "on"
    line_32 = [r for r in rows if str(r[columns.index("verification")]).startswith("droits par defaut globaux")]
    assert len(line_32) == 1
