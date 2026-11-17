"""Explicit HTTPS consultation of the acquisitions recorded in Supabase geonaf-test (D5); no Streamlit here.

The interface calls :func:`consult` only when a user asks for it. Nothing here touches the file or PostGIS
stores: the file mode stays the default and the PostGIS readers are unchanged. No PostgreSQL connection is
opened by GEONAF: the Edge Function reads the database on Supabase's side, with its dedicated role.

Configuration, server side only and never sent to the browser: ``GEONAF_EDGE_ACQUISITIONS_URL`` and
``GEONAF_EDGE_TOKEN`` from Streamlit secrets (``.streamlit/secrets.toml`` locally, the app's secrets in
production), else from the process environment. Both or none: a half configuration is an error.

Scientific scope: these are ingestion manifests (metadata). An acquisition is neither proof that the zone
was effectively observed (clouds and valid pixels are not evaluated here) nor an indicator result, and no
territorial change can be inferred from it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from src.data import edge_acquisitions as client
from src.data.edge_acquisitions import MAX_RECORDS, EdgeAcquisition, EdgeAcquisitionsError, EdgeConfig

SOURCE_LABEL = "Supabase geonaf-test"
KEYS = ("GEONAF_EDGE_ACQUISITIONS_URL", "GEONAF_EDGE_TOKEN")


@dataclass(frozen=True)
class Consultation:
    """One explicit consultation: the records and when they were read. Holds no secret."""
    acquisitions: tuple[EdgeAcquisition, ...]
    consulted_at_utc: datetime

    @property
    def truncated(self) -> bool:
        """The function returns at most MAX_RECORDS rows: a full page may hide older acquisitions."""
        return len(self.acquisitions) >= MAX_RECORDS


def _value(source: Mapping[str, object], key: str) -> str:
    value = source.get(key) if source is not None else None
    return value.strip() if isinstance(value, str) else ""


def resolve_config(secrets: Mapping[str, object] | None, environ: Mapping[str, str]) -> EdgeConfig | None:
    """Streamlit secrets first, then the environment; None when the consultation is not configured.

    The two values must come from the same source, so that a secret URL is never paired with a stray
    environment token (or the reverse).
    """
    from src.demo_mode import ensure_not_public

    # The process environment decides, never the mapping passed in (a caller could omit the flag).
    ensure_not_public("Consultation Supabase")              # Public process: no token is even read.
    for source in (secrets or {}, environ):
        values = {key: _value(source, key) for key in KEYS}
        if any(values.values()):
            if not all(values.values()):
                raise EdgeAcquisitionsError(
                    "configuration", "Consultation Supabase mal configurée sur ce serveur (adresse ou jeton manquant).")
            return EdgeConfig.from_environment(values)      # Validates the URL and the token.
    return None


def consult(config: EdgeConfig, *, fetch=None, now=None) -> Consultation:
    """One HTTPS GET through the verified client (TLS, timeouts, strict JSON contract, no redirect)."""
    from src.demo_mode import ensure_not_public

    ensure_not_public("Consultation Supabase")
    records = (fetch or client.fetch_acquisitions)(config)
    return Consultation(records, (now or (lambda: datetime.now(timezone.utc)))())


def display_rows(consultation: Consultation) -> list[dict]:
    """Rows for the table; dates in UTC; age counted from the acquisition date, never from the ingestion."""
    rows = []
    for item in consultation.acquisitions:
        acquired = item.acquired_at.astimezone(timezone.utc)
        ingested = item.ingested_at.astimezone(timezone.utc)
        rows.append({
            "Produit": item.product_id,
            "Collection": item.collection,
            "Acquisition (UTC)": acquired.strftime("%Y-%m-%d %H:%M"),
            "Ingestion GEONAF (UTC)": ingested.strftime("%Y-%m-%d %H:%M"),
            "Ancienneté de l'observation (jours)": max(0, (consultation.consulted_at_utc - acquired).days),
            "Grille": f"{item.grid_crs}, {item.grid_resolution_m:g} m",
            "Intégrité du manifeste": {"strict": "stricte", "exploratory": "exploratoire"}.get(
                item.integrity_mode, "non renseignée"),
            "Manifeste (SHA-256)": item.manifest_sha256[:12] + "…",
        })
    return rows
