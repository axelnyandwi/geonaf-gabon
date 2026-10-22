"""Shared test configuration.

Performance: every Streamlit ``AppTest`` run re-registers v2 components and
``scan_component_manifests`` re-reads the metadata of every installed
distribution (≈0.3 s per scan on this environment, profiled 22/10/2026).
Installed packages cannot change during a pytest session, so the scan result
is memoized for the session. Application behaviour is unchanged.
"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def _memoize_streamlit_component_manifest_scan():
    try:
        from streamlit.components.v2 import manifest_scanner
    except ImportError:
        yield
        return
    original = manifest_scanner.scan_component_manifests
    cache = {}

    def memoized(max_workers=None):
        if "result" not in cache:
            cache["result"] = original(max_workers)
        return list(cache["result"])

    manifest_scanner.scan_component_manifests = memoized
    try:
        yield
    finally:
        manifest_scanner.scan_component_manifests = original
