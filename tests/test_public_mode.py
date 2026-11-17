"""Public consultation (GEONAF_PUBLIC, Discovery without authentication): Discovery V4 phase A.

* the mode implies read only and fails closed;
* visitor-triggered outbound or credentialed access (Copernicus search, Supabase) is refused in the services,
  so a bypass of the pages cannot run it; the network doubles fail the test if touched;
* a public process holding credentials (GEONAF's or another service's: ``*_TOKEN``, ``*_PASSWORD``,
  ``*_API_KEY``...), a database URL, an unknown secret or GEONAF setting, a disabled read-only flag or resource
  limits looser than the defaults serves no page, and the refusal names categories only, never a variable name
  or a value; known system variables (``TOKENIZERS_PARALLELISM``, ``TOKEN_URL``, ``PWD``...) stay allowed;
* pages run on a copy of the v3 demo pack: same pages as the private mode, without the refused commands,
  with the zone status, dates, attribution and limits, and without writing the pack.
"""

from dataclasses import fields
from datetime import date, datetime, timezone
import os
from unittest.mock import Mock

import pytest

from src import demo_mode as dm
from tests.test_demo_read_only import _infos, _run, _tree, pack_copy  # noqa: F401  (fixture)


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setenv(dm.PUBLIC_ENV_NAME, "1")
    monkeypatch.delenv(dm.ENV_NAME, raising=False)
    # Page tests run the real entrypoint, which checks os.environ: variables of the host or CI runner that the
    # public policy refuses (a developer token, a test database URL...) are removed so the tests stay hermetic.
    for name in list(os.environ):
        if name != dm.PUBLIC_ENV_NAME and dm.public_configuration_problems({dm.PUBLIC_ENV_NAME: "1",
                                                                             name: os.environ[name]}):
            monkeypatch.delenv(name)


def _boom():
    return Mock(side_effect=AssertionError("network used"))


# --- Mode ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [(None, False), ("", False), ("0", False), ("off", False),
                                             ("1", True), ("true", True), ("oui", True)])
