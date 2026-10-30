"""Public error display for the Streamlit interface; no Streamlit logic outside this module.

Two categories, decided explicitly:

* Expected functional errors (unavailable acquisition, insufficient coverage, pair not
  comparable, missing data, invalid user parameter, exceeded budget): GEONAF domain
  exceptions listed in :func:`_expected_types`, and plain ``ValueError`` raised by GEONAF
  code itself (innermost frame under ``src/``), whose messages are written for users. They
  are shown after a defensive masking of local paths, URLs and secret-like values.
* Everything else (unexpected Python error, library error, file or path error, network or
  authentication failure, programming error): a generic message with a diagnostic
  identifier. ``str(exc)`` is never shown. The traceback is logged once on the server with
  the same identifier, with paths shortened and URLs/secrets masked; a repeat of the same
  failure within :data:`_REPEAT_WINDOW_S` logs one line instead of the full traceback.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import re
import sys
from threading import Lock
import time
import traceback
from uuid import uuid4

import streamlit as st

from src.config import PROJECT_ROOT

logger = logging.getLogger("geonaf.ui")

GENERIC_MESSAGE = ("Erreur interne : l'opération n'a pas pu aboutir. Aucune donnée partielle n'est affichée. "
                   "Identifiant de diagnostic : {incident}.")

_SRC_ROOT = (PROJECT_ROOT / "src").resolve()
_REPEAT_WINDOW_S = 600
_seen: OrderedDict[tuple, tuple[str, float, int]] = OrderedDict()
_seen_lock = Lock()

_MASK_PATH, _MASK_URL, _MASK_SECRET = "<chemin masqué>", "<adresse masquée>", "<valeur masquée>"
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s'\"<>]+")
_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])[a-z]:[\\/](?:[^\\/:*?\"<>|\r\n]+[\\/])*[^\\/:*?\"<>|\s]*")
_UNC_PATH = re.compile(r"\\\\[^\\\s]+\\[^\s:]+")
# Absolute POSIX path with at least two segments ("VV/VH" or "m/pixel" are left intact).
_POSIX_PATH = re.compile(r"(?<![\w.:/~-])/(?:[\w.~-]+/)+[\w.~-]*")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([\w-]*(?:token|secret|password|passwd|signature|credential|api[_-]?key|access[_-]?key)[\w-]*)"
    r"(\s*[=:]\s*)[^\s,;&'\"]+")
_AUTH_HEADER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_AWS_KEY_ID = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_SECRET_ENV = re.compile(r"(?i)(secret|token|password|passwd|access_key|api_key)")


def _expected_types() -> tuple[type[BaseException], ...]:
    # Imported lazily: domain modules are heavy and must not depend on the interface.
    from src.data.catalogue import CatalogueError
    from src.data.demo_pack import DemoPackError
    from src.data.download_control import TransferAborted
    from src.data.geoboundaries import GeoBoundariesError
    from src.data.remote_window import ByteBudgetExceeded
    from src.geo.pair_selection import IncompleteSTACSearchError
    from src.services.local_preview import LocalPreviewError

    return (LocalPreviewError, CatalogueError, TransferAborted, GeoBoundariesError, DemoPackError,
            ByteBudgetExceeded, IncompleteSTACSearchError)


def _innermost_file(error: BaseException) -> Path | None:
    tb = error.__traceback__
    if tb is None:
        return None
    while tb.tb_next is not None:
        tb = tb.tb_next
    try:
        return Path(tb.tb_frame.f_code.co_filename).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def is_expected(error: BaseException) -> bool:
    """True for a functional error whose message is written for users."""
    if isinstance(error, _expected_types()):
        return True
    if type(error) is ValueError:  # Subclasses (JSON, Unicode, configuration...) are not assumed public.
        origin = _innermost_file(error)
        return origin is not None and origin.is_relative_to(_SRC_ROOT)
    return False


def _secret_values() -> list[str]:
    return sorted((value for name, value in os.environ.items()
                   if value and len(value) >= 8 and _SECRET_ENV.search(name)), key=len, reverse=True)


def _mask_secrets(text: str) -> str:
    for value in _secret_values():
        text = text.replace(value, _MASK_SECRET)
    text = _SECRET_ASSIGNMENT.sub(lambda m: m.group(1) + m.group(2) + _MASK_SECRET, text)
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)} {_MASK_SECRET}", text)
    return _AWS_KEY_ID.sub(_MASK_SECRET, text)


def public_text(message: str) -> str:
    """Mask local paths, URLs and secret-like values in a message shown to users."""
    text = _mask_secrets(str(message))
    text = _URL.sub(_MASK_URL, text)
    text = _UNC_PATH.sub(_MASK_PATH, text)
    text = _WINDOWS_PATH.sub(_MASK_PATH, text)
    return _POSIX_PATH.sub(_MASK_PATH, text)


def _log_url(match: re.Match) -> str:
    # Keep scheme, host and path for diagnosis; drop credentials, query and fragment.
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(match.group(0))
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return _MASK_URL
    suffix = "?<masqué>" if parts.query else ""
    return f"{parts.scheme}://{host}{port}{parts.path}{suffix}"


def log_text(text: str) -> str:
    """Shorten known roots (project, Python, home) and mask URLs/secrets for server logs."""
    text = _mask_secrets(text)
    text = _URL.sub(_log_url, text)
    roots = [(PROJECT_ROOT, "<projet>"), (Path(sys.prefix), "<python>"),
             (Path(sys.base_prefix), "<python>"), (Path.home(), "~")]
    for root, alias in sorted(roots, key=lambda item: len(str(item[0])), reverse=True):
        for variant in {str(root), root.as_posix()}:
            if len(variant) > 3:
                text = text.replace(variant, alias)
    return text


def report_internal_error(error: BaseException, *, context: str) -> str:
    """Log ``error`` once with a new diagnostic identifier and return that identifier."""
    existing = getattr(error, "_geonaf_incident", None)
    if existing:
        return existing  # Already reported by an inner handler: never log the same error twice.
    origin = _innermost_file(error)
    fingerprint = (context, type(error).__module__, type(error).__qualname__, str(origin),
                   getattr(error.__traceback__, "tb_lineno", None))
    now = time.monotonic()
    with _seen_lock:
        previous = _seen.get(fingerprint)
        if previous is not None and now - previous[1] < _REPEAT_WINDOW_S:
            incident, count = previous[0], previous[2] + 1
            _seen[fingerprint] = (incident, previous[1], count)
            repeat = True
        else:
            incident, repeat = f"GN-{uuid4().hex[:10].upper()}", False
            _seen[fingerprint] = (incident, now, 1)
            _seen.move_to_end(fingerprint)
            while len(_seen) > 256:
                _seen.popitem(last=False)
    if repeat:
        logger.error("ui_internal_error_repeat incident=%s context=%s type=%s count=%d",
                     incident, context, type(error).__name__, count)
    else:
        details = log_text("".join(traceback.format_exception(type(error), error, error.__traceback__)))
        logger.error("ui_internal_error incident=%s context=%s type=%s\n%s",
                     incident, context, type(error).__name__, details)
    try:
        error._geonaf_incident = incident
    except AttributeError:
        pass
    return incident


def show_error(error: BaseException, *, context: str, prefix: str = "", suffix: str = "",
               level: str = "error") -> str | None:
    """Display ``error`` safely. Return the diagnostic identifier for an internal error."""
    show = st.warning if level == "warning" else st.error
    if is_expected(error):
        show(f"{prefix}{public_text(str(error))}{suffix}")
        return None
    incident = report_internal_error(error, context=context)
    show(f"{prefix}{GENERIC_MESSAGE.format(incident=incident)}{suffix}")
    return incident


def show_internal_error(error: BaseException, *, context: str, message: str, level: str = "error") -> str:
    """Keep an existing generic message and append the diagnostic identifier."""
    incident = report_internal_error(error, context=context)
    (st.warning if level == "warning" else st.error)(f"{message} Identifiant de diagnostic : {incident}.")
    return incident


@contextmanager
def page_guard(context: str):
    """Catch any unexpected exception of a page or fragment; ``st.stop``/``st.rerun`` pass through.

    Streamlit control flow uses ``BaseException`` subclasses, which are not caught here.
    """
    try:
        yield
    except Exception as error:
        show_error(error, context=context)
