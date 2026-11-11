"""D2.10: offline parity of the acquisitions and ingestions exported from Supabase (script 14); no database.

The CSV rows are built here as the SQL Editor would give them (text everywhere, NULL as empty), from the
same strict manifests as the file catalogue. Every kind of divergence must be reported explicitly.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

import pytest

from src.data import acquisition_export as ae
from src.data.observations import FileAcquisitionCatalog
from tests.test_observations import acquisitions, write_product  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
SUPABASE = ROOT / "db" / "supabase"
READ_ONLY_GUARD = "SELECT set_config('transaction_read_only', 'on', true);"
R5_PATH = "SELECT set_config('search_path', 'pg_catalog, extensions, pg_temp', true);"
WRITES = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|GRANT|REVOKE|TRUNCATE|COMMENT|LOCK|COPY|CALL|DO|"
                    r"SET\s+ROLE|BEGIN|COMMIT|ROLLBACK)\b", re.I)


def records(folder):
    scan = FileAcquisitionCatalog(folder).scan()
    assert scan.rejected == ()
    return scan.records


def export_rows(items, *, import_closed="true"):
    """Rows of the CSV export for these manifests (one row per ingestion)."""
    counts = {}
    for record in items:
        counts[record.identity] = counts.get(record.identity, 0) + 1
    rows = []
    for record in items:
        footprint = record.footprint_wkb
        rows.append({
            "manifest_sha256": record.manifest_sha256, "collection": record.collection,
            "product_id": record.product_id, "schema_version": record.document["schema_version"],
            "acquired_at_utc": ae.utc_text(record.acquired_at_utc),
            "product_acquired_at_utc": ae.utc_text(record.acquired_at_utc),
            "ingested_at_utc": ae.utc_text(record.ingested_at_utc), "stac_source": record.stac_source,
            "grid_crs": record.grid_crs, "grid_resolution_m": f"{record.grid_resolution_m:g}",
            "integrity_mode": record.integrity_mode or "", "import_id": "1", "import_closed": import_closed,
            # jsonb text: keys reordered and spaced by PostgreSQL; the check must not depend on it.
            "manifest_document": json.dumps(record.document, sort_keys=True, separators=(", ", ": ")),
            "footprint_srid": "" if footprint is None else "4326",
            "footprint_wkb_sha256": "" if footprint is None else hashlib.sha256(footprint).hexdigest(),
            "ingestions_of_product": str(counts[record.identity])})
    return with_total(rows)


def with_total(rows):
    for row in rows:
        row["total_rows"] = str(len(rows))
    return rows


def tampered_asset_digest(document_text):
    """A still valid manifest whose content changed after import: one asset digest replaced."""
    document = json.loads(document_text)
    asset = next(a for a in document["assets"].values() if isinstance(a, dict) and "sha256" in a)
    asset["sha256"] = "e" * 64 if asset["sha256"] != "e" * 64 else "d" * 64
    return json.dumps(document)


def other_manifest(tmp_path, product="S2C_OTHER", ingested=datetime(2025, 2, 2, 8, tzinfo=timezone.utc)):
    folder = tmp_path / "elsewhere"
    write_product(folder, product, "2025-01-30T09:30:00Z", ingested)
    return records(folder)


def test_identical_export_confirms_parity_and_dates_stay_separate(acquisitions):
    local = records(acquisitions)
    rows = export_rows(local)
    assert ae.check_acquisition_export(rows, local) == []
    later = next(r for r in rows if r["product_id"] == "S2B_LATER")
    assert (later["acquired_at_utc"], later["ingested_at_utc"]) == ("2025-01-25T09:31:59.024000Z",
                                                                     "2025-01-26T12:00:00.000000Z")


@pytest.mark.parametrize("change, expected", [
    (lambda r: r.update(manifest_sha256="0" * 64), "document différent de son empreinte"),
    (lambda r: r.update(manifest_document=tampered_asset_digest(r["manifest_document"])),
     "document différent de son empreinte"),
    (lambda r: r.update(manifest_document=r["manifest_document"].replace('"S2A_EARLIER"', '"S2A_OTHER"', 1)),
     "manifeste refusé"),
    (lambda r: r.update(manifest_document="{not json"), "manifeste refusé"),
    (lambda r: r.update(acquired_at_utc="2025-01-20T09:33:22.000000Z"), "colonne acquired_at_utc"),
    (lambda r: r.update(product_acquired_at_utc="2025-01-19T00:00:00.000000Z"), "colonne product_acquired_at_utc"),
    (lambda r: r.update(ingested_at_utc="2025-01-22T08:00:00.000000Z"), "colonne ingested_at_utc"),
    (lambda r: r.update(grid_crs="EPSG:4326"), "colonne grid_crs"),
    (lambda r: r.update(grid_resolution_m="20"), "grid_resolution_m"),
    (lambda r: r.update(footprint_srid="32632"), "SRID 32632"),
    (lambda r: r.update(footprint_wkb_sha256="f" * 64), "emprise (WKB) différente"),
    (lambda r: r.update(import_closed="false"), "import d'origine non clôturé"),
    (lambda r: r.update(ingestions_of_product="2"), "nombre d'ingestions incohérent"),
])
def test_every_divergence_of_a_row_is_reported(acquisitions, change, expected):
    local = records(acquisitions)
    rows = export_rows(local)
    change(next(r for r in rows if r["product_id"] == "S2A_EARLIER"))
    problems = ae.check_acquisition_export(rows, local)
    assert problems and any(expected in p for p in problems), problems


def test_missing_extra_duplicated_and_orphan_rows_are_reported(acquisitions, tmp_path):
    local = records(acquisitions)
    extra = other_manifest(tmp_path)
    rows = export_rows(local[1:] + extra)                  # First local manifest absent, one unknown manifest.
    problems = ae.check_acquisition_export(rows, local)
    assert any("manifeste local absent de la base" in p for p in problems)
    assert any("absent des manifestes locaux" in p for p in problems)
    duplicated = with_total(export_rows(local) + [dict(export_rows(local)[0])])
    assert any("exporté plusieurs fois" in p for p in ae.check_acquisition_export(duplicated, local))
    orphan = with_total(export_rows(local) + [{c: "" for c in ae.EXPORT_COLUMNS} | {
        "collection": "sentinel-2-l2a", "product_id": "S2X_ORPHAN", "ingestions_of_product": "0"}])
    assert any("S2X_ORPHAN : acquisition sans aucun manifeste" in p for p in ae.check_acquisition_export(orphan, local))


def test_a_second_ingestion_of_a_product_must_also_exist_locally(acquisitions, tmp_path):
    local = records(acquisitions)
    reingested = other_manifest(tmp_path, product="S2A_EARLIER", ingested=datetime(2025, 3, 1, tzinfo=timezone.utc))
    problems = ae.check_acquisition_export(export_rows(local + reingested), local)
    assert any("absent des manifestes locaux" in p for p in problems)
    assert any("2 ingestion(s) dans la base, 1 localement" in p for p in problems)


def test_truncated_or_limited_exports_are_never_confirmed(acquisitions, monkeypatch):
    local = records(acquisitions)
    truncated = export_rows(local)[:-1]                     # total_rows still counts the row not downloaded.
    assert any("export incomplet" in p for p in ae.check_acquisition_export(truncated, local))
    monkeypatch.setattr(ae, "EXPORT_LIMIT", 2)
    assert any("limite de 2 lignes" in p for p in ae.check_acquisition_export(export_rows(local), local))


def test_sql_editor_null_is_read_as_null_only_in_nullable_columns():
    manifest_row = {c: "null" for c in ae.EXPORT_COLUMNS} | {"manifest_sha256": "a" * 64}
    normalized = ae._sql_nulls_as_empty(manifest_row)
    assert {c for c, v in normalized.items() if v == ""} == set(ae.MANIFEST_NULLABLE)
    orphan_row = {c: "null" for c in ae.EXPORT_COLUMNS}
    normalized = ae._sql_nulls_as_empty(orphan_row)
    assert {c for c, v in normalized.items() if v == ""} == set(ae.ORPHAN_NULLABLE)
    for column in ("collection", "product_id", "product_acquired_at_utc", "import_closed", "ingestions_of_product",
                   "total_rows"):                          # Never NULL, even for an acquisition without manifest.
        assert normalized[column] == "null", column
    assert ae._sql_nulls_as_empty({"integrity_mode": "NULL", "manifest_sha256": "a" * 64})["integrity_mode"] == "NULL"


def test_an_acquisition_without_manifest_exported_by_the_sql_editor_is_reported_as_such(acquisitions):
    local = records(acquisitions)
    orphan = {c: "null" for c in ae.EXPORT_COLUMNS} | {
        "collection": "sentinel-2-l2a", "product_id": "S2X_ORPHAN", "product_acquired_at_utc": "2025-01-01T00:00:00.000000Z",
        "import_closed": "false", "ingestions_of_product": "0"}
    problems = ae.check_acquisition_export(with_total(export_rows(local) + [orphan]), local)
    assert any("S2X_ORPHAN : acquisition sans aucun manifeste" in p for p in problems)
    assert not any("manifeste refusé" in p for p in problems)


@pytest.mark.parametrize("column, expected", [
    ("acquired_at_utc", "colonne acquired_at_utc"),
    ("ingested_at_utc", "colonne ingested_at_utc"),
    ("product_acquired_at_utc", "colonne product_acquired_at_utc"),
    ("stac_source", "colonne stac_source"),
    ("grid_crs", "colonne grid_crs"),
    ("grid_resolution_m", "grid_resolution_m"),
    ("manifest_document", "manifeste refusé"),
    ("import_closed", "import d'origine non clôturé"),
    ("ingestions_of_product", "nombre d'ingestions incohérent"),
    ("footprint_srid", "emprise en SRID absent"),           # Nullable column, but the local grid has a footprint.
    ("footprint_wkb_sha256", "emprise (WKB) différente"),
])
def test_the_text_null_never_hides_a_missing_value_of_a_manifest(acquisitions, column, expected):
    local = records(acquisitions)
    rows = export_rows(local)
    next(r for r in rows if r["product_id"] == "S2A_EARLIER")[column] = "null"
    problems = ae.check_acquisition_export(rows, local)
    assert any(expected in p for p in problems), problems


def _write(path, rows, columns=ae.EXPORT_COLUMNS):
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _script():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import verify_acquisition_export as script
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    return script


def test_verify_script_exit_codes_and_no_document_content_in_its_output(acquisitions, tmp_path, capsys):
    local = records(acquisitions)
    script = _script()
    good = tmp_path / "good.csv"
    _write(good, export_rows(local))
    assert script.main(["--csv", str(good), "--data-dir", str(acquisitions)]) == 0
    assert "Parité confirmée" in capsys.readouterr().out
    bad_rows = export_rows(local)
    bad_rows[0]["footprint_wkb_sha256"] = "f" * 64
    bad = tmp_path / "bad.csv"
    _write(bad, bad_rows)
    assert script.main(["--csv", str(bad), "--data-dir", str(acquisitions)]) == 1
    out = capsys.readouterr().out
    assert "ÉCART" in out and "pas un changement territorial" in out
    assert "https://" not in out and "assets" not in out and str(tmp_path) not in out
    partial = tmp_path / "partial.csv"
    _write(partial, export_rows(local), columns=ae.EXPORT_COLUMNS[:-1])
    assert script.main(["--csv", str(partial), "--data-dir", str(acquisitions)]) == 2


# --- Static checks of the two parity exports ------------------------------------------------------------


def _code(name):
    text = (SUPABASE / name).read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", text)
    return text, re.sub(r"'(?:[^']|'')*'", "''", code)


@pytest.mark.parametrize("name", ["07_export_parity.sql", "14_export_acquisitions_parity.sql"])
def test_parity_exports_are_read_only_single_requests_with_the_r5_path(name):
    text, code = _code(name)
    statements = [s.strip() for s in re.sub(r"--[^\n]*", "", text).split(";") if s.strip()]
    assert statements[0] + ";" == READ_ONLY_GUARD and statements[1] + ";" == R5_PATH
    assert len(statements) == 3 and statements[2].upper().startswith("SELECT")
    assert not WRITES.search(code), name
    assert "—" not in text and "PASSWORD" not in code.upper()


def test_acquisition_export_lists_every_checked_column_and_is_bounded():
    text, _ = _code("14_export_acquisitions_parity.sql")
    select = text[text.index("SELECT m.manifest_sha256"):text.index("FROM geonaf.acquisitions")]
    for column in ae.EXPORT_COLUMNS:
        assert re.search(rf"(AS|\.)\s*{column}\b", select), column
    assert f"LIMIT {ae.EXPORT_LIMIT};" in text and "count(*) OVER () AS total_rows" in text
    assert "AT TIME ZONE 'UTC'" in text and "ST_SRID(m.footprint)" in text and "'NDR'" in text


def test_territory_export_carries_the_srid_and_the_row_total():
    text, _ = _code("07_export_parity.sql")
    assert "ST_SRID(g.geom) AS srid" in text and "count(*) OVER () AS total_rows" in text
