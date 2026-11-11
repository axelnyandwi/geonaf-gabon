"""Read-only demonstration mode (``GEONAF_DEMO_READ_ONLY``), off by default.

Why: on a public deployment every visitor shares one data directory, the installed demo
pack. Visitor actions must not download data or write into it: a write would be shown to
every other session and would invalidate the pack (``verify_pack`` refuses undeclared files).

When the mode is on, analyses, maps and in-memory computations stay available; every
visitor-triggered download (Sentinel-2, Sentinel-1, WorldCover, OSM, geoBoundaries) and every
persistent write to shared stores (layer store, territory registry, download queue) raises
:class:`DemoReadOnlyError`. The checks sit in the services and in the store primitives, not
only in the interface. The demo pack loader (``src/data/demo_pack.py``) does not use these
stores and keeps its own verified installation.

The variable is read at each call. Empty or ``0``/``false``/``no``/``off`` disables the mode;
any other value enables it (an unrecognised value fails closed).

A process that serves the shared demo pack is read-only whatever the variable says
(:func:`lock_for_shared_pack`): a public deployment cannot be left writable by a missing or
wrong secret. Local data keep the variable's behaviour.
"""

from __future__ import annotations

import os

ENV_NAME = "GEONAF_DEMO_READ_ONLY"
_DISABLED = frozenset({"", "0", "false", "no", "off"})
_shared_pack = False   # Set once this process serves the demo pack; never cleared by the application.


class DemoReadOnlyError(RuntimeError):
    """Operation refused in read-only demonstration mode; the message is for users."""

    def __init__(self, operation: str):
        self.operation = operation
        super().__init__(unavailable_text(operation))


def unavailable_text(operation: str) -> str:
    return (f"{operation} : fonctionnalité indisponible en démonstration (lecture seule). "
            "Les données du paquet de démonstration restent consultables et analysables.")


def lock_for_shared_pack() -> None:
    """The data directory is the demo pack shared by every session: read-only for the process."""
    global _shared_pack
    _shared_pack = True


def read_only(environ=os.environ) -> bool:
    return _shared_pack or public_mode(environ) or environ.get(ENV_NAME, "").strip().lower() not in _DISABLED


def ensure_writable(operation: str, environ=os.environ) -> None:
    """Raise :class:`DemoReadOnlyError` before any download or shared write in read-only mode."""
    if read_only(environ):
        raise DemoReadOnlyError(operation)


# --- Public consultation (Discovery without authentication) -------------------------------------------
#
# ``GEONAF_PUBLIC`` (same parsing as GEONAF_DEMO_READ_ONLY, fails closed) marks an anonymous public
# deployment. It implies read only, and it also refuses what read only alone still allows because it
# does not write: outbound queries started by a visitor (Copernicus STAC search) and the Supabase
# consultation, which needs a server token. The refusal sits in the services, not only in the pages.
# At start-up the configuration itself is checked (public_configuration_problems): a public process
# holding credentials, a database URL or a disabled read-only flag does not serve any page.

PUBLIC_ENV_NAME = "GEONAF_PUBLIC"
# Environment variables that must be absent (or empty) in a public process. Prefixes, upper case.
SENSITIVE_PREFIXES = {
    "identifiants Copernicus": ("CDSE_",),
    "identifiants de stockage objet": ("AWS_",),
    "base de données": ("GEONAF_DATABASE_", "GEONAF_TEST_DATABASE", "GEONAF_DB_ENV", "GEONAF_DB_SSLROOTCERT",
                        "SUPABASE_", "DATABASE_URL", "PG"),
    "service Supabase (Edge Function)": ("GEONAF_EDGE_",),
}
# Streamlit secrets a public process may hold: non-secret settings only. Anything else is refused.
PUBLIC_SECRET_KEYS = frozenset({PUBLIC_ENV_NAME, ENV_NAME, "GEONAF_DB_BACKEND", "GEONAF_CACHE_DIR",
                                "GEONAF_CACHE_MAX_MIB", "GEONAF_MAX_HEAVY_COMPUTATIONS", "GEONAF_HEAVY_WAIT_SECONDS",
                                "GEONAF_DEMO_PACK", "GEONAF_DEMO_PACK_URL", "GEONAF_DEMO_PACK_SHA256",
                                "GEONAF_DEMO_PACK_ID"})
PUBLIC_SECRET_PREFIXES = ("GEONAF_LIMIT_",)


class PublicModeError(DemoReadOnlyError):
    """Operation refused in public consultation; a DemoReadOnlyError, so pages show it the same way."""

    def __init__(self, operation: str):
        RuntimeError.__init__(self, public_unavailable_text(operation))
        self.operation = operation


class PublicConfigurationError(RuntimeError):
    """The process is marked public but its configuration allows sensitive access; message without names."""


def public_unavailable_text(operation: str) -> str:
    return (f"{operation} : fonctionnalité indisponible en consultation publique. Les données du paquet de "
            "démonstration vérifié restent consultables.")


def public_mode(environ=os.environ) -> bool:
    return environ.get(PUBLIC_ENV_NAME, "").strip().lower() not in _DISABLED


def ensure_not_public(operation: str, environ=os.environ) -> None:
    """Raise :class:`PublicModeError` before a visitor-triggered outbound or credentialed access."""
    if public_mode(environ):
        raise PublicModeError(operation)


def public_configuration_problems(environ=os.environ, secret_keys=()) -> list[str]:
    """Categories of forbidden settings of a public process (empty list: allowed). Never values or names.

    ``secret_keys``: the top-level keys of the Streamlit secrets (their values are never read).
    """
    if not public_mode(environ):
        return []
    problems = []
    flag = environ.get(ENV_NAME)
    if flag is not None and flag.strip().lower() in _DISABLED - {""}:
        problems.append("lecture seule explicitement désactivée")
    if environ.get("GEONAF_DB_BACKEND", "").strip().lower() not in ("", "file"):
        problems.append("stockage autre que les fichiers")
    for category, prefixes in SENSITIVE_PREFIXES.items():
        if any(key.upper().startswith(prefixes) and str(value).strip() for key, value in environ.items()):
            problems.append(category)
    if any(not (str(key) in PUBLIC_SECRET_KEYS or str(key).startswith(PUBLIC_SECRET_PREFIXES)) for key in secret_keys):
        problems.append("secret non autorisé en consultation publique")
    return problems


def ensure_public_configuration(environ=os.environ, secret_keys=()) -> None:
    problems = public_configuration_problems(environ, secret_keys)
    if problems:
        raise PublicConfigurationError(
            "Configuration refusée pour une consultation publique (" + ", ".join(sorted(set(problems))) + ").")
