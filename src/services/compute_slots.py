"""Bound the number of heavy scientific computations running at the same time in one process.

Why: three sessions opening the home page on cold caches peaked at 572 MiB (C4a audit,
30/10/2026); each resource guard reads the free memory at its own check, so concurrent
computations can all pass and then add up. All Streamlit sessions of a server process share
these slots.

* Only cache misses of memory-heavy computations take a slot (pair analysis, NDVI of an
  acquisition, multisource crossing, radar and OSM loading, L3 inventory, L4 pair selection,
  key figures, NDVI series); cache hits and light interactions never wait.
* Re-entrant per thread: a heavy computation that calls another one keeps its slot (no
  self-deadlock with one slot).
* Bounded wait, then :class:`ComputeBusyError`, a functional error with a safe message. The
  slot is always released (``finally``), including on errors and ``st.stop``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import threading

DEFAULT_SLOTS = 2
DEFAULT_WAIT_SECONDS = 90.0


class ComputeBusyError(RuntimeError):
    """Every heavy-computation slot stayed busy during the allowed wait; the message is for users."""


def _positive(environ, name, default, cast):
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise ValueError(f"{name} doit être strictement positif.")
    return value


@dataclass(frozen=True)
class SlotSettings:
    slots: int = DEFAULT_SLOTS
    wait_seconds: float = DEFAULT_WAIT_SECONDS

    @classmethod
    def from_environment(cls, environ=os.environ) -> "SlotSettings":
        return cls(_positive(environ, "GEONAF_MAX_HEAVY_COMPUTATIONS", DEFAULT_SLOTS, int),
                   _positive(environ, "GEONAF_HEAVY_WAIT_SECONDS", DEFAULT_WAIT_SECONDS, float))


class ComputeSlots:
    def __init__(self, settings: SlotSettings):
        self.settings = settings
        self._semaphore = threading.BoundedSemaphore(settings.slots)
        self._local = threading.local()
        self._lock = threading.Lock()
        self.active = 0
        self.waiting = 0
        self.refused = 0

    @contextmanager
    def heavy(self):
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth
            return
        with self._lock:
            self.waiting += 1
        try:
            acquired = self._semaphore.acquire(timeout=self.settings.wait_seconds)
        finally:
            with self._lock:
                self.waiting -= 1
        if not acquired:
            with self._lock:
                self.refused += 1
            raise ComputeBusyError(
                "Serveur occupé : d'autres calculs sont en cours. Réessayer dans quelques instants ; "
                "aucun résultat partiel n'a été affiché.")
        with self._lock:
            self.active += 1
        self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            with self._lock:
                self.active -= 1
            self._semaphore.release()


_shared: ComputeSlots | None = None
_shared_lock = threading.Lock()


def compute_slots() -> ComputeSlots:
    """The process-wide instance (all Streamlit sessions of the server process share it)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = ComputeSlots(SlotSettings.from_environment())
        return _shared


def heavy_computation():
    """Context manager taking one shared slot for a heavy computation (see module docstring)."""
    return compute_slots().heavy()
