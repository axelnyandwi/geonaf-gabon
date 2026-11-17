"""Read the acquisitions of the Supabase test project over HTTPS, through the Edge Function (D5).

Opt-in client, used only when called explicitly: the storage layer (``src.data.storage``) and the
interface are unchanged, and the file backend stays the default. No PostgreSQL port is needed on the
workstation: the function connects to the database on Supabase's side, as the dedicated read-only role.

Configuration (environment, never versioned):
* ``GEONAF_EDGE_ACQUISITIONS_URL``: https://<project-ref>.supabase.co/functions/v1/geonaf-acquisitions
* ``GEONAF_EDGE_TOKEN``: shared bearer token, at least 32 bytes, also stored as a function secret.

Security: verified TLS through ``secure_session`` (system or corporate CA bundle, HTTP refused, no
.netrc), no redirect followed (the token is never sent elsewhere), body capped at 1 MiB, strict schema
check of every record. Errors never contain the token, the URL or the response body.
Dates: ``acquired_at`` (sensing) and ``ingested_at`` (GEONAF ingestion) stay separate; the age of an
observation is computed from ``acquired_at`` by the caller, never from the request time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
import re
from typing import Mapping
from urllib.parse import urlsplit

from src.network_config import NetworkSettings

FUNCTION_PATH = "/functions/v1/geonaf-acquisitions"
MAX_BODY_BYTES = 1024 * 1024
MAX_RECORDS = 100                                  # LIMIT of the function's query.
_HOST = re.compile(r"[a-z0-9]{1,64}\.supabase\.co")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FIELDS = {"manifest_sha256", "collection", "product_id", "acquired_at", "ingested_at", "grid_crs",
           "grid_resolution_m", "integrity_mode"}


class EdgeAcquisitionsError(RuntimeError):
    """Public failure with a stable category; never includes secrets, URLs or response content."""

    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


@dataclass(frozen=True)
class EdgeConfig:
    url: str
    token: str

    def __repr__(self) -> str:                    # The token must never reach a log or a traceback.
        return f"EdgeConfig(url={self.url!r}, token=<masqué>)"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "EdgeConfig":
        env = os.environ if environ is None else environ
        return cls(url=validate_function_url(env.get("GEONAF_EDGE_ACQUISITIONS_URL", "")),
                   token=validate_token(env.get("GEONAF_EDGE_TOKEN", "")))


@dataclass(frozen=True)
class EdgeAcquisition:
    manifest_sha256: str
    collection: str
    product_id: str
    acquired_at: datetime
    ingested_at: datetime
    grid_crs: str
    grid_resolution_m: float
    integrity_mode: str | None


def validate_function_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        valid = (parsed.scheme == "https" and parsed.hostname is not None and _HOST.fullmatch(parsed.hostname)
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and parsed.path.rstrip("/") == FUNCTION_PATH and not parsed.query and not parsed.fragment)
    except (AttributeError, ValueError):
        valid = False
    if not valid:
        raise EdgeAcquisitionsError("configuration", "Consultation Supabase mal configurée sur ce serveur (adresse du service invalide).")
    return f"https://{parsed.hostname}{FUNCTION_PATH}"


def validate_token(value: str) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) < 32 or len(value) > 4096 \
            or not value.isprintable() or value != value.strip():
        raise EdgeAcquisitionsError("configuration", "Consultation Supabase mal configurée sur ce serveur (jeton absent ou trop court).")
    return value


def _timestamp(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed


def parse_payload(payload) -> tuple[EdgeAcquisition, ...]:
    """Strict check of the function's JSON: exact fields, types, test environment, bounded count."""
    try:
        if not isinstance(payload, dict) or set(payload) != {"environment", "count", "acquisitions"}:
            raise ValueError
        records = payload["acquisitions"]
        if payload["environment"] != "test" or not isinstance(records, list) or len(records) > MAX_RECORDS \
                or payload["count"] != len(records):
            raise ValueError
        result = []
        for record in records:
            if not isinstance(record, dict) or set(record) != _FIELDS:
                raise ValueError
            resolution = record["grid_resolution_m"]
            item = EdgeAcquisition(
                manifest_sha256=record["manifest_sha256"], collection=record["collection"],
                product_id=record["product_id"], acquired_at=_timestamp(record["acquired_at"]),
                ingested_at=_timestamp(record["ingested_at"]), grid_crs=record["grid_crs"],
                grid_resolution_m=resolution, integrity_mode=record["integrity_mode"])
            if not (isinstance(item.manifest_sha256, str) and _SHA256.fullmatch(item.manifest_sha256)
                    and isinstance(item.collection, str) and isinstance(item.product_id, str)
                    and isinstance(item.grid_crs, str) and item.grid_crs.strip()
                    and isinstance(resolution, (int, float)) and not isinstance(resolution, bool) and resolution > 0
                    and item.integrity_mode in (None, "strict", "exploratory")
                    and item.ingested_at >= item.acquired_at):
                raise ValueError
            result.append(item)
        return tuple(result)
    except (KeyError, TypeError, ValueError):
        raise EdgeAcquisitionsError("protocol", f"{SERVICE} : réponse non conforme au contrat attendu, ignorée.") from None


