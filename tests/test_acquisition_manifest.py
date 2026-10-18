from datetime import datetime, timezone
import hashlib
import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.data.acquisition_manifest import (
    ASSET_ROLES,
    build_acquisition_manifest,
    validate_acquisition_manifest,
    write_acquisition_manifest,
)


PRODUCT_ID = "S2B_SYNTHETIC_PRODUCT"
ASSET_IDS = {
    "b04": "B04_10m",
    "b08": "B08_10m",
    "scl": "SCL_20m",
    "calibration_xml": "product-metadata",
}


def _write_inputs(folder):
    paths = {}
    for role, resolution, dtype in (
        ("b04", 10, "uint16"),
        ("b08", 10, "uint16"),
        ("scl", 20, "uint8"),
    ):
        path = folder / f"{role}.tif"
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=2,
            height=2,
            count=1,
            dtype=dtype,
            crs="EPSG:32632",
            transform=from_origin(500000, 100000, resolution, resolution),
            nodata=0,
        ) as dataset:
            dataset.write(np.ones((2, 2), dtype=dtype), 1)
        paths[role] = path

    xml_path = folder / "metadata.xml"
    xml_path.write_text(
        f"""<Product><PRODUCT_URI>{PRODUCT_ID}.SAFE</PRODUCT_URI>
        <BOA_QUANTIFICATION_VALUE>10000</BOA_QUANTIFICATION_VALUE>
        <BOA_ADD_OFFSET band_id="3">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="7">-1000</BOA_ADD_OFFSET></Product>""",
        encoding="utf-8",
    )
    paths["calibration_xml"] = xml_path
    return paths


def _stac_item():
    return {
        "id": PRODUCT_ID,
        "collection": "sentinel-2-l2a",
        "properties": {"datetime": "2025-01-25T09:31:59.024Z"},
        "assets": {
            asset_id: {"href": f"s3://bucket/product/{asset_id}.bin"}
            for asset_id in ASSET_IDS.values()
        },
    }


def test_manifest_records_stac_assets_hashes_metadata_and_relative_paths(tmp_path):
    paths = _write_inputs(tmp_path)
    ingested_at = datetime(2025, 1, 26, 12, tzinfo=timezone.utc)

    manifest = build_acquisition_manifest(
        product=_stac_item(),
        asset_paths=paths,
        asset_ids=ASSET_IDS,
        stac_source="https://stac.example/v1",
        ingested_at=ingested_at,
        product_directory=tmp_path,
    )

    assert set(manifest["assets"]) == ASSET_ROLES
    assert manifest["schema_version"] == "geonaf.sentinel2-acquisition/v1"
    assert manifest["product_id"] == PRODUCT_ID
    assert manifest["acquired_at_utc"] == "2025-01-25T09:31:59.024000Z"
    assert manifest["collection"] == "sentinel-2-l2a"
    assert manifest["stac"]["source"] == "https://stac.example/v1"
    assert manifest["ingested_at_utc"] == "2025-01-26T12:00:00.000000Z"
    assert manifest["assets"]["b04"]["id"] == "B04_10m"
    assert manifest["assets"]["b04"]["path"] == "b04.tif"
    assert manifest["assets"]["b04"]["sha256"] == hashlib.sha256(
        paths["b04"].read_bytes()
    ).hexdigest()
    assert manifest["assets"]["b04"]["crs"] == "EPSG:32632"
    assert manifest["assets"]["b04"]["resolution"] == [10.0, 10.0]
    assert manifest["assets"]["b04"]["extent"] == [500000.0, 99980.0, 500020.0, 100000.0]
    assert manifest["calibration"] == {
        "method": "BOA",
        "xml_asset_role": "calibration_xml",
        "quantification_value": 10000.0,
        "offsets": {"B04": -1000.0, "B08": -1000.0},
    }


def test_manifest_rejects_missing_asset_role_or_product_mismatch(tmp_path):
    paths = _write_inputs(tmp_path)
    with pytest.raises(ValueError, match="quatre rôles"):
        build_acquisition_manifest(
            product=_stac_item(),
            asset_paths={key: value for key, value in paths.items() if key != "scl"},
            asset_ids=ASSET_IDS,
            stac_source="https://stac.example/v1",
            ingested_at=datetime(2025, 1, 26, tzinfo=timezone.utc),
            product_directory=tmp_path,
        )

    mismatched = {**_stac_item(), "id": "OTHER_PRODUCT"}
    with pytest.raises(ValueError, match="XML de calibration ne correspond"):
        build_acquisition_manifest(
            product=mismatched,
            asset_paths=paths,
            asset_ids=ASSET_IDS,
            stac_source="https://stac.example/v1",
            ingested_at=datetime(2025, 1, 26, tzinfo=timezone.utc),
            product_directory=tmp_path,
        )


