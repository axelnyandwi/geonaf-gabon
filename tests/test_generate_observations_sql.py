"""scripts/generate_observations_sql.py: SQL Editor import of the acquisitions, generated offline (D3).

Static tests only: executing the generated SQL needs PostGIS (ST_GeomFromWKB), not available locally.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import generate_observations_sql as gen
from src.data.observations import AcquisitionRecord

ROOT = Path(__file__).resolve().parents[1]
TRANSACTION_CONTROL = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK|START\s+TRANSACTION)\s*;", re.M | re.I)
WKB = bytes.fromhex("0103000000010000000400000000000000000022400000000000000000000000000000244000000000000000"
                    "00000000000000244000000000000000000000000000002240000000000000000000")


def record(sha="a" * 64, **changes) -> AcquisitionRecord:
    value = AcquisitionRecord(
        collection="sentinel-2-l2a", product_id="S2B_MSIL2A_20250125", manifest_sha256=sha,
        acquired_at_utc=datetime(2025, 1, 25, 9, 31, 59, tzinfo=timezone.utc),
        ingested_at_utc=datetime(2026, 10, 20, 8, tzinfo=timezone.utc), stac_source="https://stac.example/item",
        grid_crs="EPSG:32632", grid_resolution_m=10.0, footprint_wkb=WKB, integrity_mode="strict",
        document={"schema_version": "geonaf.sentinel2-acquisition/v1", "product_id": "S2B_MSIL2A_20250125",
                  "collection": "sentinel-2-l2a"})
    return replace(value, **changes)


def generate(records=None, **kwargs):
    return gen.generate_sql(records or [record()], label=kwargs.pop("label", "poste local"),
                            version=kwargs.pop("version", "abc1234"), **kwargs)


def do_tag(sql):
    return re.search(r"^DO (\$gi[0-9a-f]+\$)$", sql, re.M).group(1)


def test_simulation_by_default_inside_the_block_and_no_transaction_control():
    sql = generate()
    assert not TRANSACTION_CONTROL.search(sql)
    tag = do_tag(sql)
    block = sql.split(f"DO {tag}", 1)[1]
    assert "RAISE EXCEPTION 'GEONAF SIMULATION REUSSIE, AUCUNE ECRITURE" in block   # a partial selection still aborts
    applied = generate(apply_sql=True)
    assert "SIMULATION" not in applied.split("DO ", 1)[1] and "RAISE NOTICE 'GEONAF acquisitions" in applied
    assert not TRANSACTION_CONTROL.search(applied)


def test_r5_search_path_first_and_guards_before_any_write():
    sql = generate()
    lines = [line for line in sql.splitlines() if line and not line.startswith("--")]
    assert lines[0] == gen.SEARCH_PATH
    first_write = sql.index("INSERT INTO")
    for guard in ("geonaf-environment:test", "migration 0002 absente", "PostGIS absent", "pg_advisory_xact_lock"):
        assert sql.index(guard) < first_write, guard
    assert sql.index("search_path', 'pg_catalog, extensions, pg_temp'") < sql.index("ST_GeomFromWKB")
    assert "geonaf-environment:dev" in generate(environment="dev")


def test_no_value_can_close_the_block():
    """A value containing the old fixed tag, or any $gi...$ tag, cannot end the dollar-quoted block."""
    hostile = "x$geonaf_import$; DROP SCHEMA geonaf CASCADE; --"
    sql = generate([record(document={**record().document, "note": hostile})], label="lab$geonaf_import$el")
    tag = do_tag(sql)
    assert sql.count(tag) == 2 and sql.rstrip().endswith(f"{tag};")
    body = sql.split(f"DO {tag}", 1)[1].rsplit(tag, 1)[0]
    planted = generate([record(document={**record().document, "note": tag})])
    assert do_tag(planted) != tag and planted.count(do_tag(planted)) == 2
    assert "DROP SCHEMA" in body          # Inside the block, as a quoted literal only.


def test_literals_are_escaped_and_nul_refused():
    sql = generate([record(stac_source="https://stac.example/it'em")])
    assert "'https://stac.example/it''em'" in sql
    with pytest.raises(ValueError, match="nul"):
        generate([record(stac_source="https://x\x00")])


@pytest.mark.parametrize("changes,message", [
    ({"acquired_at_utc": datetime(2025, 1, 25)}, "fuseau"),
    ({"ingested_at_utc": datetime(2024, 1, 1, tzinfo=timezone.utc)}, "antérieure"),
    ({"grid_resolution_m": float("nan")}, "Résolution"),
    ({"grid_resolution_m": 0}, "Résolution"),
    ({"grid_resolution_m": True}, "Résolution"),
    ({"manifest_sha256": "A" * 64}, "SHA-256"),
    ({"footprint_wkb": b""}, "WKB"),
])
def test_invalid_records_are_refused(changes, message):
    with pytest.raises(ValueError, match=message):
        generate([record(**changes)])


def test_batch_level_refusals():
    for kwargs, message in (({"label": "C:/chemin"}, "Libellé"), ({"label": " "}, "Libellé"),
                            ({"version": ""}, "Version"), ({"environment": "prod"}, "Environnement")):
        with pytest.raises(ValueError, match=message):
            generate(**kwargs)
    with pytest.raises(ValueError, match="deux fois"):
        generate([record(), record()])
    with pytest.raises(ValueError, match="Aucun"):
        gen.generate_sql([], label="x", version="y")


def test_missing_footprint_is_null_not_refused():
    sql = generate([record(footprint_wkb=None)])
    assert "ST_GeomFromWKB" not in sql and re.search(r"^\s+NULL,$", sql, re.M)


def test_aggregate_fingerprint_matches_the_importer():
    pytest.importorskip("psycopg")
    from src.data.postgis_observations import aggregate_sha256

    hashes = ["b" * 64, "a" * 64, "c" * 64]
    assert gen.aggregate_sha256(hashes) == aggregate_sha256(hashes)
    assert f"empreinte agregee SHA-256 : {aggregate_sha256(['a' * 64, 'b' * 64])}" in \
        generate([record("b" * 64), record("a" * 64, product_id="S2_OTHER")])


def test_importing_the_generator_loads_no_database_driver():
    """Indirect imports count too: the guardrail test only sees the generator's own import lines."""
    code = ("import sys; import scripts.generate_observations_sql; "
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('psycopg', 'psycopg_pool', 'sqlalchemy', "
            "'alembic') or m in ('src.data.database', 'src.data.postgis_session', 'src.data.postgis_observations', "
            "'src.data.postgis_territory_registry')); print(bad)")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "[]"


def test_cli_refuses_an_output_inside_the_repository_and_never_overwrites(tmp_path, capsys, monkeypatch):
    class Catalog:
        def __init__(self, _):
            pass

        def scan(self):
            return type("Scan", (), {"records": [record()], "rejected": []})()

    monkeypatch.setattr(gen, "FileAcquisitionCatalog", Catalog)
    data = tmp_path / "data"
    data.mkdir()
    assert gen.main(["--data-dir", str(data), "--output", str(ROOT / "import.sql"), "--code-version", "t"]) == 1
    assert "dans le dépôt" in capsys.readouterr().err and not (ROOT / "import.sql").exists()
    fresh = tmp_path / "out" / "import.sql"
    assert gen.main(["--data-dir", str(data), "--output", str(fresh), "--code-version", "t"]) == 0
    assert "SIMULATION" in capsys.readouterr().out and "SIMULATION REUSSIE" in fresh.read_text(encoding="utf-8")
    fresh.write_text("ancien", encoding="utf-8")
    assert gen.main(["--data-dir", str(data), "--output", str(fresh), "--code-version", "t"]) == 1
    assert "déjà existant" in capsys.readouterr().err and fresh.read_text(encoding="utf-8") == "ancien"


# --- Local PostgreSQL (marker postgis; PostGIS NOT needed): syntax and guard order, never Supabase ------

TEST_URL = __import__("os").environ.get("GEONAF_TEST_DATABASE_URL", "").strip()


@pytest.mark.postgis
def test_generated_sql_compiles_and_stops_at_the_first_failing_guard():
    """PL/pgSQL compiles the whole block before running it: a syntax error (or a value that escaped its
    literal) would surface as SyntaxError, not as the GEONAF guard."""
    if not TEST_URL:
        pytest.skip("GEONAF_TEST_DATABASE_URL non défini : base locale requise.")
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    url = "postgresql://" + TEST_URL.split("://", 1)[1]
    info = conninfo_to_dict(url)
    if not str(info.get("dbname", "")).endswith("_test") or info.get("host") not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("Base locale dont le nom se termine par « _test » exigée.")
    hostile = record(document={**record().document, "note": "x$geonaf_import$'; DROP SCHEMA public; --"})
    with psycopg.connect(url, autocommit=True) as c:
        if c.execute("SELECT 1 FROM pg_extension WHERE extname = 'postgis'").fetchone():
            pytest.skip("PostGIS présent : ce test vise l'arrêt sur garde-fou sans PostGIS.")
        c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")
        try:
            for apply_sql in (False, True):
                with pytest.raises(psycopg.errors.RaiseException, match="environnement incorrect"):
                    c.execute(generate([hostile], apply_sql=apply_sql).encode("utf-8"))
            c.execute("CREATE SCHEMA geonaf")
            c.execute("COMMENT ON SCHEMA geonaf IS 'geonaf-environment:test'")
            c.execute("CREATE TABLE geonaf.data_imports (import_id bigint)")
            c.execute("CREATE TABLE geonaf.acquisition_manifests (manifest_sha256 text)")
            with pytest.raises(psycopg.errors.RaiseException, match="PostGIS absent"):
                c.execute(generate([hostile]).encode("utf-8"))
            assert c.execute("SELECT count(*) FROM geonaf.data_imports").fetchone()[0] == 0
            assert c.execute("SELECT to_regnamespace('public') IS NOT NULL").fetchone()[0]
        finally:
            c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")
