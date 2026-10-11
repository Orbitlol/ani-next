"""Tiny logging helper with optional colour.

Colour is disabled automatically when stdout is not a tty (pipes, log files)
or when NO_COLOR is set, and can be forced with ANINEXT_COLOR=1.
"""
from __future__ import annotations

import os
import sys
import threading
import time

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "quiet": 100}

_CODES = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "grey": "\033[90m",
}

_lock = threading.Lock()
_level = _LEVELS.get(os.environ.get("ANINEXT_LOG", "info"), 20)


def _colour_enabled() -> bool:
    if os.environ.get("ANINEXT_COLOR") == "1":
        return True
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stderr.isatty()


def set_level(name: str) -> None:
    global _level
    _level = _LEVELS.get(str(name).lower(), 20)


def get_level() -> int:
    return _level


def colour(text: str, *styles: str) -> str:
    if not _colour_enabled():
        return text
    prefix = "".join(_CODES.get(s, "") for s in styles)
    return f"{prefix}{text}{_CODES['reset']}" if prefix else text


def _emit(level: str, msg: str, styles: tuple[str, ...]) -> None:
    if _LEVELS[level] < _level:
        return
    stamp = time.strftime("%H:%M:%S")
    head = colour(f"{stamp} {level.upper():<5}", "grey") if level != "info" else colour(f"{stamp} INFO ", "grey")
    with _lock:
        print(f"{head} {colour(msg, *styles)}", file=sys.stderr, flush=True)


def debug(msg: str, *a) -> None:
    _emit("debug", msg % a if a else msg, ("dim",))


def info(msg: str, *a) -> None:
    _emit("info", msg % a if a else msg, ())


def ok(msg: str, *a) -> None:
    _emit("info", "\u2713 " + (msg % a if a else msg), ("green",))


def warn(msg: str, *a) -> None:
    _emit("warn", "\u26a0 " + (msg % a if a else msg), ("yellow",))


def error(msg: str, *a) -> None:
    _emit("error", "\u2717 " + (msg % a if a else msg), ("red",))


def plain(msg: str) -> None:
    """Print straight to stdout, bypassing level filtering."""
    with _lock:
        print(msg, flush=True)
