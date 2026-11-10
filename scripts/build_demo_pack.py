"""Build the reduced Estuaire demonstration pack (Akanda, Libreville, Owendo) from verified local data.

Usage : python scripts/build_demo_pack.py [--output DIR] [--created-at ISO-UTC] [--allow-dirty]

Reads ``data/`` without modifying it (no network) and refuses to write into an existing
directory. The pack mirrors the ``data/`` layout (``aoi/``, ``raw/sentinel2/``,
``external/``) so it can later serve as ``GABON_GEOAI_DATA_DIR``.

One territory = one GEONAF acquisition square of 12 × 12 km, never an administrative unit.
Each territory gets its own native-grid Sentinel-2 subsets in ``raw/sentinel2/<product>__<aoi>/``
(``src/data/derived_subset.py``), whose ``derivation.aoi`` (id and AOI SHA-256) is what the
territory filter of ``src/services/local_preview.py`` checks: subsets are never shared or mixed
between territories. External layers (WorldCover, OpenStreetMap, Sentinel-1) are byte-identical
copies of the descriptors stored for each square, checked against its bounds.

Provenance distinguishes the code (Git commit of the builder; building from uncommitted code is
refused unless ``--allow-dirty``, which is recorded) from the source data, identified by the
digests of their manifests and files. ``--created-at`` fixes the creation time written in the
manifests: two builds of the same code and data with the same value give the same archive.
A deterministic ZIP and its ``.sha256`` are written next to the pack directory.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import re
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

PACK_ID = "geonaf-demo-estuaire-2025-01"
PACK_VERSION = "4"  # v4 = three territories, each with its own subsets (v3: Akanda only).
MARGIN_M = 200.0

P2021_01_31 = "S2A_MSIL2A_20210131T093231_N0500_R136_T32NNF_20230524T163205"
P2025_01_15 = "S2B_MSIL2A_20250115T093239_N0511_R136_T32NNF_20250115T120528"
P2025_01_20 = "S2A_MSIL2A_20250120T093321_N0511_R136_T32NNF_20250120T125148"
P2025_01_25 = "S2B_MSIL2A_20250125T093159_N0511_R136_T32NNF_20250125T115658"


@dataclass(frozen=True)
class Territory:
    """One acquisition square: its label, Sentinel-2 products and external layer descriptors."""
    label: str
    products: tuple[str, ...]
    descriptors: tuple[str, ...]   # Catalogue fingerprints, in EXPECTED_LAYERS order.


EXPECTED_LAYERS = ("esa-worldcover", "openstreetmap", "sentinel-1-mosaic", "sentinel-1-mosaic")
# 20/01/2025 is kept for Akanda only (v3 non-regression); it is almost fully clouded over
# Libreville (0,14 %) and Owendo (0 %). Owendo keeps 15/01/2025 (0,12 %) so that the absence of a
# usable 2025 pair is visible and explained, never hidden. The second OpenStreetMap descriptor of
# the Akanda snapshot (d0c84364…) has no checksum and is left out.
TERRITORIES = {
    "akanda": Territory("Akanda", (P2021_01_31, P2025_01_15, P2025_01_20, P2025_01_25), (
        "2f608c46d70fdd500f05618b70ed6d274965be2a10691b48a371774c14c18053",    # WorldCover 2021 v200
        "a2c01857214543daa2b639e649e191a33761a4f3090083f0c537f21f270fffa2",    # OSM 2026-10-22
        "eaf6ab9ee3fa90156ff8bca912f6470eccb58d037352ed7dcae3cda858d98f48",    # S1 mosaic 2025-01 VV
        "e2d77d6526804ba4780abc0b6c97ca04b5ea7aabae5df5ba633ad3a805863824")),  # S1 mosaic 2025-01 VH
    "libreville": Territory("Libreville", (P2021_01_31, P2025_01_15, P2025_01_25), (
        "45ab773d1164eef2cbdea78d96d95d8aa05d3cef701977d7edc200ff2aff2690",
        "8b9102487ac3e9754f0a3f55127f85542e8924920f02a0d7b0e750c9e5c2af72",
        "57994a031eaa3b066b22031919bd6e9cb4a94525ea90bf95580c0f6ccdd57bff",
        "ee1eaf8419e205254d38e0c2b6e9f9d1e2880d8c079e34a700aea7884e557e0d")),
    "owendo": Territory("Owendo", (P2021_01_31, P2025_01_15, P2025_01_25), (
        "16ee90179e5441b1c654ba85b5915c0a570eae14d70cc2ada6c9b3a3e1e79e7b",
        "5d0e5e513f4dc3b461dda739cbb16423b89c19aa5557286a9552f040814bcd1f",
        "77a8d2770a78a3561b327a506971856a616c7e7dc4f18f5faa230839f632c95c",
        "8828a97955f41f7ac78bd55631353c0a44c78758792316d1459b655346039353")),
}
TERRITORY_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
METERS_PER_DEGREE = 111_320.0  # At the equator; only used to express one source pixel in degrees.
SENTINEL2_ATTRIBUTION = "Contains modified Copernicus Sentinel data 2021 and 2025, processed by GEONAF"


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


def check_territories(territories: dict) -> None:
    """Strict identifiers, no product listed twice, expected layer kinds (refusal before any write)."""
    if not territories:
        raise ValueError("Aucun territoire à empaqueter.")
    for aoi_id, territory in territories.items():
        if not isinstance(aoi_id, str) or not TERRITORY_ID.fullmatch(aoi_id):
            raise ValueError(f"Identifiant de territoire invalide : {aoi_id!r}")
        if not territory.products or len(set(territory.products)) != len(territory.products):
            raise ValueError(f"Produits absents ou en double pour {aoi_id}.")
        if (len(territory.descriptors) != len(EXPECTED_LAYERS)
                or len(set(territory.descriptors)) != len(territory.descriptors)):
            raise ValueError(f"Couches externes incomplètes ou en double pour {aoi_id}.")


def _aoi_bounds_wgs84(aoi_path: Path) -> tuple[float, ...]:
    return tuple(float(value) for value in gpd.read_file(aoi_path).to_crs("EPSG:4326").total_bounds)


def build_sentinel2(data_dir: Path, output: Path, aoi_id: str, products, aoi_path: Path, created_at: datetime,
                    software: dict) -> list[dict]:
    """Native-grid subsets of every product for one territory, in ``<product>__<aoi_id>`` folders."""
    aoi_record = {"id": aoi_id, "path": f"aoi/{aoi_id}.geojson", "sha256": sha256_file(aoi_path),
                  "nature": "carré d'acquisition 12 × 12 km ; pas une unité administrative"}
    records = []
    for product_id in products:
        source_dir = data_dir / "raw" / "sentinel2" / product_id
        parent, parent_sha256 = _verified_parent(source_dir)
        b04 = source_dir / parent["assets"]["b04"]["path"]
        with rasterio.open(b04) as tile:
            aoi_bounds = tuple(gpd.read_file(aoi_path).to_crs(tile.crs).total_bounds)
            bounds = aligned_subset_bounds(aoi_bounds, tile.transform, (tile.width, tile.height),
                                           margin_m=MARGIN_M)
        target_dir = output / "raw" / "sentinel2" / f"{product_id}__{aoi_id}"
        assets = {}
        for role in ("b04", "b08", "scl"):
            source = source_dir / parent["assets"][role]["path"]
            name = Path(parent["assets"][role]["path"]).stem + f"_{aoi_id}.tif"
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
        manifest = build_subset_manifest(parent_manifest=parent, parent_manifest_sha256=parent_sha256,
                                         assets=assets, aoi={**aoi_record, "bounds_tile_crs": list(aoi_bounds)},
                                         margin_m=MARGIN_M, created_at=created_at, software=software)
        write_json(manifest, target_dir / "subset_manifest.json")
        records.append({"aoi_id": aoi_id, "product_id": product_id, "acquired_at_utc": parent["acquired_at_utc"],
                        "ingested_at_utc": parent["ingested_at_utc"],
                        "processing_baseline": product_id.split("_")[3],
                        "subset_bounds": list(bounds), "crs": assets["b04"]["crs"],
                        "source_manifest_sha256": parent_sha256,
                        "source_assets_sha256": {role: record["sha256"]
                                                 for role, record in parent["assets"].items()}})
    if len({tuple(record["subset_bounds"]) for record in records}) != 1:
        raise ValueError(f"Les extraits de {aoi_id} n'ont pas la même emprise.")
    return records


def measure_quality(output: Path, records: list[dict]) -> None:
    """Valid AOI coverage per territory and date (reference profile), measured on the subsets just written.

    A date below the profile minimum is flagged ``trend_use = excluded_low_coverage``; the
    NDVI series already excludes it, and the flag keeps the reason visible in the pack.
    """
    from src.data.acquisition import create_sentinel2_acquisition
    from src.geo.scientific_profile import reference_profile
    from src.services.ndvi_observation import analyze_ndvi_observation

    for record in records:
        profile = reference_profile(record["aoi_id"])
        directory = output / "raw" / "sentinel2" / f"{record['product_id']}__{record['aoi_id']}"
        manifest = json.loads((directory / "subset_manifest.json").read_text(encoding="utf-8"))
        paths = {role: directory / asset["path"] for role, asset in manifest["assets"].items()}
        acquisition = create_sentinel2_acquisition(
            product_id=manifest["product_id"],
            acquired_at=datetime.fromisoformat(manifest["acquired_at_utc"].replace("Z", "+00:00")),
            b04_path=paths["b04"], b08_path=paths["b08"], scl_path=paths["scl"],
            calibration_xml_path=paths["calibration_xml"],
            asset_product_ids={role: asset["product_id"] for role, asset in manifest["assets"].items()})
        observation = analyze_ndvi_observation(acquisition, aoi_path=output / "aoi" / f"{record['aoi_id']}.geojson",
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


def build_external(data_dir: Path, output: Path, aoi_id: str, descriptors, aoi_path: Path) -> list[dict]:
    """Byte-identical layers of one territory; each descriptor must cover the AOI (wrong square refused)."""
    left, bottom, right, top = _aoi_bounds_wgs84(aoi_path)
    layers = []
    for expected_source, fingerprint in zip(EXPECTED_LAYERS, descriptors):
        source = data_dir / "external" / "catalogue" / f"{fingerprint}.json"
        record = json.loads(source.read_text(encoding="utf-8"))
        descriptor, key = record["descriptor"], record["storage_key"]
        if descriptor["fingerprint"] != fingerprint or Path(key).is_absolute() or ".." in Path(key).parts:
            raise ValueError(f"Descripteur incohérent : {fingerprint}")
        if descriptor["source_id"] != expected_source:
            raise ValueError(f"Source inattendue pour {aoi_id} : {descriptor['source_id']}")
        west, south, east, north = descriptor["extent"]["wgs84_bounds"]
        # Raster windows are snapped to the source pixel grid (WorldCover: 1/12000°), so an edge may
        # stop up to one source pixel inside the AOI edge; vectors (no resolution) must cover it exactly.
        # A layer of another square is off by ≥ 0,05° and stays refused.
        resolution_m = descriptor.get("resolution_m") or 0.0
        tolerance = max(1e-6, float(resolution_m) / METERS_PER_DEGREE)
        if not (west <= left + tolerance and south <= bottom + tolerance and east >= right - tolerance
                and north >= top - tolerance):
            raise ValueError(f"La couche {fingerprint[:12]} ne couvre pas la zone {aoi_id}.")
        expected = descriptor["quality"].get("checksum_sha256")
        if expected is None:
            raise ValueError(f"Couche sans empreinte : {fingerprint}")
        _copy_verified(source, output / "external" / "catalogue" / source.name)
        _copy_verified(data_dir / "external" / key, output / "external" / key, expected)
        layers.append({"aoi_id": aoi_id, "source_id": descriptor["source_id"], "product": descriptor.get("product"),
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


LIMITS = [
    "Extraits spatiaux de tuiles vérifiées localement : l'intégrité de la source a été contrôlée "
    "contre STAC à l'ingestion, pas recontrôlable depuis l'extrait seul.",
    "Dates affichées = dates d'acquisition ; aucune acquisition postérieure au 25/01/2025 dans le paquet.",
    "Owendo : aucune paire 2025 exploitable (15/01/2025 presque entièrement nuageux) ; seule la "
    "comparaison 31/01/2021 → 25/01/2025 dispose d'une couverture commune suffisante.",
    "2021 (Sentinel-2A, traitement N0500) et 2025 (Sentinel-2A/2B, N0511) : même quantification BOA et "
    "même décalage, mais satellites et versions de traitement différents ; marée et humidité non "
    "corrigées. Une variation de NDVI n'est pas une déforestation démontrée.",
    "Profils Libreville et Owendo : paramètres d'Akanda, expérimentaux et non calibrés.",
    "WorldCover 2021 et l'instantané OSM ne décrivent pas l'état du territoire aux dates Sentinel-2 ; "
    "OSM peut être incomplet au Gabon (absence d'objet ≠ absence sur le terrain).",
    "Les deux référentiels de limites diffèrent (sources et années différentes) : une divergence "
    "entre eux n'est pas un changement du territoire.",
]


def _created_at(value: str | None) -> datetime:
    """Creation time written in the manifests: now, or a fixed UTC value for a reproducible rebuild."""
    if value is None:
        return datetime.now(timezone.utc)
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None or moment.utcoffset().total_seconds() != 0:
        raise ValueError("--created-at doit être une date UTC explicite (ex. 2026-11-10T12:00:00Z).")
    return moment


def build_pack(data_dir: Path, output: Path, *, created_at: datetime, code: dict, territories=None) -> dict:
    """Write the pack directory (refused if it exists) and return its manifest."""
    territories = TERRITORIES if territories is None else territories
    if output.exists():
        raise FileExistsError("Le dossier de sortie existe déjà ; rien n'est écrasé.")
    check_territories(territories)
    software = software_versions(code)
    sentinel2, external, statements = [], [], []
    for aoi_id, territory in territories.items():
        aoi_path = data_dir / "aoi" / f"{aoi_id}.geojson"
        _copy_verified(aoi_path, output / "aoi" / aoi_path.name)
        sentinel2 += build_sentinel2(data_dir, output, aoi_id, territory.products, aoi_path, created_at, software)
        external += build_external(data_dir, output, aoi_id, territory.descriptors, aoi_path)
        statements.append({"id": aoi_id, "label": territory.label, "path": f"aoi/{aoi_id}.geojson",
                           "sha256": sha256_file(aoi_path), "crs": "EPSG:4326",
                           "products": list(territory.products)})
    measure_quality(output, sentinel2)
    boundaries = build_boundaries(data_dir, output)
    files = pack_inventory(output)
    labels = ", ".join(territory.label for territory in territories.values())
    manifest = {
        "pack_id": PACK_ID, "pack_version": PACK_VERSION,
        "created_at_utc": created_at.isoformat().replace("+00:00", "Z"), "software": software,
        "code": {"repository": "geonaf", **code},
        "source_data": {"fingerprint_sha256": source_data_fingerprint(sentinel2, external, boundaries),
                        "note": "Données sources hors Git : identifiées par les empreintes de leurs "
                                "manifestes et fichiers, vérifiées au moment de la construction."},
        "aoi": {"ids": list(territories), "territories": statements,
                "statement": f"Zones d'acquisition historiques de 12 × 12 km autour de {labels}. Ce ne sont "
                             "ni des communes ni d'autres unités administratives."},
        "sentinel2": {"collection": "sentinel-2-l2a", "tile": "T32NNF", "attribution": SENTINEL2_ATTRIBUTION,
                      "license": "Copernicus Sentinel data terms (accès libre, attribution requise)",
                      "subset_margin_m": MARGIN_M, "acquisitions": sentinel2},
        "external_layers": external,
        "boundaries": {
            "statement": "Référentiels geoBoundaries gbOpen (communautaire) et gbHumanitarian (humanitaire, "
                         "OCHA COD-AB via SALB/HDX) : limites non officielles, non vérifiées, sans valeur "
                         "juridique. Aucune limite administrative officielle n'est fournie.",
            "releases": boundaries},
        "limits": list(LIMITS),
        "files": files,
        "total_bytes": sum(item["bytes"] for item in files),
    }
    write_json(manifest, output / "PACK_MANIFEST.json")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DATA_DIR / "demo_pack" / f"{PACK_ID}-v{PACK_VERSION}")
    parser.add_argument("--created-at", help="Date de création UTC fixe (reconstruction reproductible).")
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
    manifest = build_pack(DATA_DIR, output, created_at=_created_at(args.created_at), code=code)
    digest = create_archive(output, archive)
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"Paquet écrit : {len(manifest['files'])} fichiers, {manifest['total_bytes'] / 1e6:.1f} Mo")
    print(f"Archive : {archive.name}, {archive.stat().st_size / 1e6:.1f} Mo, SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
