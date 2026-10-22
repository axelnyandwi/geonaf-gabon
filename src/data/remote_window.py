"""Bounded window reads of remote Cloud Optimized GeoTIFFs (HTTP Range or S3 Range).

Principle: never download a whole tile. GDAL reads the COG header through a
Python file object whose bytes come from Range requests made with GEONAF's
verified transports (GDAL/curl is not used for the network). Before any pixel
block is fetched, the exact compressed size of the internal tiles covering the
window is read from the TIFF tile index and compared with an explicit byte
budget; the read is refused when the estimate exceeds it.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import rasterio
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds


class ByteBudgetExceeded(ValueError):
    """Domain refusal (never retried): the authorised byte budget is exhausted."""


class ByteBudget:
    def __init__(self, max_bytes: int):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("Budget d'octets positif requis.")
        self.max_bytes, self.used, self.exceeded = max_bytes, 0, False

    @property
    def remaining(self) -> int:
        return self.max_bytes - self.used

    def consume(self, count: int) -> None:
        if self.used + count > self.max_bytes:
            self.exceeded = True  # GDAL may wrap the exception; the flag survives.
            raise ByteBudgetExceeded("Budget réseau dépassé : lecture interrompue.")
        self.used += count


class HttpRangeSource:
    """Range source over an allow-listed :class:`ExternalHttpClient`."""

    def __init__(self, client, url: str):
        client.policy(url)  # Fails closed on a non-authorised host.
        self.client, self.url = client, url
        head = client.request("HEAD", url)
        length = head.headers.get("Content-Length")
        if length is None or not str(length).isdigit():
            raise ValueError("Taille distante inconnue.")
        self.size = int(length)

    def fetch(self, start: int, end: int) -> bytes:
        response = self.client.request("GET", self.url, headers={"Range": f"bytes={start}-{end}"},
                                       max_bytes=end - start + 1, expected=(206,))
        return response.content


class S3RangeSource:
    """Range source over GEONAF's S3 client (single SDK attempt, GEONAF retries)."""

    def __init__(self, client, key: str, *, bucket: str = "eodata", settings=None):
        from src.data.network import run_with_retry
        from src.network_config import NetworkSettings

        if not isinstance(key, str) or not key or key.startswith("/") or ".." in key.split("/"):
            raise ValueError("Clé S3 invalide.")
        self.client, self.key, self.bucket = client, key, bucket
        self.settings = settings or NetworkSettings.from_environment()
        self._retry = run_with_retry
        head = self._retry(lambda: client.head_object(Bucket=bucket, Key=key), self.settings, service="s3")
        self.size = int(head["ContentLength"])

    def fetch(self, start: int, end: int) -> bytes:
        def action():
            response = self.client.get_object(Bucket=self.bucket, Key=self.key, Range=f"bytes={start}-{end}")
            body = response["Body"]
            try:
                payload = body.read(end - start + 2)
            finally:
                body.close()
            if len(payload) != end - start + 1:
                raise ValueError("Réponse Range S3 de taille inattendue.")
            return payload
        return self._retry(action, self.settings, service="s3")


class RangeFile:
    """Read-only, seekable file over a range source with a shared block cache and budget."""

    BLOCK = 64 * 1024
    closed = False

    def __init__(self, source, budget: ByteBudget, *, label: str):
        self.source, self.budget, self.label = source, budget, label
        self.size, self.position, self.received = source.size, 0, 0
        self.blocks: dict[int, bytes] = {}
        self._parent = None

    def _root(self):
        return self._parent if self._parent is not None else self

    def _block(self, index: int) -> bytes:
        root = self._root()
        if index not in root.blocks:
            start = index * root.BLOCK
            end = min(root.size, start + root.BLOCK) - 1
            root.budget.consume(end - start + 1)
            payload = root.source.fetch(start, end)
            if len(payload) != end - start + 1:
                raise OSError("Réponse Range inattendue.")
            root.received += len(payload)
            root.blocks[index] = payload
        return root.blocks[index]

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
        """rasterio opener: only this object (no sidecar files), one cursor per open."""
        if path != self.label or "w" in mode:
            raise FileNotFoundError(path)
        view = RangeFile.__new__(RangeFile)
        view.__dict__.update(self.__dict__, position=0, closed=False)
        view._parent = self._root()
        return view

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@dataclass(frozen=True)
class WindowEstimate:
    window: Window
    tiles: int
    compressed_bytes: int  # Exact sum of the internal tiles covering the window.
    width: int
    height: int


def estimate_window(source, bounds, bounds_crs) -> WindowEstimate:
    """Window covering ``bounds`` on the full-resolution image and the bytes needed to read it."""
    native = bounds if rasterio.crs.CRS.from_user_input(bounds_crs) == source.crs else transform_bounds(
        bounds_crs, source.crs, *bounds, densify_pts=21)
    raw = from_bounds(*native, transform=source.transform)
    col0, row0 = max(0, math.floor(raw.col_off)), max(0, math.floor(raw.row_off))
    col1 = min(source.width, math.ceil(raw.col_off + raw.width))
    row1 = min(source.height, math.ceil(raw.row_off + raw.height))
    if col1 <= col0 or row1 <= row0:
        raise ValueError("L'emprise demandée ne recoupe pas le raster distant.")
    window = Window(col0, row0, col1 - col0, row1 - row0)
    block_h, block_w = source.block_shapes[0]
    total, tiles = 0, 0
    for by in range(row0 // block_h, (row1 - 1) // block_h + 1):
        for bx in range(col0 // block_w, (col1 - 1) // block_w + 1):
            size = source.get_tag_item(f"BLOCK_SIZE_{bx}_{by}", "TIFF", bidx=1)
            if size is None:
                raise ValueError("Index des tuiles COG illisible : estimation impossible.")
            total += int(size)
            tiles += 1
    return WindowEstimate(window, tiles, total, window.width, window.height)


def read_remote_window(range_file: RangeFile, bounds, bounds_crs, *, max_pixels: int = 4_000_000):
    """Return (array, profile, estimate) for the window; refuse when it exceeds the budget.

    Only the first band is read at full resolution. ``profile`` is a plain
    GeoTIFF profile ready for writing (no tiling of the remote file kept).
    """
    try:
        return _read_remote_window(range_file, bounds, bounds_crs, max_pixels)
    except ByteBudgetExceeded:
        raise
    except Exception:
        if range_file.budget.exceeded:
            raise ByteBudgetExceeded("Budget réseau dépassé pendant la lecture : fenêtre abandonnée.") from None
        raise


def _read_remote_window(range_file, bounds, bounds_crs, max_pixels):
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
        with rasterio.open(range_file.label, opener=range_file.view) as source:
            if source.crs is None:
                raise ValueError("Raster distant sans CRS.")
            estimate = estimate_window(source, bounds, bounds_crs)
            if estimate.width * estimate.height > max_pixels:
                raise ValueError("Fenêtre trop grande pour une lecture bornée.")
            if estimate.compressed_bytes > range_file.budget.remaining:
                raise ByteBudgetExceeded(
                    f"Lecture refusée : {estimate.compressed_bytes} octets estimés pour un budget restant "
                    f"de {range_file.budget.remaining} octets.")
            data = source.read(1, window=estimate.window)
            profile = source.profile.copy()
            profile.update(driver="GTiff", width=data.shape[1], height=data.shape[0],
                           transform=source.window_transform(estimate.window), compress="deflate", tiled=False)
            for key in ("blockxsize", "blockysize", "interleave", "photometric"):
                profile.pop(key, None)
    return np.asarray(data), profile, estimate