def test_manifest_rejects_reused_or_unknown_stac_asset_ids(tmp_path):
    paths = _write_inputs(tmp_path)
    repeated_ids = {**ASSET_IDS, "b08": ASSET_IDS["b04"]}
    with pytest.raises(ValueError, match="distinct"):
        build_acquisition_manifest(
            product=_stac_item(),
            asset_paths=paths,
            asset_ids=repeated_ids,
            stac_source="https://stac.example/v1",
            ingested_at=datetime(2025, 1, 26, tzinfo=timezone.utc),
            product_directory=tmp_path,
        )

    unknown_ids = {**ASSET_IDS, "scl": "not-in-stac-item"}
    with pytest.raises(ValueError, match="ne provient pas de l'item STAC"):
        build_acquisition_manifest(
            product=_stac_item(),
            asset_paths=paths,
            asset_ids=unknown_ids,
            stac_source="https://stac.example/v1",
            ingested_at=datetime(2025, 1, 26, tzinfo=timezone.utc),
            product_directory=tmp_path,
        )


def test_manifest_rejects_asset_outside_product_directory_and_invalid_datetime(tmp_path):
    paths = _write_inputs(tmp_path)
    outside = tmp_path.parent / "outside.tif"
    outside.write_bytes(paths["b04"].read_bytes())
    paths["b04"] = outside
    with pytest.raises(ValueError, match="chemin relatif"):
        build_acquisition_manifest(
            product=_stac_item(),
            asset_paths=paths,
            asset_ids=ASSET_IDS,
            stac_source="https://stac.example/v1",
            ingested_at=datetime(2025, 1, 26, tzinfo=timezone.utc),
            product_directory=tmp_path,
        )

    with pytest.raises(ValueError, match="date STAC"):
        build_acquisition_manifest(
            product={**_stac_item(), "properties": {"datetime": "unknown"}},
            asset_paths={**paths, "b04": tmp_path / "b08.tif"},
            asset_ids=ASSET_IDS,
            stac_source="https://stac.example/v1",
            ingested_at=datetime(2025, 1, 26, tzinfo=timezone.utc),
            product_directory=tmp_path,
        )


def test_manifest_writer_never_overwrites_existing_file(tmp_path):
    paths = _write_inputs(tmp_path)
    manifest = build_acquisition_manifest(
        product=_stac_item(),
        asset_paths=paths,
        asset_ids=ASSET_IDS,
        stac_source="https://stac.example/v1",
        ingested_at=datetime(2025, 1, 26, tzinfo=timezone.utc),
        product_directory=tmp_path,
    )
    output = tmp_path / "acquisition_manifest.json"
    write_acquisition_manifest(manifest, output)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved == manifest
    with pytest.raises(FileExistsError):
        write_acquisition_manifest(manifest, output)


def test_manifest_requires_explicit_exploratory_mode_for_unconfirmed_source(tmp_path):
    paths = _write_inputs(tmp_path)
    integrity = {
        "mode": "exploratory",
        "source_assets": {
            role: {
                "status": "unconfirmed" if role == "calibration_xml" else "verified",
                "method": "none" if role == "calibration_xml" else "stac_file_checksum",
                "declared_checksum_status": "invalid" if role == "calibration_xml" else "valid",
                "declared_checksum": "malformed" if role == "calibration_xml" else "1220" + "a" * 64,
                **({} if role == "calibration_xml" else {"algorithm": "sha256"}),
            }
            for role in ASSET_ROLES
        },
        "evidence": {},
    }
    manifest = build_acquisition_manifest(
        product=_stac_item(),
        asset_paths=paths,
        asset_ids=ASSET_IDS,
        stac_source="https://stac.example/v1",
        ingested_at=datetime(2025, 1, 26, tzinfo=timezone.utc),
        product_directory=tmp_path,
        integrity=integrity,
    )
    validate_acquisition_manifest(manifest)

    manifest["integrity"]["mode"] = "strict"
    with pytest.raises(ValueError, match="hors mode exploratoire"):
        validate_acquisition_manifest(manifest)


def test_manifest_rejects_invalid_stac_checksum_marked_as_verified(tmp_path):
    paths = _write_inputs(tmp_path)
    integrity = {
        "mode": "strict",
        "source_assets": {
            role: {
                "status": "verified",
                "method": "stac_file_checksum",
                "declared_checksum_status": "invalid" if role == "calibration_xml" else "valid",
                "declared_checksum": "d520" + "0" * 64 if role == "calibration_xml" else "1220" + "a" * 64,
                "algorithm": "md5" if role == "calibration_xml" else "sha256",
            }
            for role in ASSET_ROLES
        },
        "evidence": {},
    }
    with pytest.raises(ValueError, match="marqué vérifié sans format valide"):
        build_acquisition_manifest(
            product=_stac_item(),
            asset_paths=paths,
            asset_ids=ASSET_IDS,
            stac_source="https://stac.example/v1",
            ingested_at=datetime(2025, 1, 26, tzinfo=timezone.utc),
            product_directory=tmp_path,
            integrity=integrity,
        )
