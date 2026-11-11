"""Offline check of the CSV export of db/supabase/14_export_acquisitions_parity.sql (D2.10); no driver.

The SQL Editor export holds one row per ingestion (``acquisition_manifests``) and one row per acquisition
without any manifest. Every manifest is rebuilt from ``manifest_document`` with the same validation as the
file catalogue (:func:`src.data.observations.acquisition_record`); its SHA-256, its columns, its dates, its
grid footprint (SRID 4326, SHA-256 of the WKB) and the number of ingestions of its product must equal the
strict local manifests. Any difference is a problem of the copy, never a change of the territory.
Messages name a product and a digest prefix only: no document content, path or connection detail.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from typing import Iterable, Mapping

from src.data.observations import AcquisitionRecord, acquisition_record

EXPORT_COLUMNS = ("manifest_sha256", "collection", "product_id", "schema_version", "acquired_at_utc",
                  "product_acquired_at_utc", "ingested_at_utc", "stac_source", "grid_crs", "grid_resolution_m",
                  "integrity_mode", "import_id", "import_closed", "manifest_document", "footprint_srid",
                  "footprint_wkb_sha256", "ingestions_of_product", "total_rows")
EXPORT_LIMIT = 5000                       # LIMIT of the SQL export; total_rows is counted before it.
STORAGE_SRID = "4326"


def utc_text(value: datetime) -> str:
    """Same text as to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') in the export."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _label(product: str | None, sha: str | None) -> str:
    return f"{product or '?'} [{(sha or '?')[:12]}]"


def _expected_columns(record: AcquisitionRecord) -> dict[str, str]:
    """Text values the export must hold for this manifest (NULL as empty, as in the CSV)."""
    return {"collection": record.collection, "product_id": record.product_id,
            "schema_version": str(record.document.get("schema_version", "")),
            "acquired_at_utc": utc_text(record.acquired_at_utc), "product_acquired_at_utc": utc_text(record.acquired_at_utc),
            "ingested_at_utc": utc_text(record.ingested_at_utc), "stac_source": record.stac_source,
            "grid_crs": record.grid_crs, "integrity_mode": record.integrity_mode or ""}


def check_acquisition_export(rows: Iterable[Mapping[str, str]], local: Iterable[AcquisitionRecord]) -> list[str]:
    """Problems found between the export and the local manifests (empty list: parity confirmed)."""
    rows = [dict(row) for row in rows]
    local_by_sha = {record.manifest_sha256: record for record in local}
    local_ingestions = Counter(record.identity for record in local_by_sha.values())
    problems: list[str] = []

    totals = {row.get("total_rows") for row in rows}
    if rows and totals != {str(len(rows))}:
        problems.append(f"export incomplet ou incohérent : {len(rows)} lignes lues, total annoncé "
                        f"{', '.join(sorted(str(t) for t in totals))}")
    if len(rows) >= EXPORT_LIMIT:
        problems.append(f"export atteignant la limite de {EXPORT_LIMIT} lignes : relancer par parties")

    exported_ingestions = Counter((row.get("collection"), row.get("product_id")) for row in rows
                                  if row.get("manifest_sha256"))
    seen: set[str] = set()
    for row in rows:
        sha, product = row.get("manifest_sha256") or "", row.get("product_id")
        label = _label(product, sha)
        if not sha:
            problems.append(f"{product} : acquisition sans aucun manifeste dans la base")
            continue
        if sha in seen:
            problems.append(f"{label} : manifeste exporté plusieurs fois")
            continue
        seen.add(sha)
        try:
            rebuilt = acquisition_record(json.loads(row["manifest_document"]))
        except (KeyError, TypeError, ValueError) as error:
            problems.append(f"{label} : manifeste refusé ({type(error).__name__})")
            continue
        if rebuilt.manifest_sha256 != sha:
            problems.append(f"{label} : document différent de son empreinte (modifié depuis l'import)")
            continue
        for column, value in _expected_columns(rebuilt).items():
            if (row.get(column) or "") != value:
                problems.append(f"{label} : colonne {column} incohérente avec le document")
        try:
            resolution_ok = float(row.get("grid_resolution_m", "nan")) == rebuilt.grid_resolution_m
        except ValueError:
            resolution_ok = False
        if not resolution_ok:
            problems.append(f"{label} : colonne grid_resolution_m incohérente avec le document")
        if str(row.get("import_closed", "")).lower() not in ("true", "t"):
            problems.append(f"{label} : import d'origine non clôturé")
        exported_count = exported_ingestions[rebuilt.identity]
        if str(exported_count) != str(row.get("ingestions_of_product")):
            problems.append(f"{label} : nombre d'ingestions incohérent dans l'export")

        reference = local_by_sha.get(sha)
        if reference is None:
            problems.append(f"{label} : présent dans la base, absent des manifestes locaux")
            continue
        if rebuilt.document != reference.document:
            problems.append(f"{label} : document différent du manifeste local")
        if reference.footprint_wkb is None:
            if row.get("footprint_srid") or row.get("footprint_wkb_sha256"):
                problems.append(f"{label} : emprise présente dans la base, absente localement")
        else:
            if (row.get("footprint_srid") or "") != STORAGE_SRID:
                problems.append(f"{label} : emprise en SRID {row.get('footprint_srid') or 'absent'} "
                                f"au lieu de {STORAGE_SRID}")
            if row.get("footprint_wkb_sha256") != hashlib.sha256(reference.footprint_wkb).hexdigest():
                problems.append(f"{label} : emprise (WKB) différente de la grille locale")
        if exported_count != local_ingestions[reference.identity]:
            problems.append(f"{label} : {exported_count} ingestion(s) dans la base, "
                            f"{local_ingestions[reference.identity]} localement")

    for sha in sorted(set(local_by_sha) - seen):
        problems.append(f"{_label(local_by_sha[sha].product_id, sha)} : manifeste local absent de la base")
    return problems
