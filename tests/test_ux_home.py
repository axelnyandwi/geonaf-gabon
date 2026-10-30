"""Home page wording, structure and accessibility (C5); offline, synthetic data."""

from pathlib import Path
import re

import pytest
from streamlit.testing.v1 import AppTest

from src.components.temporal_guide import TRANSITIONS_TITLE, VARIATION_TITLE
from src.geo.scientific_profile import REFERENCE_PROFILES
from tests.test_local_preview import local_data  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(local_data, monkeypatch):
    import src.config

    monkeypatch.setattr(src.config, "DATA_DIR", local_data)
    monkeypatch.setattr("src.components.resource_panel.available_memory_bytes", lambda: None)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not app.exception and not app.error
    return app


def _texts(app):
    """Visible texts in document order, expander contents excluded."""
    out = []

    def walk(node, folded=False):
        kind = type(node).__name__
        folded = folded or kind == "Expander"
        value = None
        for attribute in ("value", "body"):
            try:
                value = getattr(node, attribute)
                break
            except Exception:
                continue
        if isinstance(value, str) and kind not in ("Expander",):
            out.append((kind, value, folded))
        children = getattr(node, "children", None)
        for child in (children.values() if isinstance(children, dict) else []):
            walk(child, folded)
    walk(app.main)
    return out


def _visible(app):
    return [value for _, value, folded in _texts(app) if not folded]


def test_reading_guide_separates_the_three_temporal_readings_before_the_analyses(home):
    texts = _visible(home)
    guide = next(i for i, t in enumerate(texts) if t.startswith("**Trois lectures différentes"))
    transitions = next(i for i, t in enumerate(texts) if t == f"### {TRANSITIONS_TITLE}")
    assert guide < transitions
    body = texts[guide + 1]
    assert "Transitions entre classes NDVI" in body and "Variation continue du NDVI" in body
    assert "Couverture du sol WorldCover" in body and "aucune évolution" in body
    assert "seuil NDVI 0,4" in body                                            # From the transition profile.
    variation = f"{REFERENCE_PROFILES['akanda'].change_threshold:.2f}".replace(".", ",")
    assert f"±{variation}" in body                                             # From the reference profile.
    # Never an automatic attribution of cause: only negated mentions.
    for text in texts:
        for match in re.finditer(r"(déforestation|reboisement|reforestation)", text):
            window = text[max(0, match.start() - 40):match.start()]
            assert re.search(r"\b(ni|ne|pas|n'est|ne démontre|ne prouve)\b", window), text


def test_variation_section_states_dates_common_surface_coverage_and_threshold(home):
    headers = [h.value for h in home.header]
    assert headers == ["Chiffres clés du territoire", "Comparer deux dates", VARIATION_TITLE]
    summary = next(t for t in _visible(home) if t.startswith("**Dates comparées**"))
    assert "15/01/2025" in summary and "25/01/2025" in summary
    assert "**Surface observée aux deux dates**" in summary and "km²" in summary and "% de la zone d'étude" in summary
    assert "**Seuil de variation ΔNDVI** : ±0,10" in summary
    labels = [m.label for m in home.metric]
    assert labels == ["Surface observée aux deux dates", "Part de la zone d'étude observée", "Part non comparable",
                      "Variation moyenne du NDVI"]
    assert home.metric[0].value.endswith("km²")


def test_no_raw_technical_state_or_duplicate_note_in_the_visible_page(home):
    visible = _visible(home)
    joined = "\n".join(visible)
    for raw in ("not_assessed", "(unknown)", "usable_land", "Garde-fous de ressources", "AOI "):
        assert raw not in joined, raw
    texts = _texts(home)
    start = next(i for i, (kind, t, _) in enumerate(texts) if kind == "Header" and t == VARIATION_TITLE)
    captions = [t for kind, t, folded in texts[start:] if kind == "Caption" and not folded]
    assert captions and len(captions) == len(set(captions))      # Restrictions and seasonal notes shown once.
    details = [t for _, t, folded in _texts(home) if folded]
    assert any("Saisonnalité : non évaluée" in t for t in details)            # Translated, in the details.
    assert any("empreinte" in t for t in details)                             # Fingerprints moved there.


def test_authorised_resource_check_is_one_short_line_with_details_in_its_tooltip(home):
    lines = [c for c in home.caption if c.value.startswith("Ressources vérifiées avant calcul")]
    assert lines and all(c.value.endswith("calcul autorisé.") for c in lines)
    assert all("Cellules raster" in (c.help or "") for c in lines)


def test_absence_of_evolution_is_visible_without_unfolding(home):
    assert any(t.startswith("**Évolutions temporelles** — aucune évolution") for t in _visible(home))


def _contrast(foreground, background):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    def luminance(rgb):
        r, g, b = (channel(v) for v in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("name, tint", [("orange", (255, 164, 33)), ("gray", (49, 51, 63)), ("green", (33, 195, 84))])
def test_light_theme_badge_text_contrast_is_at_least_4_5(name, tint):
    config = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    section = config.split("[theme.light]", 1)[1]
    colour = re.search(rf'^{name}TextColor = "#([0-9a-fA-F]{{6}})"', section, re.M).group(1)
    foreground = tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))
    background = tuple(round(c * 0.1 + 255 * 0.9) for c in tint)       # Streamlit's 10 % tinted badge background.
    assert _contrast(foreground, background) >= 4.5


def test_pack_summary_uses_french_dates_and_decimals():
    from src.data.demo_pack import pack_summary

    lines = pack_summary({"pack_id": "p", "pack_version": "3", "aoi": {"statement": "Carré."},
                          "sentinel2": {"acquisitions": [{"acquired_at_utc": "2025-01-15T09:32:39Z",
                                                          "ingested_at_utc": "2026-10-16T17:10:00Z",
                                                          "quality": {"valid_coverage_percent": 66.7312}}]}})
    assert "acquis le 15/01/2025 (ingéré le 16/10/2026)" in lines[1] and "66,73 %" in lines[1]
