"""Byte-bounded shared cache: accounting, eviction, freezing and concurrency; offline."""

from dataclasses import dataclass
import threading

import numpy as np
import pandas as pd
import pytest

from src.services.bounded_cache import (
    MIB, ByteBudgetCache, freeze_arrays, max_bytes_from_environment, retained_memory,
)


@dataclass(frozen=True)
class Result:
    values: np.ndarray
    mask: np.ndarray
    label: str = "x"


def _array(mib, fill=1.0):
    return np.full(int(mib * MIB // 4), fill, dtype=np.float32)


def test_shared_buffers_are_counted_once():
    base = _array(4)
    view, other_view = base[::2], base[10:20]
    owners, other = retained_memory({"a": view, "b": (other_view, base), "c": Result(base, view)})
    assert len(owners) == 1 and sum(n for _, n in owners.values()) == base.nbytes and other == 0

    cache = ByteBudgetCache(64 * MIB)
    cache.get_or_compute("ns", 1, lambda: Result(base, view))
    cache.get_or_compute("ns", 2, lambda: {"again": other_view})  # Same memory, another entry.
    assert cache.retained_bytes() == base.nbytes


def test_pandas_and_bytes_are_counted_without_traversal():
    frame = pd.DataFrame({"a": np.arange(1000, dtype=np.int64)})
    owners, other = retained_memory({"frame": frame, "payload": b"x" * 100})
    assert not owners and other == int(frame.memory_usage(deep=True).sum()) + 100


def test_least_recently_used_entries_are_evicted_by_bytes():
    cache = ByteBudgetCache(25 * MIB)
    for key in "abc":
        cache.get_or_compute("ns", key, lambda: _array(10))
    assert cache.stats().entries == 2 and cache.retained_bytes() <= 25 * MIB
    calls = []
    cache.get_or_compute("ns", "b", lambda: calls.append("b"))  # Hit: refreshes "b".
    cache.get_or_compute("ns", "d", lambda: _array(10))         # Evicts "c", the least recently used.
    assert calls == []
    recomputed = []
    cache.get_or_compute("ns", "b", lambda: recomputed.append("b") or _array(10))
    cache.get_or_compute("ns", "c", lambda: recomputed.append("c") or _array(10))
    assert recomputed == ["c"] and cache.stats().evictions >= 2


def test_value_larger_than_the_budget_is_returned_but_not_retained():
    cache = ByteBudgetCache(MIB)
    value = cache.get_or_compute("ns", "big", lambda: _array(2))
    assert value.size and cache.stats().entries == 0 and cache.stats().not_retained == 1


def test_eviction_never_changes_a_deterministic_result():
    def compute():
        return Result(np.linspace(-1, 1, 500_000, dtype=np.float32), np.arange(500_000) % 3 == 0)

    cache = ByteBudgetCache(3 * MIB)
    first = cache.get_or_compute("ns", "k", compute)
    cache.get_or_compute("ns", "other", lambda: _array(2.5))  # Evicts "k".
    again = cache.get_or_compute("ns", "k", compute)
    assert again is not first
    np.testing.assert_array_equal(again.values, first.values)
    np.testing.assert_array_equal(again.mask, first.mask)


def test_shared_values_are_read_only_including_views_and_bases():
    base = np.zeros(10, dtype=np.float32)
    view = base[2:5]
    value = ByteBudgetCache(MIB).get_or_compute("ns", "k", lambda: Result(view, base > 0))
    for array in (value.values, value.mask, base, value.values[1:]):
        assert not array.flags.writeable
        with pytest.raises(ValueError):
            array[0] = 1
    with pytest.raises(ValueError):
        value.values.setflags(write=True)  # Cannot be made writable again through the view.


def test_freeze_leaves_pandas_untouched():
    frame = pd.DataFrame({"a": [1.0, 2.0]})
    freeze_arrays({"frame": frame})
    frame.loc[0, "a"] = 3.0
    assert frame.loc[0, "a"] == 3.0


def test_concurrent_requests_for_one_key_compute_once():
    cache, started, release = ByteBudgetCache(16 * MIB), threading.Event(), threading.Event()
    calls, results = [], []

    def compute():
        calls.append(1)
        started.set()
        release.wait(5)
        return _array(1)

    threads = [threading.Thread(target=lambda: results.append(cache.get_or_compute("ns", "k", compute)))
               for _ in range(4)]
    for thread in threads:
        thread.start()
    assert started.wait(5)
    release.set()
    for thread in threads:
        thread.join(10)
    assert len(calls) == 1 and len(results) == 4 and all(r is results[0] for r in results)


def test_failed_computation_is_not_cached():
    cache = ByteBudgetCache(MIB)
    with pytest.raises(RuntimeError):
        cache.get_or_compute("ns", "k", lambda: (_ for _ in ()).throw(RuntimeError("échec")))
    assert cache.get_or_compute("ns", "k", lambda: 42) == 42 and cache.stats().entries == 1


def test_peek_never_computes_nor_refreshes():
    cache = ByteBudgetCache(25 * MIB)
    assert cache.peek("ns", "a") is None
    first = cache.get_or_compute("ns", "a", lambda: _array(10))
    cache.get_or_compute("ns", "b", lambda: _array(10))
    assert cache.peek("ns", "a") is first            # Does not refresh "a"...
    cache.get_or_compute("ns", "c", lambda: _array(10))
    assert cache.peek("ns", "a") is None             # ...which stays the least recently used.
    assert cache.stats().hits == 0


def test_clear_by_namespace():
    cache = ByteBudgetCache(16 * MIB)
    cache.get_or_compute("a", 1, lambda: _array(1))
    cache.get_or_compute("b", 1, lambda: _array(1))
    cache.clear("a")
    assert cache.stats().entries == 1 and cache.retained_bytes() == _array(1).nbytes
    cache.clear()
    assert cache.stats().entries == 0 and cache.retained_bytes() == 0


@pytest.mark.parametrize("raw, expected", [("", 256 * MIB), ("64", 64 * MIB)])
def test_budget_from_environment(raw, expected):
    assert max_bytes_from_environment({"GEONAF_CACHE_MAX_MIB": raw}) == expected


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1.5"])
def test_invalid_budget_is_refused(raw):
    with pytest.raises(ValueError):
        max_bytes_from_environment({"GEONAF_CACHE_MAX_MIB": raw})
