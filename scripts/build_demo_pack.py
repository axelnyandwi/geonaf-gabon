"""Build the reduced Akanda demonstration pack from verified local data (no network).

Usage : python scripts/build_demo_pack.py [--output DIR] [--allow-dirty]

Reads ``data/`` without modifying it and refuses to write into an existing
directory. The pack mirrors the ``data/`` layout (``aoi/``, ``raw/sentinel2/``,
``external/``) so it can later serve as ``GABON_GEOAI_DATA_DIR``. Sentinel-2
assets are native-grid subsets around the Akanda acquisition square (see
``src/data/derived_subset.py``); every other file is a byte-identical copy.

Provenance distinguishes the code (Git commit of the builder; building from
uncommitted code is refused unless ``--allow-dirty``) from the source data,
which are not versioned in Git and are identified by the digests of their
manifests and files. A deterministic ZIP and its ``.sha256`` are written next
to the pack directory.
"""

from __future__ import annotations

import argparse
import hashlib
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
from src.data.demo_pack import create_archive  # noqa: E402

PACK_ID = "geonaf-demo-akanda-2025-01"
PACK_VERSION = "3"  # v3 = v2 + geoBoundaries referentials (v2 never published).
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


CODE_PATHS = ("src", "scripts/build_demo_pack.py", "requirements.txt")


def _git(*args) -> str:
    return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True).stdout


def code_provenance() -> dict:
    """Commit of the code that produced the pack, and uncommitted changes to that code."""
    try:
        commit = _git("rev-parse", "HEAD").strip()
        dirty = sorted(line[3:] for line in _git("status", "--porcelain", "--", *CODE_PATHS).splitlines())
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "uncommitted_paths": None}
    return {"commit": commit, "uncommitted_paths": dirty}


def software_versions(code: dict) -> dict:
    return {"python": platform.python_version(), "rasterio": rasterio.__version__,
            "gdal": rasterio.__gdal_version__, "numpy": np.__version__, "pyproj": pyproj.__version__,
            "geopandas": gpd.__version__, "code_commit": code["commit"]}


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
                        "crs": assets["b04"]["crs"], "source_manifest_sha256": parent_sha256,
                        "source_assets_sha256": {role: record["sha256"]
                                                 for role, record in parent["assets"].items()}})
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
                       "descriptor_fingerprint": fingerprint, "file_sha256": expected,
                       "version": descriptor.get("version"), "temporal": descriptor["temporal"],
                       "license": descriptor["license"], "quality_notes": descriptor["quality"].get("notes", []),
                       "storage_key": key})
    return layers


