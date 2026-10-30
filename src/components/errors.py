"""Public error display for the Streamlit interface; no Streamlit logic outside this module.

Two categories, decided only by explicit exception types:

* Expected functional errors (unavailable acquisition, insufficient coverage, pair not
  comparable, missing data, invalid user parameter, exceeded budget): the GEONAF domain
  exceptions listed in :func:`_expected_types`, whose messages are written for users. They
  are shown after a defensive masking of local paths, URLs and secret-like values. When such
  an error carries a technical cause (``raise ... from error``, or raised while handling
  another exception, even with ``from None``), the cause is logged and the message gets a
  diagnostic identifier.
* Everything else, including any generic ``ValueError`` whatever its origin: a generic
  message with a diagnostic identifier; ``str(exc)`` is never shown.

Server log (logger ``geonaf.ui``): one structured JSON record per incident, built from
exception types, ``errno``/``winerror``/``status_code`` attributes and stack locations with
shortened roots. Exception messages, the main leak vector, appear only as a secondary,
masked and truncated field. A repeat of the same failure within :data:`_REPEAT_WINDOW_S`
logs a one-line record with a counter instead of a new record.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import json
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
DIAGNOSTIC_SUFFIX = " Identifiant de diagnostic : {incident}."

_REPEAT_WINDOW_S = 600
_MAX_FRAMES, _MAX_CHAIN, _MAX_MESSAGE = 12, 5, 300
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
    from src.services.compute_slots import ComputeBusyError
    from src.services.local_preview import LocalPreviewError
    from src.services.territory_catalog import ZoneImportRefused

    return (LocalPreviewError, CatalogueError, TransferAborted, GeoBoundariesError, DemoPackError,
            ByteBudgetExceeded, IncompleteSTACSearchError, ZoneImportRefused, ComputeBusyError)


def is_expected(error: BaseException) -> bool:
    """True only for an explicitly listed domain exception, whose message is written for users."""
    return isinstance(error, _expected_types())


def _technical_cause(error: BaseException) -> BaseException | None:
    # ``__context__`` is kept even after ``raise ... from None``: it is the diagnosis.
    return error.__cause__ or error.__context__


# --- Masking -----------------------------------------------------------------------------------------

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


# --- Structured diagnosis ------------------------------------------------------------------------------

def _code_location(filename: str) -> str:
    """Project or Python-relative location; any other file keeps only its name (no private path)."""
    path = Path(filename)
    for root, alias in ((PROJECT_ROOT, "<projet>"), (Path(sys.prefix), "<python>"),
                        (Path(sys.base_prefix), "<python>")):
        try:
            return f"{alias}/{path.resolve().relative_to(root.resolve()).as_posix()}"
        except (ValueError, OSError, RuntimeError):
            continue
    return f"<autre>/{path.name}"


def _frames(error: BaseException) -> list[str]:
    summary = traceback.extract_tb(error.__traceback__)[-_MAX_FRAMES:]
    return [f"{_code_location(frame.filename)}:{frame.lineno} {frame.name}" for frame in summary]


def _log_message(error: BaseException) -> str:
    """Exception text for the server log: URLs reduced, secrets and any remaining local path masked."""
    text = log_text(str(error))
    text = _UNC_PATH.sub(_MASK_PATH, text)
    text = _WINDOWS_PATH.sub(_MASK_PATH, text)
    return _POSIX_PATH.sub(lambda m: m.group(0) if m.string[:m.start()].endswith(("<projet>", "<python>", "~"))
                           else _MASK_PATH, text)


def _describe(error: BaseException) -> dict:
    """Type, numeric attributes and location of one exception; its message only masked and truncated."""
    frames = _frames(error)
    item = {"type": f"{type(error).__module__}.{type(error).__qualname__}",
            "where": frames[-1] if frames else None}
    for name in ("errno", "winerror", "status_code", "category"):
        value = getattr(error, name, None)
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            item[name] = value
    message = _log_message(error)
    item["message_masked"] = message[:_MAX_MESSAGE] + ("…" if len(message) > _MAX_MESSAGE else "")
    return item


def _diagnosis(error: BaseException, *, incident: str, context: str, category: str) -> dict:
    record = {"incident": incident, "context": context, "category": category, **_describe(error),
              "frames": _frames(error), "chain": []}
    cause, seen = _technical_cause(error), {id(error)}
    while cause is not None and id(cause) not in seen and len(record["chain"]) < _MAX_CHAIN:
        seen.add(id(cause))
        record["chain"].append(_describe(cause))
        cause = _technical_cause(cause)
    return record


def _fingerprint(error: BaseException, context: str) -> tuple:
    last = traceback.extract_tb(error.__traceback__)[-1:] or [None]
    where = (last[0].filename, last[0].lineno) if last[0] is not None else None
    return context, type(error).__module__, type(error).__qualname__, where


def report_error(error: BaseException, *, context: str, category: str = "internal") -> str:
    """Log ``error`` once as a structured record and return its diagnostic identifier."""
    existing = getattr(error, "_geonaf_incident", None)
    if existing:
        return existing  # Already reported by an inner handler: never log the same error twice.
    fingerprint = _fingerprint(error, context)
    now = time.monotonic()
    with _seen_lock:
        previous = _seen.get(fingerprint)
        if previous is not None and now - previous[1] < _REPEAT_WINDOW_S:
            incident, count = previous[0], previous[2] + 1
            _seen[fingerprint] = (incident, previous[1], count)
        else:
            incident, count = f"GN-{uuid4().hex[:10].upper()}", 1
            _seen[fingerprint] = (incident, now, 1)
            _seen.move_to_end(fingerprint)
            while len(_seen) > 256:
                _seen.popitem(last=False)
    level = logging.ERROR if category == "internal" else logging.WARNING
    if count > 1:
        record = {"incident": incident, "context": context, "category": category,
                  "type": f"{type(error).__module__}.{type(error).__qualname__}", "repeat": count}
        logger.log(level, "ui_error_repeat %s", json.dumps(record, ensure_ascii=False, sort_keys=True))
    else:
        record = _diagnosis(error, incident=incident, context=context, category=category)
        logger.log(level, "ui_error %s", json.dumps(record, ensure_ascii=False, sort_keys=True))
    try:
        error._geonaf_incident = incident
    except AttributeError:
        pass
    return incident


def report_internal_error(error: BaseException, *, context: str) -> str:
    return report_error(error, context=context, category="internal")


# --- Display -------------------------------------------------------------------------------------------

def show_error(error: BaseException, *, context: str, prefix: str = "", suffix: str = "",
               level: str = "error") -> str | None:
    """Display ``error`` safely. Return the diagnostic identifier when one was logged."""
    show = st.warning if level == "warning" else st.error
    if is_expected(error):
        incident = None
        if _technical_cause(error) is not None:
            incident = report_error(error, context=context, category="functional_with_cause")
        tail = DIAGNOSTIC_SUFFIX.format(incident=incident) if incident else ""
        show(f"{prefix}{public_text(str(error))}{suffix}{tail}")
        return incident
    incident = report_internal_error(error, context=context)
    show(f"{prefix}{GENERIC_MESSAGE.format(incident=incident)}{suffix}")
    return incident


def show_internal_error(error: BaseException, *, context: str, message: str, level: str = "error") -> str | None:
    """Show a controlled message (never the exception text) with the diagnostic identifier.

    A listed domain error keeps its own message, written for users (e.g. « Serveur occupé »).
    """
    if is_expected(error):
        return show_error(error, context=context, level=level)
    incident = report_internal_error(error, context=context)
    (st.warning if level == "warning" else st.error)(f"{message}{DIAGNOSTIC_SUFFIX.format(incident=incident)}")
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
