"""Public error display (src/components/errors.py), Streamlit configuration and page behaviour; offline."""

import json
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from src.components import errors
from src.config import PROJECT_ROOT
from tests.test_local_preview import local_data  # noqa: F401  (fixture)

SECRET = "abc123SECRETvalue"
PRIVATE = "C:/Users/someone/OneDrive - Entreprise/geonaf/data/aoi/akanda.geojson"


def _logged(caplog, context):
    return [record.getMessage() for record in caplog.records
            if record.name == "geonaf.ui" and f"context={context}" in record.getMessage()]


def _raise(error):
    raise error


# --- Masking ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, hidden", [
    (f"Fichier {PRIVATE} illisible", "Users/someone"),
    (r"Fichier C:\Users\someone\data\x.tif illisible", "someone"),
    (r"Partage \\serveur\partage\dossier\x.tif", "serveur"),
    ("Fichier /home/someone/geonaf/data/x.tif illisible", "/home/someone"),
    (f"https://user:pw@stac.example.org/v1/search?token={SECRET}", SECRET),
    (f"Échec token={SECRET} ; suite", SECRET),
    (f"Authorization: Bearer {SECRET}", SECRET),
    ("Clé AKIAABCDEFGHIJKLMNOP refusée", "AKIAABCDEFGHIJKLMNOP"),
])
def test_public_text_masks_paths_urls_and_secrets(text, hidden):
    assert hidden not in errors.public_text(text)


def test_public_text_masks_secret_environment_values(monkeypatch):
    monkeypatch.setenv("CDSE_S3_SECRET_KEY", SECRET)
    assert SECRET not in errors.public_text(f"Réponse : {SECRET}")


@pytest.mark.parametrize("text", [
    "Polarisations VV/VH disponibles.",
    "Résolution 10 m/pixel ; couverture 42,5 %.",
    "Aucune acquisition le 15/01/2025 UTC ; aucune autre date ne la remplace.",
    "Politique SCL « observation » refusée.",
])
def test_public_text_keeps_business_messages(text):
    assert errors.public_text(text) == text


def test_log_text_shortens_roots_and_masks_urls_and_secrets():
    text = (f'File "{PROJECT_ROOT / "src" / "x.py"}", line 3 ; '
            f"GET https://stac.example.org/v1/search?token={SECRET} ; password={SECRET}")
    logged = errors.log_text(text)
    assert str(PROJECT_ROOT) not in logged and "<projet>" in logged
    assert "https://stac.example.org/v1/search?<masqué>" in logged and SECRET not in logged


# --- Classification --------------------------------------------------------------------------------

def test_domain_errors_and_geonaf_value_errors_are_expected():
    from src.data.catalogue import CatalogueError
    from src.data.download_control import TransferAborted
    from src.data.remote_window import ByteBudget, ByteBudgetExceeded
    from src.geo.pair_selection import PairSelectionConfig
    from src.services.local_preview import LocalPreviewError

    for error in (LocalPreviewError("x"), CatalogueError("x"), TransferAborted("x"), ByteBudgetExceeded("x")):
        assert errors.is_expected(error)
    with pytest.raises(ByteBudgetExceeded) as budget:
        ByteBudget(10).consume(11)  # Resource budget exceeded: functional.
    assert errors.is_expected(budget.value)
    with pytest.raises(ValueError) as invalid:
        PairSelectionConfig(min_interval_days=-1)  # Invalid user parameter, raised by GEONAF code.
    assert errors.is_expected(invalid.value)


def test_library_programming_network_and_auth_errors_are_internal():
    from src.network_config import NetworkConfigurationError

    with pytest.raises(json.JSONDecodeError) as decode:
        json.loads("{")
    with pytest.raises(ValueError) as library:
        _raise(ValueError(PRIVATE))  # Raised outside src/ (here: tests).
    for error in (decode.value, library.value, RuntimeError("x"), KeyError("x"), PermissionError(PRIVATE),
                  NetworkConfigurationError("Variables CDSE manquantes."), ZeroDivisionError()):
        assert not errors.is_expected(error)


# --- Server log: one entry per incident, no duplicates ---------------------------------------------

def test_internal_error_is_logged_once_with_its_identifier_and_masked(caplog, monkeypatch):
    monkeypatch.setenv("GEONAF_TEST_TOKEN", SECRET)
    with pytest.raises(RuntimeError) as raised:
        _raise(RuntimeError(f"échec {SECRET} https://h.example.org/p?sig={SECRET}"))
    with caplog.at_level(logging.ERROR, logger="geonaf.ui"):
        incident = errors.report_internal_error(raised.value, context="test.unique")
        assert errors.report_internal_error(raised.value, context="test.outer") == incident  # Nested handler.
    (record,) = _logged(caplog, "test.unique")
    assert incident.startswith("GN-") and f"incident={incident}" in record and "Traceback" in record
    assert SECRET not in record and str(PROJECT_ROOT) not in record
    assert not _logged(caplog, "test.outer")


