"""Shared limit of simultaneous heavy computations; offline, thread-based."""

import threading
import time

import pytest

from src.services.compute_slots import ComputeBusyError, ComputeSlots, SlotSettings


def test_at_most_n_heavy_computations_run_together():
    slots, lock = ComputeSlots(SlotSettings(slots=2, wait_seconds=10)), threading.Lock()
    running, peak = [0], [0]

    def work():
        with slots.heavy():
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.15)
            with lock:
                running[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert peak[0] == 2 and slots.active == 0 and slots.waiting == 0


def test_saturation_raises_a_safe_error_after_a_bounded_wait():
    slots, inside, release = ComputeSlots(SlotSettings(slots=1, wait_seconds=0.2)), threading.Event(), threading.Event()

    def holder():
        with slots.heavy():
            inside.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert inside.wait(5)
    start = time.perf_counter()
    with pytest.raises(ComputeBusyError) as busy:
        with slots.heavy():
            pass
    assert time.perf_counter() - start < 2 and slots.refused == 1
    assert "Serveur occupé" in str(busy.value) and "Traceback" not in str(busy.value)
    release.set()
    thread.join(5)
    with slots.heavy():  # The slot was released.
        assert slots.active == 1


def test_nested_heavy_computations_reuse_the_thread_slot():
    slots = ComputeSlots(SlotSettings(slots=1, wait_seconds=0.5))
    with slots.heavy():
        with slots.heavy():  # Would deadlock (then time out) without re-entrance.
            assert slots.active == 1
    assert slots.active == 0


def test_slot_is_released_on_error():
    slots = ComputeSlots(SlotSettings(slots=1, wait_seconds=0.2))
    with pytest.raises(RuntimeError):
        with slots.heavy():
            raise RuntimeError("échec")
    with slots.heavy():
        pass
    assert slots.active == 0


def test_settings_from_environment():
    assert SlotSettings.from_environment({}) == SlotSettings()
    assert SlotSettings.from_environment({"GEONAF_MAX_HEAVY_COMPUTATIONS": "3",
                                         "GEONAF_HEAVY_WAIT_SECONDS": "12.5"}) == SlotSettings(3, 12.5)
    for name, raw in (("GEONAF_MAX_HEAVY_COMPUTATIONS", "0"), ("GEONAF_MAX_HEAVY_COMPUTATIONS", "x"),
                      ("GEONAF_HEAVY_WAIT_SECONDS", "-1")):
        with pytest.raises(ValueError):
            SlotSettings.from_environment({name: raw})