# Messages written for users (shown by src/components/errors.py): no status detail, URL, host or token.
SERVICE = "Service des acquisitions Supabase (geonaf-test)"
_STATUS = {401: ("unauthorized", f"{SERVICE} : accès refusé, jeton du serveur GEONAF non reconnu. "
                                 "Contacter l'administrateur."),
           429: ("busy", f"{SERVICE} momentanément saturé. Réessayer dans quelques secondes."),
           503: ("unavailable", f"{SERVICE} indisponible (base ou configuration du service). Réessayer plus tard.")}


def fetch_acquisitions(config: EdgeConfig, *, session=None, settings: NetworkSettings | None = None,
                       timeout=(10.0, 20.0)) -> tuple[EdgeAcquisition, ...]:
    """One GET, no retry (the function is cheap to call again; a retry loop would multiply connections)."""
    from src.demo_mode import ensure_not_public

    ensure_not_public("Consultation Supabase")    # Last line of defence: a public process never calls out.
    if session is None:
        from src.data.network import secure_session
        from src.network_config import NetworkConfigurationError

        try:
            session = secure_session(settings or NetworkSettings.from_environment(load_file=False))
        except NetworkConfigurationError:
            raise EdgeAcquisitionsError("configuration", "Configuration TLS du serveur GEONAF invalide.") from None
    import requests
    from requests.utils import get_environ_proxies

    try:
        # secure_session ignores the environment (trust_env=False): proxies are passed explicitly, as for STAC.
        with session.get(config.url, headers={"Authorization": f"Bearer {config.token}",
                                              "Accept": "application/json"},
                         timeout=timeout, allow_redirects=False, stream=True,
                         proxies=get_environ_proxies(config.url)) as response:
            if response.status_code != 200:
                category, message = _STATUS.get(response.status_code,
                                                ("http", f"{SERVICE} : réponse inattendue. Réessayer plus tard."))
                error = EdgeAcquisitionsError(category, message)
                error.status_code = int(response.status_code)   # Logged as a number, never shown.
                raise error
            if not response.headers.get("content-type", "").startswith("application/json"):
                raise EdgeAcquisitionsError("protocol", f"{SERVICE} : réponse non JSON, ignorée.")
            body = b""
            for chunk in response.iter_content(65536):
                body += chunk
                if len(body) > MAX_BODY_BYTES:
                    raise EdgeAcquisitionsError("protocol", f"{SERVICE} : réponse trop volumineuse, ignorée.")
    except EdgeAcquisitionsError:
        raise
    except requests.exceptions.SSLError:
        raise EdgeAcquisitionsError("tls", f"{SERVICE} : certificat TLS refusé, connexion interrompue.") from None
    except requests.exceptions.Timeout:
        raise EdgeAcquisitionsError("timeout", f"{SERVICE} : délai dépassé. Réessayer plus tard.") from None
    except requests.exceptions.RequestException:
        raise EdgeAcquisitionsError("connection", f"{SERVICE} : connexion impossible.") from None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise EdgeAcquisitionsError("protocol", f"{SERVICE} : réponse non JSON, ignorée.") from None
    return parse_payload(payload)
