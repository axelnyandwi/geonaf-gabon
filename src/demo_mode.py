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
import re

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
# holding credentials (GEONAF's own or another service's), a database URL, an unknown GEONAF setting, a
# disabled read-only flag or resource limits looser than the defaults does not serve any page.

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

# Environment policy of a public process, in three tiers (names compared in upper case):
# 1. Application settings (GEONAF_*, GABON_*): only this allowlist; any other one is refused.
PUBLIC_APP_SETTINGS = PUBLIC_SECRET_KEYS | {"GABON_GEOAI_DATA_DIR", "GEONAF_CA_BUNDLE", "GEONAF_TLS_MODE",
                                            "GEONAF_STAC_ENDPOINT", "GEONAF_S3_ENDPOINT"}
PUBLIC_APP_PREFIXES = PUBLIC_SECRET_PREFIXES + ("GEONAF_NETWORK_",)
APP_NAMESPACES = ("GEONAF_", "GABON_")
# 2. System and technical variables are allowed unless their name is a secret name (tier 3). These known
#    names are never read as secrets: their secret-like word is not the last one, or they hold no secret.
LEGITIMATE_NAMES = frozenset({"TOKENIZERS_PARALLELISM", "TOKEN_URL", "PASSWORD_STORE_DIR", "PWD", "OLDPWD", "GPG_KEY"})
# 3. Secret names of any other service: the last word (words split on "_", "." or "-") or the last two words.
#    Whole words only, never a substring (TOKENIZERS_PARALLELISM stays allowed).
SECRET_LAST_WORDS = frozenset({"TOKEN", "SECRET", "PASSWORD", "PASSWD", "PWD", "PASSPHRASE", "CREDENTIAL",
                               "CREDENTIALS", "DSN"})
SECRET_LAST_PAIRS = frozenset({("API", "KEY"), ("ACCESS", "KEY"), ("SECRET", "KEY"), ("PRIVATE", "KEY"),
                               ("SIGNING", "KEY"), ("CONNECTION", "STRING"), ("DATABASE", "URL"), ("DB", "URL")})
SECRET_NAMES = frozenset({"GITHUB_PAT"})
THIRD_PARTY_SECRET = "secret d'un service tiers"
UNKNOWN_SETTING = "réglage applicatif non autorisé"

# Resource ceilings of a public process: the defaults (src/services/resource_guards.py, compute_slots.py,
# bounded_cache.py). A setting may be stricter, never looser. For these ResourceLimits fields a LOWER value
# is looser (smaller memory estimate); for every other field a HIGHER value is looser.
LOOSER_WHEN_LOWER = frozenset({"bytes_per_cell", "baseline_bytes", "osm_expansion"})
LOOSER_WHEN_HIGHER = frozenset({"resolution_m", "warn_cells", "max_cells", "warn_memory_bytes", "max_memory_bytes",
                                "available_memory_fraction", "available_memory_refuse_fraction", "warn_s2_tiles",
                                "max_s2_tiles", "warn_osm_bytes", "max_osm_bytes", "max_osm_bbox_degrees",
                                "max_import_bytes", "max_import_vertices", "max_import_parts"})
RESOURCE_ABOVE_CEILING = "limite de ressources au-delà du plafond public"
RESOURCE_INVALID = "limite de ressources invalide"


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


def is_secret_name(name: str) -> bool:
    """Tier 3: name of a credential of any service (``*_TOKEN``, ``*_PASSWORD``, ``*_API_KEY``...)."""
    upper = str(name).upper()
    if upper in LEGITIMATE_NAMES:
        return False
    words = [word for word in re.split(r"[_.\-]+", upper) if word]
    if not words:
        return False
    if upper in SECRET_NAMES or tuple(words[-2:]) in SECRET_LAST_PAIRS:
        return True
    # A single word is a secret name too ("TOKEN", "PASSWORD"), except PWD: the current directory.
    return words[-1] in SECRET_LAST_WORDS and (words[-1] != "PWD" or len(words) > 1)


def _sensitive_category(name: str) -> str | None:
    upper = str(name).upper()
    for category, prefixes in SENSITIVE_PREFIXES.items():
        if upper.startswith(prefixes):
            return category
    if is_secret_name(upper):
        return THIRD_PARTY_SECRET
    if upper.startswith(APP_NAMESPACES) and not (upper in PUBLIC_APP_SETTINGS or upper.startswith(PUBLIC_APP_PREFIXES)):
        return UNKNOWN_SETTING
    return None


def public_resource_problems(environ=os.environ) -> list[str]:
    """Resource settings looser than the public ceilings (the defaults), or invalid. Never names or values."""
    from dataclasses import fields

    from src.services.bounded_cache import max_bytes_from_environment
    from src.services.compute_slots import SlotSettings
    from src.services.resource_guards import ResourceLimits

    known = {f"GEONAF_LIMIT_{item.name.upper()}" for item in fields(ResourceLimits)}
    if any(str(key).upper().startswith("GEONAF_LIMIT_") and str(key).upper() not in known for key in environ):
        return [RESOURCE_INVALID]   # A misspelt limit would otherwise be ignored silently.
    try:
        limits, slots, cache = (ResourceLimits.from_environment(environ), SlotSettings.from_environment(environ),
                                max_bytes_from_environment(environ))
    except (TypeError, ValueError):
        return [RESOURCE_INVALID]
    default, default_slots = ResourceLimits(), SlotSettings()

    def looser_field(name: str) -> bool:
        value, ceiling = getattr(limits, name), getattr(default, name)
        if name in LOOSER_WHEN_LOWER:
            return value < ceiling
        if name in LOOSER_WHEN_HIGHER:
            return value > ceiling
        return value != ceiling     # A field without a declared direction may not change (fails closed).

    looser = [item.name for item in fields(ResourceLimits) if looser_field(item.name)]
    if (looser or slots.slots > default_slots.slots or slots.wait_seconds > default_slots.wait_seconds
            or cache > max_bytes_from_environment({})):
        return [RESOURCE_ABOVE_CEILING]
    return []


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
    for key, value in environ.items():
        category = _sensitive_category(key) if str(value).strip() else None
        if category is not None and category not in problems:
            problems.append(category)
    if any(not (str(key) in PUBLIC_SECRET_KEYS or str(key).startswith(PUBLIC_SECRET_PREFIXES)) for key in secret_keys):
        problems.append("secret non autorisé en consultation publique")
    problems += public_resource_problems(environ)
    return problems


def ensure_public_configuration(environ=os.environ, secret_keys=()) -> None:
    problems = public_configuration_problems(environ, secret_keys)
    if problems:
        raise PublicConfigurationError(
            "Configuration refusée pour une consultation publique (" + ", ".join(sorted(set(problems))) + ").")
