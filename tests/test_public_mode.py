"""Public consultation (GEONAF_PUBLIC, Discovery without authentication): Discovery V4 phase A.

* the mode implies read only and fails closed;
* visitor-triggered outbound or credentialed access (Copernicus search, Supabase) is refused in the services,
  so a bypass of the pages cannot run it; the network doubles fail the test if touched;
* a public process holding credentials, a database URL, an unknown secret or a disabled read-only flag serves
  no page, and the refusal names categories only, never a variable name or a value;
* pages run on a copy of the v3 demo pack: same pages as the private mode, without the refused commands,
  with the zone status, dates, attribution and limits, and without writing the pack.
"""

from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

from src import demo_mode as dm
from tests.test_demo_read_only import _infos, _run, _tree, pack_copy  # noqa: F401  (fixture)


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setenv(dm.PUBLIC_ENV_NAME, "1")
    monkeypatch.delenv(dm.ENV_NAME, raising=False)


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


@pytest.mark.parametrize("page", ["app.py", "views/analyses_temporelles.py?section=serie",
                                  "views/analyses_multisources.py"])
def test_public_pages_state_the_zone_dates_attribution_and_limits(page, pack_copy, public, monkeypatch):
    app = _run(page, pack_copy, monkeypatch)
    assert not app.exception and not [e.value for e in app.error]
    assert any("carré d'acquisition GEONAF de 12 × 12 km, pas une limite administrative" in text
               for text in _infos(app))
    captions = " ".join(caption.value for caption in app.caption)
    assert "Contains modified Copernicus Sentinel data 2025" in captions
    assert "dernière acquisition le 25/01/2025" in captions and "n'est pas un changement territorial démontré" in captions


def test_dashboard_links_never_advertise_refused_commands_in_public(pack_copy, public, monkeypatch):
    app = _run("app.py", pack_copy, monkeypatch)
    helps = " ".join(str(link.proto.help) for link in app.get("page_link"))
    assert "télécharger sur commande" not in helps and "importer une zone" not in helps
    assert "désactivés en consultation publique" in helps
