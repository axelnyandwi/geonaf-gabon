"""Helpers for diagnostics that must not disclose local filesystem paths."""

from __future__ import annotations

import re
from pathlib import Path


_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])[a-z]:[\\/](?:[^\\/:]+[\\/])*[^\\/:]+")
_UNC_PATH = re.compile(r"\\\\[^\\\s]+\\[^:\r\n]+")
_POSIX_PATH = re.compile(r"(?<![:/])/(?!/)(?:[^\s/:]+/)*[^\s/:]+")


def safe_exception_message(error: BaseException, paths=()) -> str:
    """Return the useful error text with known or recognizable local paths hidden."""
    message = str(error)
    for value in paths:
        if value is None:
            continue
        path = Path(value)
        variants = {str(path), path.as_posix()}
        try:
            resolved = path.resolve(strict=False)
            variants.update((str(resolved), resolved.as_posix()))
        except (OSError, RuntimeError):
            pass
        for variant in sorted(variants, key=len, reverse=True):
            if variant:
                message = message.replace(variant, "<chemin local masqué>")

    message = _WINDOWS_PATH.sub("<chemin local masqué>", message)
    message = _UNC_PATH.sub("<chemin local masqué>", message)
    return _POSIX_PATH.sub("<chemin local masqué>", message)


def safe_call(function, *args, paths=(), **kwargs):
    """Call an I/O boundary and preserve exception type with sanitized text."""
    try:
        return function(*args, **kwargs)
    except Exception as error:
        message = safe_exception_message(error, paths=paths)
        if message == str(error):
            raise
        try:
            sanitized_error = type(error)(message)
        except Exception:
            sanitized_error = RuntimeError(
                f"{type(error).__name__}: {message}"
            )
        raise sanitized_error from None
