"""D5: explicit HTTPS consultation of the Supabase geonaf-test acquisitions in the interface; offline.

Only the TLS session is replaced (``src.data.network.secure_session``): configuration, the real client
(status mapping, strict JSON contract), the service and the Streamlit panel all run as in production.
Every token and URL below is a dummy built for the tests.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.data import edge_acquisitions as client
from src.services import supabase_acquisitions as service
from tests.test_local_preview import local_data  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "Tk_" + "s" * 40
URL = "https://abcdefghijklmnopqrst.supabase.co/functions/v1/geonaf-acquisitions"
CONFIG = {"GEONAF_EDGE_ACQUISITIONS_URL": URL, "GEONAF_EDGE_TOKEN": TOKEN}


def record(day, product, integrity="strict"):
    return {"manifest_sha256": f"{day:02d}" * 32, "collection": "sentinel-2-l2a", "product_id": product,
            "acquired_at": f"2025-01-{day:02d}T09:32:00.000Z", "ingested_at": "2026-10-20T08:00:00.000Z",
            "grid_crs": "EPSG:32632", "grid_resolution_m": 10, "integrity_mode": integrity}


FOUR = {"environment": "test", "count": 4, "acquisitions": [
    record(25, "S2B_MSIL2A_20250125"), record(20, "S2A_MSIL2A_20250120", "exploratory"),
    record(15, "S2B_MSIL2A_20250115"), record(10, "S2A_MSIL2A_20250110", None)]}


class Response:
    def __init__(self, status=200, body=b"", content_type="application/json; charset=utf-8"):
        self.status_code, self.body, self.headers = status, body, {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        yield from (self.body[i:i + size] for i in range(0, len(self.body), size))


class Session:
    """Replaces the verified TLS session; records every request."""
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def json_response(payload, status=200):
    return Response(status, json.dumps(payload).encode())


@pytest.fixture
def network(monkeypatch):
    """Every secure session created by the client; none must exist until the button is clicked."""
    import src.data.network as net

    state = {"session": Session(json_response(FOUR)), "created": 0}

    def factory(settings):
        state["created"] += 1
        return state["session"]

    monkeypatch.setattr(net, "secure_session", factory)
    for key in (*service.KEYS, "GEONAF_DB_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    return state


# --- Service (no Streamlit) -----------------------------------------------------------------------------------

def test_resolve_config_sources_and_precedence():
    assert service.resolve_config({}, {}) is None
    assert service.resolve_config(None, CONFIG).token == TOKEN
    other = {"GEONAF_EDGE_ACQUISITIONS_URL": URL.replace("abcdefghijklmnopqrst", "zzzzzzzzzzzzzzzzzzzz"),
             "GEONAF_EDGE_TOKEN": "O" * 40}
    assert service.resolve_config(CONFIG, other).url == URL          # Streamlit secrets first.
    assert repr(service.resolve_config(CONFIG, {})).count(TOKEN) == 0


@pytest.mark.parametrize("secrets,environ", [
    ({"GEONAF_EDGE_ACQUISITIONS_URL": URL}, CONFIG),                # Half in secrets: never mixed with env.
    ({}, {"GEONAF_EDGE_TOKEN": TOKEN}),
    ({}, {**CONFIG, "GEONAF_EDGE_ACQUISITIONS_URL": "http://abc.supabase.co/functions/v1/geonaf-acquisitions"}),
    ({}, {**CONFIG, "GEONAF_EDGE_TOKEN": "short"}),
])
def test_resolve_config_refuses_half_or_invalid_configuration(secrets, environ):
    with pytest.raises(client.EdgeAcquisitionsError) as error:
        service.resolve_config(secrets, environ)
    assert error.value.category == "configuration"
    assert TOKEN not in str(error.value) and "supabase.co" not in str(error.value)


def test_four_acquisitions_rows_keep_dates_apart_and_age_from_acquisition():
    session = Session(json_response(FOUR))
    consulted = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    result = service.consult(client.EdgeConfig(URL, TOKEN),
                             fetch=lambda config: client.fetch_acquisitions(config, session=session),
                             now=lambda: consulted)
    rows = service.display_rows(result)
    assert len(rows) == 4 and not result.truncated
    assert rows[0]["Acquisition (UTC)"] == "2025-01-25 09:32" and rows[0]["Ingestion GEONAF (UTC)"] == "2026-10-20 08:00"
    assert rows[0]["Ancienneté de l'observation (jours)"] == (consulted - datetime(2025, 1, 25, 9, 32,
                                                                                    tzinfo=timezone.utc)).days
    assert [r["Intégrité du manifeste"] for r in rows] == ["stricte", "exploratoire", "stricte", "non renseignée"]
    assert rows[0]["Manifeste (SHA-256)"] == "25" * 6 + "…" and rows[0]["Grille"] == "EPSG:32632, 10 m"
    assert TOKEN not in repr(result) + repr(rows)


def test_truncation_flag_at_the_function_limit():
    records = [record(1 + i % 28, f"P{i}") for i in range(client.MAX_RECORDS)]
    result = service.consult(client.EdgeConfig(URL, TOKEN), fetch=lambda config: client.parse_payload(
        {"environment": "test", "count": len(records), "acquisitions": records}))
    assert result.truncated


# --- Streamlit page (AppTest) ----------------------------------------------------------------------------------

def _page(local_data, monkeypatch, *, secrets=None):
    import src.config
    import src.services.local_preview as preview

    monkeypatch.setattr(src.config, "DATA_DIR", local_data)
    monkeypatch.setattr(preview, "list_local_observations", lambda data_dir: ())
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    for key, value in (secrets or {}).items():
        app.secrets[key] = value
    app.run()
    from src.navigation import SECTIONS

    app.session_state["section"] = SECTIONS["donnees"]
    app.switch_page("views/analyses_temporelles.py").run()
    assert not app.exception
    return app


def _visible_text(app) -> str:
    parts = []
    for kind in ("title", "header", "subheader", "caption", "markdown", "info", "warning", "error", "success"):
        parts += [str(element.value) for element in getattr(app, kind)]
    parts += [element.value.to_csv() for element in app.dataframe]
    parts += [button.label for button in app.button]
    return "\n".join(parts)


def _no_secret(app, caplog=None):
    stored = app.session_state["donnees_supabase_result"] if "donnees_supabase_result" in app.session_state else None
    text = _visible_text(app) + repr(stored)                        # The session keeps records, never the token.
    assert TOKEN not in text and "abcdefghijklmnopqrst" not in text and "Bearer" not in text
    if caplog is not None:
        assert TOKEN not in caplog.text


def test_page_without_configuration_is_unchanged_and_offline(local_data, monkeypatch, network):
    app = _page(local_data, monkeypatch)
    assert not app.error
    assert any(h.value == "Catalogue des acquisitions Sentinel-2" for h in app.header)   # File mode, unchanged.
    assert any("non configurée sur ce serveur" in i.value for i in app.info)
    assert not any(b.key == "donnees_supabase_consult" for b in app.button)
    assert len(app.dataframe) == 0 and network["created"] == 0


def test_configured_page_sends_no_request_until_asked(local_data, monkeypatch, network):
    for key, value in CONFIG.items():
        monkeypatch.setenv(key, value)
    app = _page(local_data, monkeypatch)
    assert not app.error and network["created"] == 0
    assert app.button(key="donnees_supabase_consult").label == "Consulter Supabase geonaf-test"
    assert any("Aucune requête n'est envoyée" in c.value for c in app.caption)
    app.selectbox(key="catalogue_aoi").set_value(app.selectbox(key="catalogue_aoi").value).run()
    assert network["created"] == 0                                       # Other reruns stay offline.
    _no_secret(app)


def test_click_shows_the_four_acquisitions_with_source_and_limits(local_data, monkeypatch, network, caplog):
    for key, value in CONFIG.items():
        monkeypatch.setenv(key, value)
    app = _page(local_data, monkeypatch)
    app.button(key="donnees_supabase_consult").click().run()
    assert not app.exception and not app.error
    assert network["created"] == 1 and len(network["session"].calls) == 1
    url, kwargs = network["session"].calls[0]
    assert url == URL and kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert kwargs["allow_redirects"] is False and kwargs["timeout"] == (10.0, 20.0)
    table = app.dataframe[0].value
    assert len(table) == 4 and list(table["Produit"])[0] == "S2B_MSIL2A_20250125"
    assert {"Acquisition (UTC)", "Ingestion GEONAF (UTC)", "Ancienneté de l'observation (jours)"} <= set(table)
    text = _visible_text(app)
    assert "Source : Supabase geonaf-test (environnement de test" in text and "4 acquisition(s)" in text
    assert "ni un résultat d'indicateur" in text and "aucun changement territorial ne peut en être déduit" in text
    assert "ne sont pas rattachées à une zone d'étude ni à une collectivité" in text
    app.run()                                                            # Rerun: kept, no new request.
    assert len(app.dataframe) == 1 and len(network["session"].calls) == 1
    _no_secret(app, caplog)


def test_secrets_toml_configuration_is_used(local_data, monkeypatch, network):
    app = _page(local_data, monkeypatch, secrets=CONFIG)
    app.button(key="donnees_supabase_consult").click().run()
    assert not app.error and len(app.dataframe[0].value) == 4
    _no_secret(app)


@pytest.mark.parametrize("status,expected", [
    (401, "accès refusé, jeton du serveur GEONAF non reconnu"),
    (429, "momentanément saturé. Réessayer dans quelques secondes"),
    (503, "indisponible (base ou configuration du service)"),
    (500, "réponse inattendue"),
])
def test_http_errors_show_sober_messages(local_data, monkeypatch, network, caplog, status, expected):
    for key, value in CONFIG.items():
        monkeypatch.setenv(key, value)
    network["session"] = Session(Response(status, b'{"error":"db.secret-host.internal 28P01 geonaf_edge_reader"}'))
    app = _page(local_data, monkeypatch)
    app.button(key="donnees_supabase_consult").click().run()
    assert not app.exception and len(app.error) == 1 and expected in app.error[0].value
    assert "secret-host" not in app.error[0].value and "28P01" not in app.error[0].value
    assert str(status) not in app.error[0].value and len(app.dataframe) == 0
    _no_secret(app, caplog)


def test_timeout_and_transport_errors_never_leak_details(local_data, monkeypatch, network, caplog):
    import requests

    for key, value in CONFIG.items():
        monkeypatch.setenv(key, value)
    leak = f"HTTPSConnectionPool(host='abcdefghijklmnopqrst.supabase.co') Authorization: Bearer {TOKEN} token={TOKEN}"
    caplog.set_level(logging.WARNING, logger="geonaf.ui")
    for error, expected in ((requests.exceptions.ReadTimeout(leak), "délai dépassé"),
                            (requests.exceptions.SSLError(leak), "certificat TLS refusé"),
                            (requests.exceptions.ConnectionError(leak), "connexion impossible")):
        network["session"] = Session(error=error)
        app = _page(local_data, monkeypatch)
        app.button(key="donnees_supabase_consult").click().run()
        assert not app.exception and len(app.error) == 1 and expected in app.error[0].value
        assert "Identifiant de diagnostic : GN-" in app.error[0].value     # The cause was logged...
        _no_secret(app, caplog)
    assert "supabase_acquisitions.consultation" in caplog.text            # ...masked: no token in it.
    assert "Bearer <valeur masquée>" in caplog.text and "token=<valeur masquée>" in caplog.text
    assert TOKEN not in caplog.text    # The server log keeps the host for diagnosis (errors.py policy), not secrets.


@pytest.mark.parametrize("response", [
    Response(200, b"<html>login</html>", "text/html"),
    Response(200, b"{not json"),
    json_response({"environment": "dev", "count": 0, "acquisitions": []}),
    json_response({**FOUR, "count": 5}),
    json_response({**FOUR, "acquisitions": [{**record(25, "P"), "manifest_document": {}}]}),
    json_response({"environment": "test", "count": 101, "acquisitions": [record(1, "P")] * 101}),
])
def test_invalid_or_unexpected_json_is_refused(local_data, monkeypatch, network, response):
    for key, value in CONFIG.items():
        monkeypatch.setenv(key, value)
    network["session"] = Session(response)
    app = _page(local_data, monkeypatch)
    app.button(key="donnees_supabase_consult").click().run()
    assert not app.exception and len(app.error) == 1 and "ignorée" in app.error[0].value
    assert len(app.dataframe) == 0
    _no_secret(app)


def test_a_failed_new_request_hides_the_previous_result(local_data, monkeypatch, network):
    for key, value in CONFIG.items():
        monkeypatch.setenv(key, value)
    app = _page(local_data, monkeypatch)
    app.button(key="donnees_supabase_consult").click().run()
    assert len(app.dataframe) == 1
    network["session"] = Session(Response(503, b"{}"))
    app.button(key="donnees_supabase_consult").click().run()
    assert len(app.dataframe) == 0 and len(app.error) == 1


def test_half_configuration_warns_without_button(local_data, monkeypatch, network):
    monkeypatch.setenv("GEONAF_EDGE_ACQUISITIONS_URL", URL)
    app = _page(local_data, monkeypatch)
    assert any("mal configurée" in w.value for w in app.warning)
    assert not any(b.key == "donnees_supabase_consult" for b in app.button) and network["created"] == 0
    _no_secret(app)


def test_other_sections_never_load_the_panel(local_data, monkeypatch, network):
    for key, value in CONFIG.items():
        monkeypatch.setenv(key, value)
    import src.config
    import src.services.local_preview as preview
    from tests.streamlit_pages import open_section

    monkeypatch.setattr(src.config, "DATA_DIR", local_data)
    monkeypatch.setattr(preview, "list_local_observations", lambda data_dir: ())
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not any(b.key == "donnees_supabase_consult" for b in app.button)     # Dashboard.
    for section in ("comparer", "variation", "serie"):
        app = open_section(app, section)
        assert not any(b.key == "donnees_supabase_consult" for b in app.button), section
    assert network["created"] == 0
