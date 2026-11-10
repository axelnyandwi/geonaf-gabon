"""Streamlit-side caches of the home page: public PNG bytes, and the pair analysis shared read-only."""

import streamlit as st

from src.components.ndvi_maps import render_ndvi_png
from src.services import local_preview
from src.services.bounded_cache import shared_cache
from src.services.compute_slots import heavy_computation

PAIR_NAMESPACE = "accueil.paire"


@st.cache_data(max_entries=4, show_spinner=False)
def cached_map_png(input_key, _result):
    # _result is intentionally not hashed: input_key covers acquisitions,
    # file revisions, AOI, SCL and threshold. Bump png-v1 when rendering changes.
    return render_ndvi_png(_result)


def shared_pair_analysis(key, earlier, later, data_dir, scientific_profile):
    """Pair analysis shared by all sessions instead of one copy per session (C4b, P3).

    ``key`` is ``local_preview_key``: profile fingerprint, SCL policy, threshold, file revisions
    and manifest digests of both acquisitions, AOI path and content digest. The result's arrays
    are read-only (views included); the cache is bounded in bytes and an evicted result is
    recomputed identically from the same key. Sessions keep only the key.
    """
    def compute():
        with heavy_computation():
            return local_preview.analyze_local_akanda(earlier, later, data_dir,
                                                      scientific_profile=scientific_profile)
    return shared_cache().get_or_compute(PAIR_NAMESPACE, key, compute)


def peek_pair_analysis(key):
    """The shared result of ``key`` if it is currently cached, else ``None`` (never computes)."""
    return shared_cache().peek(PAIR_NAMESPACE, key)
