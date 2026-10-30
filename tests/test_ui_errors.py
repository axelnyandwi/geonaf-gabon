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
    """Structured ``geonaf.ui`` records (JSON payload) of one context."""
    records = []
    for record in caplog.records:
        if record.name != "geonaf.ui":
            continue
        kind, _, payload = record.getMessage().partition(" ")
        data = json.loads(payload)
        if data["context"] == context:
            records.append({"kind": kind, "level": record.levelname, **data})
    return records


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

def test_only_listed_domain_errors_are_expected():
    from src.data.catalogue import CatalogueError
    from src.data.download_control import TransferAborted
    from src.data.remote_window import ByteBudget, ByteBudgetExceeded
    from src.services.local_preview import LocalPreviewError
    from src.services.territory_catalog import ZoneImportRefused

    for error in (LocalPreviewError("x"), CatalogueError("x"), TransferAborted("x"), ByteBudgetExceeded("x"),
                  ZoneImportRefused("x")):
        assert errors.is_expected(error)
    with pytest.raises(ByteBudgetExceeded) as budget:
        ByteBudget(10).consume(11)  # Resource budget exceeded: functional.
    assert errors.is_expected(budget.value)


def test_generic_value_errors_are_internal_whatever_their_origin():
    from src.geo.pair_selection import PairSelectionConfig
    from src.network_config import NetworkConfigurationError

    with pytest.raises(ValueError) as geonaf:
        PairSelectionConfig(min_interval_days=-1)  # Raised by GEONAF code itself: still not trusted.
    with pytest.raises(json.JSONDecodeError) as decode:
        json.loads("{")
    with pytest.raises(ValueError) as library:
        _raise(ValueError(PRIVATE))
    for error in (geonaf.value, decode.value, library.value, RuntimeError("x"), KeyError("x"),
                  PermissionError(PRIVATE), NetworkConfigurationError("Variables CDSE manquantes."),
                  ZeroDivisionError()):
        assert not errors.is_expected(error)


# --- Server log: one entry per incident, no duplicates ---------------------------------------------

def test_internal_error_is_logged_once_as_structured_masked_record(caplog, monkeypatch):
    monkeypatch.setenv("GEONAF_TEST_TOKEN", SECRET)
    with pytest.raises(RuntimeError) as raised:
        _raise(RuntimeError(f"échec {SECRET} {PRIVATE} https://h.example.org/p?sig={SECRET}"))
    with caplog.at_level(logging.WARNING, logger="geonaf.ui"):
        incident = errors.report_internal_error(raised.value, context="test.unique")
        assert errors.report_internal_error(raised.value, context="test.outer") == incident  # Nested handler.
    (record,) = _logged(caplog, "test.unique")
    assert record["kind"] == "ui_error" and record["level"] == "ERROR" and record["incident"] == incident
    assert record["category"] == "internal" and record["type"] == "builtins.RuntimeError"
    assert record["where"].startswith("<projet>/tests/test_ui_errors.py:") and record["frames"]
    raw = json.dumps(record, ensure_ascii=False)
    assert SECRET not in raw and str(PROJECT_ROOT) not in raw and "Users/someone" not in raw
    assert "https://h.example.org/p?<masqué>" in record["message_masked"]
    assert not _logged(caplog, "test.outer")


def test_repeated_failure_logs_one_line_instead_of_a_new_record(caplog):
    with caplog.at_level(logging.ERROR, logger="geonaf.ui"):
        incidents = []
        for _ in range(3):
            try:
                _raise(RuntimeError("même défaillance"))
            except RuntimeError as error:
                incidents.append(errors.report_internal_error(error, context="test.repeat"))
    first, *repeats = _logged(caplog, "test.repeat")
    assert len(set(incidents)) == 1 and first["kind"] == "ui_error" and "frames" in first
    assert [(r["kind"], r["repeat"]) for r in repeats] == [("ui_error_repeat", 2), ("ui_error_repeat", 3)]
    assert all("frames" not in r and "message_masked" not in r for r in repeats)


def _catalogue_failure_with_technical_cause():
    from src.data.catalogue import CatalogueError

    try:
        raise OSError(13, f"Accès refusé token={SECRET}", PRIVATE)
    except OSError:
        raise CatalogueError("Recherche Copernicus indisponible. Vérifier le réseau et la configuration TLS.") \
            from None


SHOWN = """
import streamlit as st
from src.components.errors import show_error
from tests.test_ui_errors import {factory} as factory

try:
    factory()
except Exception as error:
    show_error(error, context="test.{context}")
"""


def test_functional_error_with_technical_cause_keeps_message_and_logs_the_chain(caplog):
    with caplog.at_level(logging.WARNING, logger="geonaf.ui"):
        app = AppTest.from_string(SHOWN.format(factory="_catalogue_failure_with_technical_cause",
                                               context="cause")).run(timeout=30)
    (message,) = [e.value for e in app.error]
    assert message.startswith("Recherche Copernicus indisponible.") and "Identifiant de diagnostic : GN-" in message
    assert SECRET not in message and "someone" not in message and "Accès refusé" not in message
    (record,) = _logged(caplog, "test.cause")
    assert record["level"] == "WARNING" and record["category"] == "functional_with_cause"
    assert message.endswith(f"Identifiant de diagnostic : {record['incident']}.")
    (cause,) = record["chain"]
    assert cause["type"] == "builtins.PermissionError" and cause["errno"] == 13  # OSError(13) → PermissionError.
    assert cause["where"].startswith("<projet>/tests/test_ui_errors.py:")
    raw = json.dumps(record, ensure_ascii=False)
    assert SECRET not in raw and "Users/someone" not in raw


def _functional_without_cause():
    from src.services.local_preview import LocalPreviewError

    raise LocalPreviewError("Couverture comparable insuffisante : paire non comparable.")


def test_functional_error_without_cause_shows_its_message_only(caplog):
    with caplog.at_level(logging.WARNING, logger="geonaf.ui"):
        app = AppTest.from_string(SHOWN.format(factory="_functional_without_cause",
                                               context="plain")).run(timeout=30)
    assert [e.value for e in app.error] == ["Couverture comparable insuffisante : paire non comparable."]
    assert not _logged(caplog, "test.plain")


def _technical_value_error():
    from src.geo.pair_selection import PairSelectionConfig

    PairSelectionConfig(max_pair_evaluations=0)


def test_technical_value_error_is_generic_with_identifier(caplog):
    with caplog.at_level(logging.WARNING, logger="geonaf.ui"):
        app = AppTest.from_string(SHOWN.format(factory="_technical_value_error", context="value")).run(timeout=30)
    (message,) = [e.value for e in app.error]
    assert message.startswith("Erreur interne") and "max_pair_evaluations" not in message
    (record,) = _logged(caplog, "test.value")
    assert record["category"] == "internal" and record["type"] == "builtins.ValueError"
    assert "max_pair_evaluations" in record["message_masked"]  # Kept for diagnosis, server side only.


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
    assert record["incident"] == incident and record["type"] == "builtins.RuntimeError"
    assert SECRET not in json.dumps(record, ensure_ascii=False)


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
    assert record["incident"] == incident and SECRET not in json.dumps(record, ensure_ascii=False)
