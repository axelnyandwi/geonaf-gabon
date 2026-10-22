"""Transfer limits and atomic files, independent of UI and scientific rules."""

from dataclasses import dataclass
import os
import logging
from pathlib import Path
from threading import Event, RLock
from uuid import uuid4


class TransferAborted(ValueError):
    """Non-retryable local policy failure; never treated as a network failure."""


class TransferCancelled(TransferAborted):
    pass


@dataclass(frozen=True)
class DownloadBudget:
    storage_bytes: int = 1024**3
    network_bytes: int = 2 * 1024**3
    memory_bytes: int = 64 * 1024**2
    reserve_bytes: int = 512 * 1024**2
    workers: int = 1

    def __post_init__(self):
        values = (self.storage_bytes, self.network_bytes, self.memory_bytes, self.reserve_bytes, self.workers)
        if (any(isinstance(x, bool) or not isinstance(x, int) or x < 1 for x in values)
                or self.workers > 2 or self.memory_bytes < self.workers * 16 * 1024**2):
            raise ValueError("Budgets invalides : 1–2 transferts, au moins 16 Mio par transfert.")


class TransferControl:
    def __init__(self, budget: DownloadBudget, cancelled: Event):
        self.budget, self.cancelled = budget, cancelled
        self.transferred = 0
        self._lock = RLock()

    def check(self):
        if self.cancelled.is_set():
            raise TransferCancelled("Annulation demandée ; reprise possible après nouvelle validation.")

    def consume(self, size):
        self.check()
        if size > 0:
            with self._lock:
                self.transferred += size  # Includes failed attempts, never refunded.
                if self.transferred > self.budget.network_bytes:
                    raise TransferAborted("Budget réseau dépassé ; augmenter le budget avant reprise.")


_LOCAL_WRITE_ERRORS = (FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError)


def atomic_download(client, key: str, destination: Path, options, control):
    """SDK transport/retries remain with ingestion; publish only a closed file.

    Incomplete .part files are retained in managed staging, never reused as assets.
    Chunk callbacks bound cancellation latency by SDK chunks/timeouts, not hard real time.
    """
    control.check()
    temporary = destination.with_name(destination.name + "." + uuid4().hex + ".part")
    try:
        client.download_file("eodata", key, str(temporary), Callback=control.consume, **options)
        control.check()
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise TransferAborted("Transfert vide ou incomplet.")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        if destination.exists():
            raise TransferAborted("Collision de fichier ; aucun fichier existant remplacé.")
        temporary.rename(destination)
    except TransferAborted:
        raise
    except _LOCAL_WRITE_ERRORS as error:
        # Local file-system failures (MAX_PATH, OneDrive/antivirus sharing locks, rights)
        # are neither network errors nor retryable; never report them as transport faults.
        logging.getLogger(__name__).warning("asset_local_write_failed type=%s errno=%s winerror=%s",
                                           type(error).__name__, error.errno, getattr(error, "winerror", None))
        raise TransferAborted("Écriture locale impossible (chemin trop long, fichier verrouillé par "
                              "OneDrive/antivirus ou droits) ; fichiers conservés.") from None
    except Exception as error:
        logging.getLogger(__name__).warning("asset_transfer_failed type=%s errno=%s",
                                           type(error).__name__, getattr(error, "errno", None))
        raise
    return destination


def atomic_metadata(destination: Path, payload: bytes):
    temporary = destination.with_name(destination.name + "." + uuid4().hex + ".part")
    with temporary.open("xb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    if destination.exists():
        raise TransferAborted("Collision de métadonnées ; aucun fichier existant remplacé.")
    temporary.rename(destination)
    return destination
