"""Build the reduced Akanda demonstration pack from verified local data (no network).

Usage : python scripts/build_demo_pack.py [--output data/demo_pack/akanda-2025-01-v1]

Reads ``data/`` without modifying it and refuses to write into an existing
directory. The pack mirrors the ``data/`` layout (``aoi/``, ``raw/sentinel2/``,
``external/``) so it can later serve as ``GABON_GEOAI_DATA_DIR``. Sentinel-2
assets are native-grid subsets around the Akanda acquisition square (see
``src/data/derived_subset.py``); every other file is a byte-identical copy.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pyproj  # noqa: E402
import rasterio  # noqa: E402

from src.config import DATA_DIR  # noqa: E402
from src.data.acquisition_manifest import validate_acquisition_manifest  # noqa: E402
from src.data.derived_subset import (  # noqa: E402
    aligned_subset_bounds, build_subset_manifest, sha256_file, write_json, write_raster_subset,
)

PACK_ID = "geonaf-demo-akanda-2025-01"
PACK_VERSION = "1"
AOI_ID = "akanda"
MARGIN_M = 200.0
PRODUCTS = (
    "S2B_MSIL2A_20250115T093239_N0511_R136_T32NNF_20250115T120528",
    "S2A_MSIL2A_20250120T093321_N0511_R136_T32NNF_20250120T125148",
    "S2B_MSIL2A_20250125T093159_N0511_R136_T32NNF_20250125T115658",
)
# Catalogue descriptors of the external layers stored for the Akanda square. The second
# OpenStreetMap descriptor of the same snapshot (d0c84364…) has no checksum and is left out.
EXTERNAL_DESCRIPTORS = (
    "2f608c46d70fdd500f05618b70ed6d274965be2a10691b48a371774c14c18053",  # WorldCover 2021 v200
    "a2c01857214543daa2b639e649e191a33761a4f3090083f0c537f21f270fffa2",  # OSM 2026-10-22
    "eaf6ab9ee3fa90156ff8bca912f6470eccb58d037352ed7dcae3cda858d98f48",  # S1 mosaic 2025-01 VV
    "e2d77d6526804ba4780abc0b6c97ca04b5ea7aabae5df5ba633ad3a805863824",  # S1 mosaic 2025-01 VH
)
SENTINEL2_ATTRIBUTION = "Contains modified Copernicus Sentinel data 2025, processed by GEONAF"


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def software_versions() -> dict:
    return {"python": platform.python_version(), "rasterio": rasterio.__version__,
            "gdal": rasterio.__gdal_version__, "numpy": np.__version__, "pyproj": pyproj.__version__,
            "geopandas": gpd.__version__, "geonaf_commit": _git_commit()}


def _copy_verified(source: Path, destination: Path, expected_sha256: str | None = None) -> str:
    digest = sha256_file(source)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError(f"Empreinte inattendue : {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if sha256_file(destination) != digest:
        raise ValueError(f"Copie altérée : {destination.name}")
    return digest


def _verified_parent(product_dir: Path) -> tuple[dict, str]:
    """Parent manifest in strict mode, with every full-tile asset digest re-checked."""
    path = product_dir / "acquisition_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    validate_acquisition_manifest(manifest)
    integrity = manifest.get("integrity", {})
    if integrity.get("mode") != "strict" or any(
            record.get("status") != "verified" for record in integrity.get("source_assets", {}).values()):
        raise ValueError(f"Intégrité stricte non vérifiée : {manifest['product_id']}")
    for role, record in manifest["assets"].items():
        if sha256_file(product_dir / record["path"]) != record["sha256"]:
            raise ValueError(f"Empreinte de tuile différente du manifeste : {role}")
    return manifest, sha256_file(path)


def build_sentinel2(data_dir: Path, output: Path, aoi_path: Path, created_at: datetime, software: dict):
    records = []
    for product_id in PRODUCTS:
        source_dir = data_dir / "raw" / "sentinel2" / product_id
        parent, parent_sha256 = _verified_parent(source_dir)
        b04 = source_dir / parent["assets"]["b04"]["path"]
        with rasterio.open(b04) as tile:
            aoi_bounds = tuple(gpd.read_file(aoi_path).to_crs(tile.crs).total_bounds)
            bounds = aligned_subset_bounds(aoi_bounds, tile.transform, (tile.width, tile.height),
                                           margin_m=MARGIN_M)
        target_dir = output / "raw" / "sentinel2" / product_id
        assets = {}
        for role in ("b04", "b08", "scl"):
            source = source_dir / parent["assets"][role]["path"]
            name = Path(parent["assets"][role]["path"]).stem + "_akanda.tif"
            assets[role] = write_raster_subset(source, target_dir / name, bounds, product_id=product_id)
        xml = parent["assets"]["calibration_xml"]
        assets["calibration_xml"] = {"path": xml["path"],
                                     "sha256": _copy_verified(source_dir / xml["path"], target_dir / xml["path"],
                                                              xml["sha256"])}
        evidence = parent["integrity"].get("evidence", {}).get("safe_manifest")
        if evidence:
            _copy_verified(source_dir / evidence["path"], target_dir / evidence["path"], evidence["sha256"])
        _copy_verified(source_dir / "acquisition_manifest.json", target_dir / "source_acquisition_manifest.json",
                       parent_sha256)
        aoi = {"id": AOI_ID, "path": f"aoi/{AOI_ID}.geojson", "sha256": sha256_file(aoi_path),
               "nature": "carré d'acquisition 12 × 12 km ; pas une unité administrative",
               "bounds_tile_crs": list(aoi_bounds)}
        manifest = build_subset_manifest(parent_manifest=parent, parent_manifest_sha256=parent_sha256,
                                         assets=assets, aoi=aoi, margin_m=MARGIN_M, created_at=created_at,
                                         software=software)
        write_json(manifest, target_dir / "subset_manifest.json")
        records.append({"product_id": product_id, "acquired_at_utc": parent["acquired_at_utc"],
                        "ingested_at_utc": parent["ingested_at_utc"], "subset_bounds": list(bounds),
                        "crs": assets["b04"]["crs"]})
    if len({tuple(record["subset_bounds"]) for record in records}) != 1:
        raise ValueError("Les extraits n'ont pas la même emprise.")
    return records


def measure_quality(output: Path, records: list[dict]) -> None:
    """Valid AOI coverage per date (reference profile), measured on the subsets just written.

    A date below the profile minimum is flagged ``trend_use = excluded_low_coverage``; the
    NDVI series already excludes it, and the flag keeps the reason visible in the pack.
    """
    from src.data.acquisition import create_sentinel2_acquisition
    from src.geo.scientific_profile import reference_profile
    from src.services.ndvi_observation import analyze_ndvi_observation

    profile = reference_profile(AOI_ID)
    for record in records:
        directory = output / "raw" / "sentinel2" / record["product_id"]
        manifest = json.loads((directory / "subset_manifest.json").read_text(encoding="utf-8"))
        paths = {role: directory / asset["path"] for role, asset in manifest["assets"].items()}
        acquisition = create_sentinel2_acquisition(
            product_id=manifest["product_id"],
            acquired_at=datetime.fromisoformat(manifest["acquired_at_utc"].replace("Z", "+00:00")),
            b04_path=paths["b04"], b08_path=paths["b08"], scl_path=paths["scl"],
            calibration_xml_path=paths["calibration_xml"],
            asset_product_ids={role: asset["product_id"] for role, asset in manifest["assets"].items()})
        observation = analyze_ndvi_observation(acquisition, aoi_path=output / "aoi" / f"{AOI_ID}.geojson",
                                               scientific_profile=profile)
        coverage = float(observation.valid_coverage_percent)
        low = coverage < profile.min_comparable_coverage_percent
        record["quality"] = {
            "valid_coverage_percent": coverage, "scl_policy": profile.scl_policy,
            "profile": f"{profile.profile_id} {profile.version}", "profile_fingerprint": profile.fingerprint,
            "min_coverage_percent": profile.min_comparable_coverage_percent,
            "trend_use": "excluded_low_coverage" if low else "eligible",
            "note": ("Couverture exploitable très faible : à exclure de toute analyse de tendance." if low
                     else "Couverture suffisante pour le profil exploratoire ; non validée."),
        }


def build_external(data_dir: Path, output: Path):
    layers = []
    for fingerprint in EXTERNAL_DESCRIPTORS:
        source = data_dir / "external" / "catalogue" / f"{fingerprint}.json"
        record = json.loads(source.read_text(encoding="utf-8"))
        descriptor, key = record["descriptor"], record["storage_key"]
        if descriptor["fingerprint"] != fingerprint or Path(key).is_absolute() or ".." in Path(key).parts:
            raise ValueError(f"Descripteur incohérent : {fingerprint}")
        expected = descriptor["quality"].get("checksum_sha256")
        if expected is None:
            raise ValueError(f"Couche sans empreinte : {fingerprint}")
        _copy_verified(source, output / "external" / "catalogue" / source.name)
        _copy_verified(data_dir / "external" / key, output / "external" / key, expected)
        layers.append({"source_id": descriptor["source_id"], "product": descriptor.get("product"),
                       "version": descriptor.get("version"), "temporal": descriptor["temporal"],
                       "license": descriptor["license"], "quality_notes": descriptor["quality"].get("notes", []),
                       "storage_key": key})
    return layers


def pack_inventory(output: Path) -> list[dict]:
    return [{"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size,
             "sha256": sha256_file(path)}
            for path in sorted(output.rglob("*")) if path.is_file() and path.name != "PACK_MANIFEST.json"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DATA_DIR / "demo_pack" / f"{PACK_ID}-v{PACK_VERSION}")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        print("Le dossier de sortie existe déjà ; rien n'est écrasé.", file=sys.stderr)
        return 1
    created_at = datetime.now(timezone.utc)
    software = software_versions()
    aoi_path = DATA_DIR / "aoi" / f"{AOI_ID}.geojson"
    _copy_verified(aoi_path, output / "aoi" / aoi_path.name)
    sentinel2 = build_sentinel2(DATA_DIR, output, aoi_path, created_at, software)
    measure_quality(output, sentinel2)
    external = build_external(DATA_DIR, output)
    files = pack_inventory(output)
    write_json({
        "pack_id": PACK_ID, "pack_version": PACK_VERSION,
        "created_at_utc": created_at.isoformat().replace("+00:00", "Z"), "software": software,
        "aoi": {"id": AOI_ID, "path": f"aoi/{AOI_ID}.geojson",
                "statement": "Zone d'acquisition historique de 12 × 12 km autour d'Akanda. "
                             "Ce n'est ni la commune d'Akanda ni une autre unité administrative."},
        "sentinel2": {"collection": "sentinel-2-l2a", "tile": "T32NNF", "attribution": SENTINEL2_ATTRIBUTION,
                      "license": "Copernicus Sentinel data terms (accès libre, attribution requise)",
                      "subset_margin_m": MARGIN_M, "acquisitions": sentinel2},
        "external_layers": external,
        "limits": [
            "Extraits spatiaux de tuiles vérifiées localement : l'intégrité de la source a été contrôlée "
            "contre STAC à l'ingestion, pas recontrôlable depuis l'extrait seul.",
            "Dates affichées = dates d'acquisition ; aucune acquisition postérieure au 25/01/2025 dans le paquet.",
            "WorldCover 2021 et l'instantané OSM ne décrivent pas l'état du territoire aux dates Sentinel-2.",
        ],
        "files": files,
        "total_bytes": sum(item["bytes"] for item in files),
    }, output / "PACK_MANIFEST.json")
    print(f"Paquet écrit : {len(files)} fichiers, {sum(item['bytes'] for item in files) / 1e6:.1f} Mo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
