"""Open the pages of the st.navigation router (app.py) the way a visitor does (docs/navigation.md)."""

from pathlib import Path

from src.navigation import SECTION_KEY, SECTIONS

APP = str(Path(__file__).resolve().parents[1] / "app.py")
TEMPORAL = "views/analyses_temporelles.py"


def open_section(app, section: str, timeout: float | None = None):
    """Temporal analyses section; ``app`` has run once (dashboard). Session values set before are kept."""
    app.session_state[SECTION_KEY] = SECTIONS[section]
    return app.switch_page(TEMPORAL).run(timeout=timeout)


def variation_app(timeout: float = 30, **kwargs):
    """Dashboard first, then the « Variation NDVI » section (the former home pair analysis)."""
    from streamlit.testing.v1 import AppTest

    return open_section(AppTest.from_file(APP, **kwargs).run(timeout=timeout), "variation", timeout)
