"""AOI search and metadata selection use cases; no UI, pixel reads or downloads."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import geopandas as gpd

from src.data.catalogue import CatalogueAOI, CatalogueError, CatalogueQuery, CatalogueResult, CatalogueRepository
from src.data.stac_catalogue import CopernicusCatalogue
from src.geo.aoi import get_aoi_bbox
from src.services.local_preview import _manifest, manifest_path


LOCAL_STATE_LABELS = {
    "absent": "Absent localement",
    "manifest_present": "Présent — manifeste strict, fichiers à revérifier avant analyse",
    "subset_present": "Extrait de démonstration — dérivé d'une tuile vérifiée, fichiers à revérifier",
    "partial": "Partiel — fichiers ou manifeste manquants",
    "unverified": "Non vérifié — manifeste ou chemins invalides",
    "ambiguous": "Ambigu — plusieurs produits locaux pour cette identité",
}


def available_catalogue_aois(data_dir: Path) -> tuple[str, ...]:
    return tuple(sorted(path.stem for path in (data_dir / "aoi").glob("*.geojson")
                        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", path.stem)))


def load_catalogue_aoi(data_dir: Path, aoi_id: str) -> CatalogueAOI:
    try:
        if not isinstance(aoi_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", aoi_id):
            raise ValueError
        root = (data_dir / "aoi").resolve()
        path = (root / f"{aoi_id}.geojson").resolve()
        if not path.is_relative_to(root) or path.stat().st_size > 8 * 1024**2:
            raise ValueError
        # Reuse the existing CRS, coordinate, topology and finite-value checks.
        bbox = tuple(get_aoi_bbox(path))
        aoi = gpd.read_file(path).to_crs("EPSG:4326")
        geometry = aoi.geometry.union_all()
        if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError
        encoded = json.dumps(geometry.__geo_interface__, sort_keys=True, allow_nan=False)
        return CatalogueAOI(aoi_id, bbox, encoded, hashlib.sha256(path.read_bytes()).hexdigest())
    except Exception:
        raise CatalogueError("AOI indisponible ou invalide. Vérifier sa géométrie et son CRS.") from None


def _local_states(data_dir):
    """Read manifests once, stat assets once; never claim full file integrity.

    Directory names without a valid manifest are only unverified presence hints.
    Full SHA-256/raster validation remains mandatory in the scientific service.
    """
    root = (data_dir / "raw" / "sentinel2").resolve()
    states = {}
    for folder in sorted(root.glob("*")):
        if not folder.is_dir():
            continue
        identity = ("sentinel-2-l2a", folder.name)
        state, ingested = "partial", None
        path = None
        try:
            if not folder.resolve().is_relative_to(root):
                raise ValueError
            path = manifest_path(folder) or folder / "acquisition_manifest.json"
            if path.exists():
                if path.stat().st_size > 2 * 1024**2 or not path.resolve().is_relative_to(folder.resolve()):
                    raise ValueError
                manifest = _manifest(path)
                identity = (manifest["collection"], manifest["product_id"])
                complete = True
                for asset in manifest["assets"].values():
                    target = (folder / asset["path"]).resolve()
                    if not target.is_relative_to(folder.resolve()):
                        raise ValueError
                    complete &= target.is_file() and target.stat().st_size > 0
                subset = path.name == "subset_manifest.json"
                state = ("subset_present" if subset else "manifest_present") if complete else "partial"
                ingested_text = manifest["parent"]["ingested_at_utc"] if subset else manifest["ingested_at_utc"]
                ingested = datetime.fromisoformat(ingested_text.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            state, ingested = "unverified", None
            # A rejected manifest may still declare an identity hint. This is
            # never integrity evidence; avoid falsely labelling it absent.
            try:
                if (path is not None and folder.resolve().is_relative_to(root)
                        and path.resolve().is_relative_to(folder.resolve())
                        and path.stat().st_size <= 2 * 1024**2):
                    hint = json.loads(path.read_text(encoding="utf-8"))
                    identifier = hint.get("product_id")
                    if (hint.get("collection") == "sentinel-2-l2a" and isinstance(identifier, str)
                            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", identifier)):
                        identity = ("sentinel-2-l2a", identifier)
            except Exception:
                pass
        if identity in states:
            states[identity] = ("ambiguous", None)
        else:
            states[identity] = (state, ingested)
    return states


def refresh_local_states(result: CatalogueResult, data_dir: Path) -> CatalogueResult:
    try:
        states = _local_states(data_dir)
    except OSError:
        raise CatalogueError("État local indisponible ; vérifier les permissions du répertoire de données.") from None
    return replace(result, records=tuple(
        replace(row, local_state=states.get(row.identity, ("absent", None))[0],
                ingested_at_utc=states.get(row.identity, ("absent", None))[1])
        for row in result.records
    ))


def search_catalogue(query: CatalogueQuery, data_dir: Path, *,
                     repository: CatalogueRepository | None = None) -> CatalogueResult:
    try:
        result = (repository if repository is not None else CopernicusCatalogue()).search(query)
        if (result.query != query or len({row.identity for row in result.records}) != len(result.records)
                or any(row.collection != "sentinel-2-l2a" for row in result.records)):
            raise CatalogueError("Résultat de catalogue incohérent avec la recherche.")
        return refresh_local_states(result, data_dir)
    except CatalogueError:
        raise
    except Exception:
        raise CatalogueError("Catalogue indisponible ; aucune sélection précédente ne doit être réutilisée.") from None


def select_acquisitions(result: CatalogueResult, stac_ids) -> CatalogueResult:
    """A metadata-only selection with the exact query/source/time provenance.

    No pair or series is constructed; multiple selection never downloads assets.
    ``complete`` describes the parent search, not the selected subset.
    """
    by_id = {row.stac_id: row for row in result.records}
    selected = tuple(stac_ids)
    if len(set(selected)) != len(selected) or any(key not in by_id for key in selected):
        raise CatalogueError("Sélection invalide ou issue d'une autre recherche.")
    return replace(result, records=tuple(by_id[key] for key in selected))


def catalogue_rows(result: CatalogueResult) -> list[dict]:
    """Explicit public metadata; never expose paths, asset URLs or manifests."""
    return [{
        "Acquisition UTC": row.acquired_at_utc.isoformat().replace("+00:00", "Z"),
        "Identifiant STAC": row.stac_id, "Nuages globaux (%)": row.global_cloud_cover,
        "B04": row.b04_available, "B08": row.b08_available, "SCL": row.scl_available,
        "État local": LOCAL_STATE_LABELS[row.local_state],
        "Ingestion UTC": row.ingested_at_utc.isoformat().replace("+00:00", "Z") if row.ingested_at_utc else None,
    } for row in result.records]
