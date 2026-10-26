"""Data-source component: public errors and provenance display (no network)."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from src.components import data_source
from src.data.demo_pack import DataSource, DemoPackError


def _page():
    import streamlit as st
    from src.components.data_source import active_data_dir

    st.text(f"dir={active_data_dir().name}")


def test_download_failure_is_reported_without_details(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise DemoPackError("network", "Téléchargement du paquet de démonstration impossible.")

    data_source._resolve.clear()
    monkeypatch.setattr(data_source.config, "DATA_DIR", tmp_path / "empty")
    monkeypatch.setattr(data_source, "resolve_data_dir", fail)
    app = AppTest.from_function(_page).run()
    warning = app.warning[0].value
    assert "impossible" in warning and "http" not in warning and str(tmp_path) not in warning
    assert app.text[0].value == "dir=empty"


def test_installed_pack_is_used_and_its_provenance_shown(monkeypatch, tmp_path):
    pack = {"pack_id": "geonaf-demo", "pack_version": "2", "aoi": {"statement": "Carré d'acquisition."},
            "sentinel2": {"acquisitions": [{"acquired_at_utc": "2025-01-20T09:33:21Z",
                                            "ingested_at_utc": "2026-10-22T11:50:45Z",
                                            "quality": {"valid_coverage_percent": 2.72,
                                                        "trend_use": "excluded_low_coverage"}}]},
            "limits": ["Limite déclarée."]}
    data_source._resolve.clear()
    monkeypatch.setattr(data_source.config, "DATA_DIR", tmp_path / "empty")
    monkeypatch.setattr(data_source, "resolve_data_dir",
                        lambda configured, **kwargs: DataSource(Path(tmp_path / "pack"), "demo_pack", pack))
    app = AppTest.from_function(_page).run()
    assert app.text[0].value == "dir=pack"
    captions = [caption.value for caption in app.caption]
    assert any("geonaf-demo v2" in caption for caption in captions)
    assert any("exclue des tendances" in caption for caption in captions)
    data_source._resolve.clear()