def build_boundaries(data_dir: Path, output: Path) -> list[dict]:
    """geoBoundaries snapshots and their registered ADM2 units, byte-identical and digest-checked.

    Only ``admin_unit`` records whose source is geoBoundaries are copied; each keeps
    its legal status (community or humanitarian referential), never « official ».
    """
    from src.data.territory_registry import ADMIN_UNIT, OFFICIAL_VERIFIED, TerritoryRegistry

    releases = []
    for meta_path in sorted((data_dir / "external" / "geoboundaries").glob("*/GAB/ADM2/*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        snapshot = meta_path.with_name(meta_path.name.replace(".meta.json", ".geojson"))
        relative = meta_path.parent.relative_to(data_dir)
        _copy_verified(snapshot, output / relative / snapshot.name, meta["sha256"])
        _copy_verified(meta_path, output / relative / meta_path.name)
        details = meta["metadata"]
        releases.append({"release": relative.parts[2], "level": "ADM2", "iso3": "GAB",
                         "snapshot_sha256": meta["sha256"], "retrieved_at_utc": meta["retrieved_at_utc"],
                         "metadata_url": meta["metadata_url"], "license": details["boundaryLicense"],
                         "license_source": details["licenseSource"], "source": details["boundarySource"],
                         "year_represented": details["boundaryYearRepresented"],
                         "build_date": details["buildDate"]})
    registry = TerritoryRegistry(data_dir)
    statuses = {}
    for record in registry.territories(ADMIN_UNIT):
        if not record.source.source_id.startswith("geoboundaries-"):
            continue
        geometry = registry.geometry_path(record)  # Verifies the file digest of the record.
        _copy_verified(geometry, output / "territories" / record.storage_key, record.file_sha256)
        for version_key in registry.store.keys(f"records/{record.territory_id}"):
            _copy_verified(registry.store.path(version_key), output / "territories" / version_key)
        statuses.setdefault(record.source.source_id, set()).add(record.legal_status)
    for release in releases:
        found = statuses.get(f"geoboundaries-{release['release'].lower()}", set())
        if not found or OFFICIAL_VERIFIED in found:
            raise ValueError(f"Statut juridique inattendu pour {release['release']}.")
        release["legal_status"] = sorted(found)
        release["units"] = sum(1 for record in registry.territories(ADMIN_UNIT)
                               if record.source.source_id == f"geoboundaries-{release['release'].lower()}")
    return releases


def source_data_fingerprint(sentinel2: list[dict], external: list[dict], boundaries: list[dict]) -> str:
    """Identity of the source data (not in Git): digest of their manifests and file digests."""
    canonical = {"sentinel2": sorted([item["product_id"], item["source_manifest_sha256"],
                                      sorted(item["source_assets_sha256"].items())] for item in sentinel2),
                 "external": sorted([item["descriptor_fingerprint"], item["file_sha256"]] for item in external),
                 "boundaries": sorted([item["release"], item["snapshot_sha256"]] for item in boundaries)}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def pack_inventory(output: Path) -> list[dict]:
    return [{"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size,
             "sha256": sha256_file(path)}
            for path in sorted(output.rglob("*")) if path.is_file() and path.name != "PACK_MANIFEST.json"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DATA_DIR / "demo_pack" / f"{PACK_ID}-v{PACK_VERSION}")
    parser.add_argument("--allow-dirty", action="store_true", help="Accepter du code non commité (tracé).")
    args = parser.parse_args()
    output = args.output.resolve()
    archive = output.with_name(output.name + ".zip")
    if output.exists() or archive.exists():
        print("Le dossier ou l'archive de sortie existe déjà ; rien n'est écrasé.", file=sys.stderr)
        return 1
    code = code_provenance()
    if code["uncommitted_paths"] and not args.allow_dirty:
        print("Code du constructeur non commité : " + ", ".join(code["uncommitted_paths"]), file=sys.stderr)
        return 1
    created_at = datetime.now(timezone.utc)
    software = software_versions(code)
    aoi_path = DATA_DIR / "aoi" / f"{AOI_ID}.geojson"
    _copy_verified(aoi_path, output / "aoi" / aoi_path.name)
    sentinel2 = build_sentinel2(DATA_DIR, output, aoi_path, created_at, software)
    measure_quality(output, sentinel2)
    external = build_external(DATA_DIR, output)
    boundaries = build_boundaries(DATA_DIR, output)
    files = pack_inventory(output)
    write_json({
        "pack_id": PACK_ID, "pack_version": PACK_VERSION,
        "created_at_utc": created_at.isoformat().replace("+00:00", "Z"), "software": software,
        "code": {"repository": "geonaf", **code},
        "source_data": {"fingerprint_sha256": source_data_fingerprint(sentinel2, external, boundaries),
                        "note": "Données sources hors Git : identifiées par les empreintes de leurs "
                                "manifestes et fichiers, vérifiées au moment de la construction."},
        "aoi": {"id": AOI_ID, "path": f"aoi/{AOI_ID}.geojson",
                "statement": "Zone d'acquisition historique de 12 × 12 km autour d'Akanda. "
                             "Ce n'est ni la commune d'Akanda ni une autre unité administrative."},
        "sentinel2": {"collection": "sentinel-2-l2a", "tile": "T32NNF", "attribution": SENTINEL2_ATTRIBUTION,
                      "license": "Copernicus Sentinel data terms (accès libre, attribution requise)",
                      "subset_margin_m": MARGIN_M, "acquisitions": sentinel2},
        "external_layers": external,
        "boundaries": {
            "statement": "Référentiels geoBoundaries gbOpen (communautaire) et gbHumanitarian (humanitaire, "
                         "OCHA COD-AB via SALB/HDX) : limites non officielles, non vérifiées, sans valeur "
                         "juridique. Aucune limite administrative officielle n'est fournie.",
            "releases": boundaries},
        "limits": [
            "Extraits spatiaux de tuiles vérifiées localement : l'intégrité de la source a été contrôlée "
            "contre STAC à l'ingestion, pas recontrôlable depuis l'extrait seul.",
            "Dates affichées = dates d'acquisition ; aucune acquisition postérieure au 25/01/2025 dans le paquet.",
            "WorldCover 2021 et l'instantané OSM ne décrivent pas l'état du territoire aux dates Sentinel-2.",
            "Les deux référentiels de limites diffèrent (sources et années différentes) : une divergence "
            "entre eux n'est pas un changement du territoire.",
        ],
        "files": files,
        "total_bytes": sum(item["bytes"] for item in files),
    }, output / "PACK_MANIFEST.json")
    digest = create_archive(output, archive)
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"Paquet écrit : {len(files)} fichiers, {sum(item['bytes'] for item in files) / 1e6:.1f} Mo")
    print(f"Archive : {archive.name}, {archive.stat().st_size / 1e6:.1f} Mo, SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
