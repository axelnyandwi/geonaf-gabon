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
"""

from __future__ import annotations

import os

ENV_NAME = "GEONAF_DEMO_READ_ONLY"
_DISABLED = frozenset({"", "0", "false", "no", "off"})


class DemoReadOnlyError(RuntimeError):
    """Operation refused in read-only demonstration mode; the message is for users."""

    def __init__(self, operation: str):
        self.operation = operation
        super().__init__(unavailable_text(operation))


def unavailable_text(operation: str) -> str:
    return (f"{operation} : fonctionnalité indisponible en démonstration (lecture seule). "
            "Les données du paquet de démonstration restent consultables et analysables.")


def read_only(environ=os.environ) -> bool:
    return environ.get(ENV_NAME, "").strip().lower() not in _DISABLED


def ensure_writable(operation: str, environ=os.environ) -> None:
    """Raise :class:`DemoReadOnlyError` before any download or shared write in read-only mode."""
    if read_only(environ):
        raise DemoReadOnlyError(operation)
