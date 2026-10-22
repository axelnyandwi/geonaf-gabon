"""Non-regression: the Multisource page and its service contract import and run in a fresh process."""

import inspect
from pathlib import Path
import subprocess
import sys

from tests.test_local_preview import local_data  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]


def test_page_dependencies_import_in_a_clean_interpreter():
    # A fresh interpreter avoids stale modules cached by an earlier import (e.g. a long-running server).
    code = ("import runpy, sys; sys.path.insert(0, '.');"
            "from src.services.local_preview import analyze_local_observation, local_observation_key;"
            "import src.components.multisource as ui;"
            "assert ui.analyze_local_observation is analyze_local_observation")
    completed = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr[-2000:]


def test_service_signature_keeps_profile_and_verification_contract():
    from src.services.local_preview import analyze_local_observation, local_observation_key

    parameters = inspect.signature(analyze_local_observation).parameters
    assert list(parameters)[:2] == ["observation", "data_dir"]
    assert parameters["scientific_profile"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "scientific_profile" in inspect.signature(local_observation_key).parameters


def test_page_file_runs_in_streamlit(local_data, monkeypatch):
    """Real multipage registry: app.py, then the Multisource page file itself."""
    from streamlit.testing.v1 import AppTest
    import src.config

    monkeypatch.setattr(src.config, "DATA_DIR", local_data)
    app = AppTest.from_file(str(ROOT / "app.py"))
    app.run(timeout=30)
    app.switch_page("pages/3_Multisource.py").run(timeout=60)
    assert not app.exception, app.exception
    assert app.title[0].value == "Démonstrateur multisource"
    assert len(app.metric) == 5  # NDVI computed through analyze_local_observation.
