"""Process-wide cache of large scientific results, bounded in bytes and shared between sessions.

Why: Streamlit's ``max_entries`` bounds a number of entries, not memory; one multisource entry
retains 19 to 61 MiB of arrays (C4a audit, 30/10/2026). This cache retains at most
``max_bytes`` of *distinct* memory and evicts the least recently used entries first.

Accounting. Every numpy array reachable from a value is reduced to the array that owns its
memory (end of the ``.base`` chain); each owner is counted once for the whole cache, even when
several entries (or a view and its base) share it. pandas objects are counted with
``memory_usage(deep=True)``, ``bytes`` with their length. Small Python objects are ignored: the
figure is the memory that eviction can actually release, not an exact process total.

Safety. Values are frozen before being shared: every reachable array, and the arrays its views
derive from, becomes read-only, so no session can modify another's result in place (pandas
objects are not frozen). One lock per key: concurrent requests for the same key compute once.
An evicted entry is recomputed on its next use from the same key; the computations cached
here are deterministic, so eviction never changes a result. A value larger than the whole
budget is returned but not retained.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, fields, is_dataclass
import os
import threading
from typing import Callable, Hashable

import numpy as np

DEFAULT_MAX_MIB = 256
MIB = 1024 ** 2


def _memory_owner(array: np.ndarray) -> np.ndarray:
    owner = array
    while isinstance(owner.base, np.ndarray):
        owner = owner.base
    return owner


def _is_pandas(value) -> bool:
    return type(value).__module__.split(".")[0] in {"pandas", "geopandas"}


def _children(value):
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    if is_dataclass(value) and not isinstance(value, type):
        return [getattr(value, field.name, None) for field in fields(value)]
    children = []
    if hasattr(value, "__dict__"):
        children.extend(vars(value).values())
    for slot in getattr(type(value), "__slots__", ()) or ():
        if isinstance(slot, str) and hasattr(value, slot):
            children.append(getattr(value, slot))
    return children


def _walk(value, visit_array, visit_other) -> None:
    """Depth-first traversal of containers, dataclasses and objects; arrays and pandas are leaves."""
    stack, seen = [value], set()
    while stack:
        item = stack.pop()
        if item is None or id(item) in seen or isinstance(item, (type, str, int, float, complex, bool)):
            continue
        seen.add(id(item))
        if isinstance(item, np.ndarray):
            visit_array(item)
        elif _is_pandas(item) or isinstance(item, (bytes, bytearray, memoryview)):
            visit_other(item)
        else:
            stack.extend(_children(item))


def retained_memory(value) -> tuple[dict[int, tuple[np.ndarray, int]], int]:
    """(distinct numpy owners {id: (owner, nbytes)}, other bytes) reachable from ``value``."""
    owners: dict[int, tuple[np.ndarray, int]] = {}
    other = [0]

    def array(item):
        owner = _memory_owner(item)
        owners[id(owner)] = (owner, int(owner.nbytes))

    def non_array(item):
        if isinstance(item, (bytes, bytearray, memoryview)):
            other[0] += len(item)
            return
        try:
            usage = item.memory_usage(deep=True)
            other[0] += int(usage.sum() if hasattr(usage, "sum") else usage)
        except Exception:
            pass

    _walk(value, array, non_array)
    return owners, other[0]


def freeze_arrays(value):
    """Make every reachable array and the arrays its views derive from read-only; return ``value``."""
    def array(item):
        current = item
        while isinstance(current, np.ndarray):
            try:
                current.setflags(write=False)
            except ValueError:
                pass  # Already read-only or not owning a writable buffer.
            current = current.base

    _walk(value, array, lambda item: None)
    return value


def max_bytes_from_environment(environ=os.environ) -> int:
    """``GEONAF_CACHE_MAX_MIB`` (positive integer, MiB), else :data:`DEFAULT_MAX_MIB`."""
    raw = environ.get("GEONAF_CACHE_MAX_MIB", "").strip()
    if not raw:
        return DEFAULT_MAX_MIB * MIB
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError("GEONAF_CACHE_MAX_MIB doit être un entier strictement positif (Mio).")
    return int(raw) * MIB


@dataclass(frozen=True)
class CacheStats:
    entries: int
    retained_bytes: int
    max_bytes: int
    hits: int
    misses: int
    evictions: int
    not_retained: int


class ByteBudgetCache:
    def __init__(self, max_bytes: int):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("Budget d'octets positif requis.")
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, tuple[object, frozenset[int], int]] = OrderedDict()
        self._owners: dict[int, list] = {}          # id -> [owner array, nbytes, reference count]
        self._other_bytes = 0
        self._key_locks: dict[tuple, threading.Lock] = {}
        self._hits = self._misses = self._evictions = self._not_retained = 0

    # -- public ------------------------------------------------------------------------------------

    def get_or_compute(self, namespace: str, key: Hashable, compute: Callable[[], object]):
        """Cached value of ``(namespace, key)``; ``compute()`` runs at most once at a time per key."""
        full_key = (namespace, key)
        with self._lock:
            value = self._hit(full_key)
            if value is not _MISSING:
                return value
            key_lock = self._key_locks.setdefault(full_key, threading.Lock())
        try:
            with key_lock:
                with self._lock:
                    value = self._hit(full_key)  # Computed by another session meanwhile.
                    if value is not _MISSING:
                        return value
                    self._misses += 1
                value = freeze_arrays(compute())
                self._store(full_key, value)
                return value
        finally:
            with self._lock:
                if self._key_locks.get(full_key) is key_lock and not key_lock.locked():
                    del self._key_locks[full_key]

    def peek(self, namespace: str, key: Hashable):
        """The cached value or ``None``; never computes and does not change the eviction order."""
        with self._lock:
            entry = self._entries.get((namespace, key))
            return None if entry is None else entry[0]

    def retained_bytes(self) -> int:
        with self._lock:
            return self._retained()

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(len(self._entries), self._retained(), self.max_bytes, self._hits, self._misses,
                              self._evictions, self._not_retained)

    def clear(self, namespace: str | None = None) -> None:
        with self._lock:
            for full_key in [k for k in self._entries if namespace is None or k[0] == namespace]:
                self._remove(full_key)

    # -- internals (caller holds self._lock unless stated) --------------------------------------------

    def _hit(self, full_key):
        entry = self._entries.get(full_key)
        if entry is None:
            return _MISSING
        self._entries.move_to_end(full_key)
        self._hits += 1
        return entry[0]

    def _retained(self) -> int:
        return sum(item[1] for item in self._owners.values()) + self._other_bytes

    def _store(self, full_key, value) -> None:
        # Sizes are measured outside the lock: traversal may take a few milliseconds.
        owners, other = retained_memory(value)
        if sum(nbytes for _, nbytes in owners.values()) + other > self.max_bytes:
            with self._lock:
                self._not_retained += 1
            return
        with self._lock:
            if full_key in self._entries:
                return
            for owner_id, (owner, nbytes) in owners.items():
                record = self._owners.get(owner_id)
                if record is None:
                    self._owners[owner_id] = [owner, nbytes, 1]
                else:
                    record[2] += 1
            self._other_bytes += other
            self._entries[full_key] = (value, frozenset(owners), other)
            while self._retained() > self.max_bytes and len(self._entries) > 1:
                oldest = next(iter(self._entries))
                if oldest == full_key:
                    break
                self._remove(oldest)
                self._evictions += 1

    def _remove(self, full_key) -> None:
        _, owner_ids, other = self._entries.pop(full_key)
        for owner_id in owner_ids:
            record = self._owners[owner_id]
            record[2] -= 1
            if record[2] == 0:
                del self._owners[owner_id]
        self._other_bytes -= other


_MISSING = object()
_shared: ByteBudgetCache | None = None
_shared_lock = threading.Lock()


def shared_cache() -> ByteBudgetCache:
    """The process-wide instance (all Streamlit sessions of the server process share it)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = ByteBudgetCache(max_bytes_from_environment())
        return _shared