def test_public_mode_is_off_by_default_fails_closed_and_implies_read_only(monkeypatch, value, expected):
    monkeypatch.delenv(dm.ENV_NAME, raising=False)
    if value is None:
        monkeypatch.delenv(dm.PUBLIC_ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(dm.PUBLIC_ENV_NAME, value)
    assert dm.public_mode() is expected
    if expected:
        assert dm.read_only()
        with pytest.raises(dm.DemoReadOnlyError):
            dm.ensure_writable("Écriture")


def test_refusal_is_a_read_only_error_with_a_public_message(public):
    with pytest.raises(dm.PublicModeError) as error:
        dm.ensure_not_public("Recherche dans le catalogue Copernicus")
    assert isinstance(error.value, dm.DemoReadOnlyError)            # Shown by the pages like read only.
    assert "indisponible en consultation publique" in str(error.value)


def test_private_mode_keeps_every_access(monkeypatch):
    monkeypatch.delenv(dm.PUBLIC_ENV_NAME, raising=False)
    dm.ensure_not_public("Recherche")                                # No exception.
    assert dm.public_configuration_problems({"CDSE_S3_SECRET_KEY": "x"}, ("ANY",)) == []


# --- Start-up configuration ------------------------------------------------------------------------------

@pytest.mark.parametrize("name, category", [
    ("CDSE_S3_ACCESS_KEY", "identifiants Copernicus"), ("CDSE_S3_SECRET_KEY", "identifiants Copernicus"),
    ("AWS_SECRET_ACCESS_KEY", "identifiants de stockage objet"),
    ("GEONAF_DATABASE_URL", "base de données"), ("GEONAF_TEST_DATABASE_URL", "base de données"),
    ("PGPASSWORD", "base de données"), ("SUPABASE_SERVICE_ROLE_KEY", "base de données"),
    ("DATABASE_URL", "base de données"), ("GEONAF_DB_ENV", "base de données"),
    ("GEONAF_EDGE_TOKEN", "service Supabase (Edge Function)"),
])
def test_a_public_process_with_credentials_or_a_database_is_refused(name, category):
    environ = {dm.PUBLIC_ENV_NAME: "1", name: "SECRET-VALUE-123"}
    assert dm.public_configuration_problems(environ) == [category]
    with pytest.raises(dm.PublicConfigurationError) as error:
        dm.ensure_public_configuration(environ)
    assert category in str(error.value)
    assert name not in str(error.value) and "SECRET-VALUE-123" not in str(error.value)


def test_empty_variables_are_not_credentials():
    assert dm.public_configuration_problems({dm.PUBLIC_ENV_NAME: "1", "CDSE_S3_SECRET_KEY": " "}) == []


@pytest.mark.parametrize("environ, category", [
    ({dm.ENV_NAME: "0"}, "lecture seule explicitement désactivée"),
    ({dm.ENV_NAME: "off"}, "lecture seule explicitement désactivée"),
    ({"GEONAF_DB_BACKEND": "postgres"}, "stockage autre que les fichiers"),
])
def test_writes_or_postgis_are_refused_in_public(environ, category):
    assert category in dm.public_configuration_problems({dm.PUBLIC_ENV_NAME: "1", **environ})


def test_allowed_public_settings_pass():
    environ = {dm.PUBLIC_ENV_NAME: "1", dm.ENV_NAME: "1", "GEONAF_DB_BACKEND": "file",
               "GEONAF_LIMIT_MAX_CELLS": "8000000", "GEONAF_CACHE_MAX_MIB": "256"}
    keys = (dm.PUBLIC_ENV_NAME, dm.ENV_NAME, "GEONAF_LIMIT_MAX_MEMORY_BYTES", "GEONAF_DEMO_PACK")
    assert dm.public_configuration_problems(environ, keys) == []


@pytest.mark.parametrize("key", ["GEONAF_EDGE_TOKEN", "CDSE_S3_SECRET_KEY", "api_key", "connections"])
def test_any_other_streamlit_secret_is_refused_without_reading_it(key):
    problems = dm.public_configuration_problems({dm.PUBLIC_ENV_NAME: "1"}, (key,))
    assert problems == ["secret non autorisé en consultation publique"]


# --- Environment policy: application settings, system variables, secrets of other services --------------

SECRET_NAMES = ["GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_API_KEY",
                "OPENAI_API_KEY", "HF_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_STORAGE_CONNECTION_STRING",
                "MY_SERVICE_PASSWORD", "SMTP_PASSWD", "DB_PWD", "MAPBOX_ACCESS_TOKEN", "STRIPE_SECRET_KEY",
                "JWT_SECRET", "SERVICE_PRIVATE_KEY", "DB_PASSPHRASE", "SENTRY_DSN", "CLIENT_CREDENTIALS",
                "POSTGRES_PASSWORD", "MONGO_DATABASE_URL", "REDIS_DB_URL", "TOKEN", "password", "github_token"]
LEGITIMATE_NAMES = ["TOKENIZERS_PARALLELISM", "TOKEN_URL", "PASSWORD_STORE_DIR", "PWD", "OLDPWD", "GPG_KEY",
                    "PATH", "HOME", "HOSTNAME", "LANG", "TZ", "PYTHONPATH", "PYTHONHASHSEED", "SSH_AUTH_SOCK",
                    "KEYRING_BACKEND", "STREAMLIT_SERVER_PORT", "STREAMLIT_SERVER_HEADLESS", "XDG_CACHE_HOME",
                    "GITHUB_ACTIONS", "GITHUB_WORKSPACE", "RUNNER_OS", "CI", "HTTP_PROXY", "NUMBER_OF_PROCESSORS"]


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_secrets_of_other_services_are_refused_without_naming_them(name):
    environ = {dm.PUBLIC_ENV_NAME: "1", name: "SECRET-VALUE-123"}
    problems = dm.public_configuration_problems(environ)
    assert problems and set(problems) <= {dm.THIRD_PARTY_SECRET, "base de données"}
    with pytest.raises(dm.PublicConfigurationError) as error:
        dm.ensure_public_configuration(environ)
    assert name not in str(error.value) and name.upper() not in str(error.value)
    assert "SECRET-VALUE-123" not in str(error.value)


@pytest.mark.parametrize("name", LEGITIMATE_NAMES)
def test_system_and_technical_variables_are_not_secrets(name):
    assert not dm.is_secret_name(name)
    assert dm.public_configuration_problems({dm.PUBLIC_ENV_NAME: "1", name: "value"}) == []


def test_application_settings_follow_an_allowlist():
    allowed = {dm.PUBLIC_ENV_NAME: "1", dm.ENV_NAME: "1", "GEONAF_DB_BACKEND": "file", "GEONAF_CACHE_DIR": "/tmp/c",
               "GEONAF_CACHE_MAX_MIB": "128", "GEONAF_MAX_HEAVY_COMPUTATIONS": "1", "GEONAF_HEAVY_WAIT_SECONDS": "30",
               "GEONAF_DEMO_PACK": "off", "GEONAF_DEMO_PACK_URL": "https://example.org/p.zip",
               "GEONAF_DEMO_PACK_SHA256": "a" * 64, "GEONAF_DEMO_PACK_ID": "p", "GABON_GEOAI_DATA_DIR": "/data",
               "GEONAF_TLS_MODE": "system", "GEONAF_NETWORK_READ_TIMEOUT": "30", "GEONAF_LIMIT_MAX_CELLS": "8000000"}
    assert dm.public_configuration_problems(allowed) == []
    for name in ("GEONAF_ALLOW_DESTRUCTIVE_TESTS", "GEONAF_SCHEMA", "GABON_SOMETHING"):
        assert dm.public_configuration_problems({**allowed, name: "1"}) == [dm.UNKNOWN_SETTING]


def test_the_entrypoint_refuses_a_github_token_without_naming_it(public, monkeypatch, caplog):
    from streamlit.testing.v1 import AppTest
    from tests.test_demo_read_only import ROOT

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_FAKEVALUE1234567890")
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    errors = [e.value for e in app.error]
    assert len(errors) == 1 and dm.THIRD_PARTY_SECRET in errors[0] and not app.title
    for text in (errors[0], caplog.text):
        assert "GITHUB" not in text and "ghp_FAKE" not in text


# --- Resource ceilings (public only) ----------------------------------------------------------------------

def test_every_resource_limit_has_a_declared_direction():
    from src.services.resource_guards import ResourceLimits

    names = {item.name for item in fields(ResourceLimits)}
    assert dm.LOOSER_WHEN_LOWER | dm.LOOSER_WHEN_HIGHER == names
    assert not dm.LOOSER_WHEN_LOWER & dm.LOOSER_WHEN_HIGHER


@pytest.mark.parametrize("name, value", [
    ("GEONAF_LIMIT_MAX_CELLS", "16000001"), ("GEONAF_LIMIT_MAX_MEMORY_BYTES", "1000000000000"),
    ("GEONAF_LIMIT_AVAILABLE_MEMORY_REFUSE_FRACTION", "1"), ("GEONAF_LIMIT_MAX_S2_TILES", "5"),
    ("GEONAF_LIMIT_MAX_OSM_BYTES", "1000000000"), ("GEONAF_LIMIT_MAX_IMPORT_BYTES", "100000000"),
    ("GEONAF_LIMIT_BYTES_PER_CELL", "1"), ("GEONAF_LIMIT_BASELINE_BYTES", "1"), ("GEONAF_LIMIT_OSM_EXPANSION", "2"),
    ("GEONAF_MAX_HEAVY_COMPUTATIONS", "3"), ("GEONAF_HEAVY_WAIT_SECONDS", "91"), ("GEONAF_CACHE_MAX_MIB", "257"),
])
def test_looser_resource_settings_are_refused_in_public(name, value):
    assert dm.public_configuration_problems({dm.PUBLIC_ENV_NAME: "1", name: value}) == [dm.RESOURCE_ABOVE_CEILING]
    assert dm.public_configuration_problems({name: value}) == []          # Private mode keeps its behaviour.


@pytest.mark.parametrize("name, value", [
    ("GEONAF_LIMIT_MAX_CELLS", "8000000"), ("GEONAF_LIMIT_MAX_MEMORY_BYTES", "1073741824"),
    ("GEONAF_LIMIT_BYTES_PER_CELL", "300"), ("GEONAF_LIMIT_OSM_EXPANSION", "40"), ("GEONAF_MAX_HEAVY_COMPUTATIONS", "1"),
    ("GEONAF_HEAVY_WAIT_SECONDS", "10"), ("GEONAF_CACHE_MAX_MIB", "64"), ("GEONAF_CACHE_MAX_MIB", "256"),
])
def test_stricter_or_equal_resource_settings_are_kept(name, value):
    assert dm.public_configuration_problems({dm.PUBLIC_ENV_NAME: "1", name: value}) == []


@pytest.mark.parametrize("name, value", [
    ("GEONAF_LIMIT_MAX_CELLS", "beaucoup"), ("GEONAF_LIMIT_MAX_CELLS", "0"), ("GEONAF_LIMIT_MAX_CELLS", "-5"),
    ("GEONAF_LIMIT_BYTES_PER_CELL", "nan"), ("GEONAF_LIMIT_WARN_CELLS", "20000000"), ("GEONAF_LIMIT_MAX_CELL", "1"),
    ("GEONAF_MAX_HEAVY_COMPUTATIONS", "0"), ("GEONAF_HEAVY_WAIT_SECONDS", "x"), ("GEONAF_CACHE_MAX_MIB", "1.5"),
])
def test_invalid_resource_settings_are_refused_in_public(name, value):
    assert dm.public_configuration_problems({dm.PUBLIC_ENV_NAME: "1", name: value}) == [dm.RESOURCE_INVALID]


def test_the_entrypoint_serves_no_page_when_the_public_configuration_is_refused(public, monkeypatch, caplog):
    from streamlit.testing.v1 import AppTest
    from tests.test_demo_read_only import ROOT

    monkeypatch.setenv("CDSE_S3_SECRET_KEY", "SECRET-VALUE-123")
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    errors = [e.value for e in app.error]
    assert len(errors) == 1 and "Configuration refusée pour une consultation publique" in errors[0]
    assert "identifiants Copernicus" in errors[0] and "CDSE" not in errors[0] and "SECRET-VALUE" not in errors[0]
    assert not app.title                                             # No page ran.
    assert "SECRET-VALUE" not in caplog.text and "CDSE_S3" not in caplog.text


# --- Services refuse even when the pages are bypassed ----------------------------------------------------

def test_copernicus_search_is_refused_before_any_query(public, tmp_path):
    from src.data.catalogue import CatalogueQuery
    from src.services.acquisition_catalogue import search_catalogue

    repository = Mock()
    repository.search = _boom()
    query = Mock(spec=CatalogueQuery)
    with pytest.raises(dm.PublicModeError):
        search_catalogue(query, tmp_path, repository=repository)
    repository.search.assert_not_called()


def test_supabase_consultation_is_refused_without_reading_the_token(public):
    from src.services import supabase_acquisitions as service

    class Untouchable(dict):
        def get(self, *_):
            raise AssertionError("secret read")

    with pytest.raises(dm.PublicModeError):
        service.resolve_config(Untouchable(), {})
    fetch = _boom()
    with pytest.raises(dm.PublicModeError):
        service.consult(Mock(), fetch=fetch)
    fetch.assert_not_called()


def test_the_edge_client_never_calls_out_in_public(public):
    from src.data.edge_acquisitions import fetch_acquisitions

    session = Mock()
    session.get = _boom()
    with pytest.raises(dm.PublicModeError):
        fetch_acquisitions(Mock(), session=session)
    session.get.assert_not_called()


def test_downloads_imports_and_retrievals_stay_refused(public, tmp_path):
    from src.services.territory_catalog import import_custom_zone

    with pytest.raises(dm.DemoReadOnlyError):
        import_custom_zone(tmp_path, b"{}", label="Zone", author="Test", license_spdx="CC-BY-4.0",
                           source_date="2025-01-01")
    assert not any(tmp_path.iterdir())


# --- Notice and CSV --------------------------------------------------------------------------------------

def test_notice_texts_state_the_zone_dates_attribution_and_limits():
    from src.services import public_notice as notice

    assert "carré d'acquisition GEONAF de 12 × 12 km, pas une limite administrative" in notice.zone_statement("Owendo")
    dates = [datetime(2025, 1, 25, 9, 31, tzinfo=timezone.utc), datetime(2021, 1, 31, 9, 32, tzinfo=timezone.utc)]
    text = notice.acquisition_dates_text(dates)
    assert "2 (31/01/2021 → 25/01/2025" in text and "dernière acquisition le 25/01/2025" in text
    assert notice.acquisition_dates_text([]) == "Aucune acquisition Sentinel-2 vérifiée pour cette zone."
    assert notice.acquisition_dates_text(None).startswith("Dates des acquisitions Sentinel-2 indisponibles")
    assert notice.sentinel2_attribution([2025, 2021, 2025]) == "Contains modified Copernicus Sentinel data 2021, 2025"
    assert "n'est pas un changement territorial démontré" in notice.INTERPRETATION
    assert "1159/2013" in notice.SENTINEL_LICENCE


def _series_date(acquired, product_id):
    from types import SimpleNamespace as N

    return N(acquired_at_utc=acquired, product_id=product_id, season_id=None, included=True, exclusion_reason=None,
             valid_coverage_percent=50.0, own=N(count=1, mean=0.5, median=0.5, std=0.0), common=None,
             out_of_range_pixels=0, calibration={"quantification_value": 10000, "red_offset": -1000,
                                                 "nir_offset": -1000}, source_sha256={})


def test_csv_export_v2_carries_attribution_licence_zone_and_limits():
    from types import SimpleNamespace as N

    from src.services import ndvi_time_series as series
    from src.services.public_notice import INTERPRETATION, SENTINEL_LICENCE

    grid = N(crs=N(to_string=lambda: "EPSG:32632"), resolution=(10.0, 10.0), width=10, height=10)
    result = N(scientific_profile=N(profile_id="gabon-equatorial-owendo", version="1.0.0", fingerprint="f" * 64,
                                    aoi_id="owendo"),
               period=(date(2021, 1, 1), date(2025, 2, 28)), computed_at_utc=datetime(2026, 11, 11, tzinfo=timezone.utc),
               aoi_sha256="a" * 64, scl_policy="usable_land", grid=grid, common_coverage_percent=40.0,
               common_pixel_count=10, interpretation=N(status=N(value="exploratoire")), interpretation_scope="descriptif",
               dates=(_series_date(datetime(2021, 1, 31, tzinfo=timezone.utc), "Q"),
                      _series_date(datetime(2025, 1, 25, 9, 31, tzinfo=timezone.utc), "P")))
    text = series.series_csv(result)
    header = dict(line[2:].split(": ", 1) for line in text.splitlines() if line.startswith("# "))
    assert header["geonaf_export"] == "ndvi-time-series-v2"
    assert header["attribution"] == "Contains modified Copernicus Sentinel data 2021, 2025"
    assert header["licence"] == SENTINEL_LICENCE and header["interpretation_limits"] == INTERPRETATION
    assert "owendo" in header["zone"] and "pas une limite administrative" in header["zone"]


# --- Pages on a copy of the v3 demo pack ------------------------------------------------------------------

def test_public_pages_refuse_search_and_supabase_and_never_write(pack_copy, public, monkeypatch):
    monkeypatch.setenv("GEONAF_EDGE_ACQUISITIONS_URL", "")             # Even an empty key path stays unused.
    before = _tree(pack_copy)
    data = _run("views/analyses_temporelles.py?section=donnees", pack_copy, monkeypatch)
    assert not data.exception and not [e.value for e in data.error]
    infos = _infos(data)
    assert any(text.startswith("Recherche dans le catalogue Copernicus : fonctionnalité indisponible en "
                               "consultation publique") for text in infos)
    assert any(text.startswith("Consultation Supabase : fonctionnalité indisponible en consultation publique")
               for text in infos)
    labels = [button.label for button in data.button]
    assert "Rechercher dans Copernicus" not in labels and not [l for l in labels if l.startswith("Consulter")]
    assert _tree(pack_copy) == before


def _pack_acquisitions(pack, aoi_id: str) -> list[datetime]:
    """Sentinel-2 acquisition instants of one zone, read from the pack manifest (``sentinel2.acquisitions``).

    v4 packs record ``aoi_id`` on each acquisition; v3 packs hold one zone (Akanda) and do not.
    """
    import json

    manifest = json.loads((pack / "PACK_MANIFEST.json").read_text(encoding="utf-8"))
    return [datetime.fromisoformat(item["acquired_at_utc"].replace("Z", "+00:00"))
            for item in manifest["sentinel2"]["acquisitions"] if item.get("aoi_id", "akanda") == aoi_id]


@pytest.mark.parametrize("page", ["app.py", "views/analyses_temporelles.py?section=serie",
                                  "views/analyses_multisources.py"])
def test_public_pages_state_the_zone_dates_attribution_and_limits(page, pack_copy, public, monkeypatch):
    app = _run(page, pack_copy, monkeypatch)
    assert not app.exception and not [e.value for e in app.error]
    # The pages open on Akanda (first zone of every pack); the expected texts follow its acquisitions.
    assert any(text.startswith("Akanda : carré d'acquisition GEONAF de 12 × 12 km, pas une limite administrative")
               for text in _infos(app))
    acquired = _pack_acquisitions(pack_copy, "akanda")
    assert acquired
    years = ", ".join(str(year) for year in sorted({moment.year for moment in acquired}))
    captions = " ".join(caption.value for caption in app.caption)
    assert f"Contains modified Copernicus Sentinel data {years}." in captions
    assert f"dernière acquisition le {max(acquired):%d/%m/%Y}" in captions
    assert "n'est pas un changement territorial démontré" in captions


def test_dashboard_links_never_advertise_refused_commands_in_public(pack_copy, public, monkeypatch):
    app = _run("app.py", pack_copy, monkeypatch)
    helps = " ".join(str(link.proto.help) for link in app.get("page_link"))
    assert "télécharger sur commande" not in helps and "importer une zone" not in helps
    assert "désactivés en consultation publique" in helps