def test_repeated_failure_logs_one_line_instead_of_a_new_traceback(caplog):
    with caplog.at_level(logging.ERROR, logger="geonaf.ui"):
        incidents = []
        for _ in range(3):
            try:
                _raise(RuntimeError("même défaillance"))
            except RuntimeError as error:
                incidents.append(errors.report_internal_error(error, context="test.repeat"))
    first, *repeats = _logged(caplog, "test.repeat")
    assert len(set(incidents)) == 1 and "Traceback" in first
    assert [line.rsplit("count=", 1)[1] for line in repeats] == ["2", "3"]
    assert all("Traceback" not in line for line in repeats)


# --- Streamlit configuration and pages --------------------------------------------------------------

def test_streamlit_configuration_hides_uncaught_exception_details():
    from streamlit import config

    assert config.get_option("client.showErrorDetails") == "none"
    assert config.get_option("client.showErrorLinks") is False
    app = AppTest.from_string(f'raise RuntimeError("{PRIVATE} token={SECRET}")\n').run(timeout=30)
    (shown,) = app.exception
    assert shown.stack_trace == [] and PRIVATE not in shown.message and SECRET not in shown.message


GUARDED_PAGE = '''
import streamlit as st
from src.components.errors import page_guard

with page_guard("test.page"):
    st.write("début")
    if st.session_state.get("stop"):
        st.stop()
    raise RuntimeError("{private} token={secret}")
'''


def test_page_guard_shows_generic_message_with_identifier_and_logs_it(caplog):
    script = GUARDED_PAGE.format(private=PRIVATE, secret=SECRET)
    with caplog.at_level(logging.ERROR, logger="geonaf.ui"):
        app = AppTest.from_string(script).run(timeout=30)
    assert not app.exception
    (message,) = [e.value for e in app.error]
    assert message.startswith("Erreur interne") and "Identifiant de diagnostic : GN-" in message
    assert PRIVATE not in message and SECRET not in message and "Traceback" not in message
    incident = message.split("Identifiant de diagnostic : ")[1].rstrip(".")
    (record,) = _logged(caplog, "test.page")
    assert f"incident={incident}" in record and "RuntimeError" in record and SECRET not in record


def test_page_guard_lets_streamlit_stop_through():
    app = AppTest.from_string(GUARDED_PAGE.format(private="x", secret="y"))
    app.session_state["stop"] = True
    app.run(timeout=30)
    assert not app.exception and not app.error and app.markdown[0].value == "début"


# --- Source reading and resource budget on the multisource page ------------------------------------

@pytest.fixture
def multisource(local_data, monkeypatch):
    """Multisource page on verified local data; ``retrieve_worldcover`` raises the given error."""
    import src.config
    import src.components.multisource as ui
    from src.services import external_retrieval

    def open_page(error):
        monkeypatch.setattr(src.config, "DATA_DIR", local_data)
        for name in ("cached_observation", "cached_osm", "cached_radar", "cached_multisource",
                     "cached_map_payload"):
            getattr(ui, name).clear()
        monkeypatch.setattr(external_retrieval, "retrieve_worldcover", Mock(side_effect=error))
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        app.switch_page("pages/3_Multisource.py").run(timeout=60)
        assert not app.exception and not app.error
        app.button(key="ms_fetch_wc").click().run(timeout=60)
        assert not app.exception
        return [e.value for e in app.error]
    return open_page


def test_source_budget_exceeded_shows_its_functional_message(multisource, caplog):
    from src.data.remote_window import ByteBudgetExceeded

    with caplog.at_level(logging.ERROR, logger="geonaf.ui"):
        (message,) = multisource(ByteBudgetExceeded("Budget réseau dépassé : lecture interrompue."))
    assert message == ("Récupération impossible. Budget réseau dépassé : lecture interrompue. "
                       "Aucune donnée partielle n'a été enregistrée.")
    assert not _logged(caplog, "multisource.recuperation")


@pytest.mark.parametrize("error", [
    OSError(f"[Errno 2] No such file or directory: '{PRIVATE}'"),
    RuntimeError(f"HTTP 403 https://esa-worldcover.s3.amazonaws.com/x.tif?X-Amz-Signature={SECRET}"),
])
def test_source_read_or_network_failure_is_generic_with_identifier(multisource, caplog, error):
    with caplog.at_level(logging.ERROR, logger="geonaf.ui"):
        (message,) = multisource(error)
    assert message.startswith("Récupération impossible. Erreur interne") and "GN-" in message
    assert PRIVATE not in message and SECRET not in message and "amazonaws" not in message
    incident = message.split("Identifiant de diagnostic : ")[1].split(".")[0]
    (record,) = _logged(caplog, "multisource.recuperation")
    assert f"incident={incident}" in record and SECRET not in record
