"""Manual live diagnostic (network): WorldCover window over Akanda joined to the S2 pair.

Reads only the COG window covering the AOI via HTTP Range (/vsicurl/), never the
full tile. Writes data/external/worldcover/<name>_akanda.tif once; an existing
file is reused, never overwritten. Not part of pytest. Run from the repo root.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.windows import from_bounds

from src.config import DATA_DIR
from src.data.network import secure_session
from src.data.worldcover import BUCKET_URL, WorldCoverAdapter
from src.network_config import NetworkSettings
from src.geo.scientific_profile import GABON_EQUATORIAL_EXPERIMENTAL as PROFILE
from src.services.local_preview import analyze_local_akanda, list_local_observations
from src.services.multisource import join_categorical_raster


class RangeFile:
    """Read-only file over HTTPS Range requests using GEONAF's TLS session."""

    BLOCK = 256 * 1024

    def __init__(self, session, url):
        if not url.startswith(BUCKET_URL + "/"):
            raise ValueError("Hôte non autorisé.")
        self.session, self.url, self.position, self.blocks, self.received = session, url, 0, {}, 0
        head = session.head(url, timeout=30)
        head.raise_for_status()
        self.size = int(head.headers["Content-Length"])

    closed = False

    def _fetch(self, index):
        if index not in self.blocks:
            start = index * self.BLOCK
            end = min(self.size, start + self.BLOCK) - 1
            response = self.session.get(self.url, headers={"Range": f"bytes={start}-{end}"}, timeout=60)
            if response.status_code != 206 or len(response.content) != end - start + 1:
                raise OSError("Réponse Range inattendue.")
            self.received += len(response.content)
            self.blocks[index] = response.content
        return self.blocks[index]

    def read(self, size=-1):
        if size is None or size < 0:
            size = self.size - self.position
        size = max(0, min(size, self.size - self.position))
        chunks, position = [], self.position
        while size > 0:
            index, offset = divmod(position, self.BLOCK)
            chunk = self._block(index)[offset:offset + size]
            chunks.append(chunk)
            position += len(chunk)
            size -= len(chunk)
        self.position = position
        return b"".join(chunks)

    def seek(self, offset, whence=0):
        self.position = {0: offset, 1: self.position + offset, 2: self.size + offset}[whence]
        return self.position

    def tell(self):
        return self.position

    def view(self, path, mode="rb"):
        """rasterio opener: the tile only (no sidecars), one cursor per open."""
        if path != self.url or "w" in mode:
            raise FileNotFoundError(path)
        view = RangeFile.__new__(RangeFile)
        view.__dict__.update(self.__dict__, position=0)
        view._parent, view.closed = self, False
        return view

    def _block(self, index):  # Views share the parent's block cache and byte counter.
        parent = self.__dict__.get("_parent")
        return parent._block(index) if parent is not None else self._fetch(index)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def main():
    aoi_path = DATA_DIR / "aoi" / f"{PROFILE.aoi_id}.geojson"
    bounds = tuple(gpd.read_file(aoi_path).to_crs("EPSG:4326").total_bounds)
    adapter = WorldCoverAdapter()
    (url,) = adapter.plan(bounds).resources
    output = DATA_DIR / "external" / "worldcover" / f"{Path(url).stem}_{PROFILE.aoi_id}.tif"
    if not output.exists():
        output.parent.mkdir(parents=True, exist_ok=True)
        session = secure_session(NetworkSettings.from_environment())
        remote = RangeFile(session, url)
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
            with rasterio.open(url, opener=remote.view) as source:
                window = from_bounds(*bounds, transform=source.transform).round_offsets().round_lengths()
                data = source.read(1, window=window, boundless=False)
                profile = source.profile.copy()
                profile.update(width=data.shape[1], height=data.shape[0],
                               transform=source.window_transform(window), driver="GTiff",
                               compress="deflate", tiled=False)
                profile.pop("blockxsize", None), profile.pop("blockysize", None)
        with rasterio.open(output, "w", **profile) as destination:  # Absence checked above.
            destination.write(data, 1)
        print(f"Octets reçus par HTTP Range : {remote.received} sur {remote.size} (tuile complète).")
    descriptor = adapter.describe_local(output, retrieved_at=datetime.now(timezone.utc), source_url=url)
    observations = {o.acquired_at.date().isoformat(): o for o in list_local_observations(DATA_DIR)}
    pair = analyze_local_akanda(observations["2025-01-15"], observations["2025-01-25"], DATA_DIR,
                                scientific_profile=PROFILE)
    mask = pair.analysis.comparable_mask
    report = {"window_bytes_on_disk": output.stat().st_size, "layer": descriptor.product,
              "layer_sha256": descriptor.quality.checksum_sha256}
    for name, values in (("ndvi_2025-01-25", pair.comparison.ndvi_date_2), ("delta_ndvi", pair.analysis.delta_ndvi)):
        joined = join_categorical_raster(pair.common_grid, values, mask, observed_at=pair.later.acquired_at_utc,
                                         layer_path=output, descriptor=descriptor, aoi_path=aoi_path)
        report[name] = {zone.label: {"pixels": zone.pixel_count, "ha": round((zone.area_m2 or 0) / 1e4, 2),
                                     "mean": None if zone.mean is None else round(zone.mean, 4)}
                        for zone in joined.zones if zone.pixel_count}
        report["temporal"] = joined.temporal.message
        report["unmatched_pixels"] = joined.unmatched_pixels
        report["common_pixels"] = int(np.count_nonzero(mask))
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
