"""Termux integration: wake locks, notifications, clipboard, opening files.

Every function degrades to a no-op when the helper `termux-*` commands are not
installed (i.e. on desktop Linux, in CI, or on a bare Termux without
termux-api), so callers never need to guard.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import log
from .paths import is_termux, which


def available(tool: str) -> bool:
    return which(tool) is not None


def _run(cmd: list[str], timeout: float = 8.0, detach: bool = False) -> bool:
    exe = shutil.which(cmd[0]) or cmd[0]
    try:
        if detach:
            subprocess.Popen(
                [exe, *cmd[1:]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        proc = subprocess.run(
            [exe, *cmd[1:]],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------
# wake lock
# --------------------------------------------------------------------------

_wake_held = False

_api_missing_warned = False


def _warn_api_missing(tool: str) -> None:
    global _api_missing_warned
    if _api_missing_warned:
        return
    _api_missing_warned = True
    if is_termux():
        log.warn(
            "`%s` not found - install the Termux:API app and run "
            "`pkg install termux-api` for notifications/wake-lock.",
            tool,
        )


def wake_lock() -> None:
    """Prevent Android from killing long downloads when the screen is off."""
    global _wake_held
    if _wake_held:
        return
    if available("termux-wake-lock"):
        if _run(["termux-wake-lock"]):
            _wake_held = True
            log.debug("termux wake lock acquired")
    elif is_termux():
        _warn_api_missing("termux-wake-lock")


def wake_unlock() -> None:
    global _wake_held
    if not _wake_held:
        return
    _run(["termux-wake-unlock"])
    _wake_held = False
    log.debug("termux wake lock released")


def acquire_wake_lock_if_available() -> bool:  # convenience alias
    wake_lock()
    return _wake_held


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

def notify(title: str, message: str = "", *, id_: str | None = None, ongoing: bool = False) -> None:
    if not available("termux-notification"):
        if is_termux():
            _warn_api_missing("termux-notification")
        return
    cmd = ["termux-notification", "--title", title]
    if message:
        cmd += ["--content", message[:900]]
    if id_:
        cmd += ["--id", id_]
    if ongoing:
        cmd += ["--ongoing"]
    _run(cmd, detach=True)


def notify_progress(title: str, message: str, percent: int, id_: str = "aninext-progress") -> None:
    """Update (or create) a progress notification."""
    if not available("termux-notification"):
        return
    percent = max(1, min(99, int(percent)))
    cmd = [
        "termux-notification",
        "--title",
        title,
        "--content",
        message[:900],
        "--id",
        id_,
        "--ongoing",
        "--progress-max",
        "100",
        "--progress",
        str(percent),
    ]
    _run(cmd, detach=True)


def notify_remove(id_: str) -> None:
    if available("termux-notification-remove"):
        _run(["termux-notification-remove", id_], detach=True)


def vibrate(ms: int = 60) -> None:
    if available("termux-vibrate"):
        _run(["termux-vibrate", "-d", str(ms)], detach=True)


def toast(message: str) -> None:
    if available("termux-toast"):
        _run(["termux-toast", "-g", "bottom", message[:200]], detach=True)


# --------------------------------------------------------------------------
# clipboard / sharing
# --------------------------------------------------------------------------

def clipboard_set(text: str) -> bool:
    if available("termux-clipboard-set"):
        return _run(["termux-clipboard-set", text])
    if sys.platform == "darwin" and available("pbcopy"):
        return _run(["pbcopy"], timeout=5)
    return False


def clipboard_get() -> str | None:
    if available("termux-clipboard-get"):
        exe = shutil.which("termux-clipboard-get") or "termux-clipboard-get"
        try:
            out = subprocess.run([exe], capture_output=True, text=True, timeout=5, check=False)
            if out.returncode == 0:
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
    return None


# --------------------------------------------------------------------------
# intents: open / share / call
# --------------------------------------------------------------------------

def open_path(path: str | Path, mime: str | None = None) -> bool:
    """Open a file with the Android intent system (falls back to xdg-open)."""
    path = str(path)
    if available("termux-open"):
        cmd = ["termux-open", path]
        if mime:
            cmd += ["--content-type", mime]
        return _run(cmd, detach=True)
    if os.name == "posix" and available("xdg-open"):
        return _run(["xdg-open", path], detach=True)
    return False


def open_url(url: str) -> bool:
    if available("termux-open-url"):
        return _run(["termux-open-url", url], detach=True)
    if available("termux-open"):
        return _run(["termux-open", url], detach=True)
    if available("xdg-open"):
        return _run(["xdg-open", url], detach=True)
    return False


def share(path: str | Path) -> bool:
    if available("termux-share"):
        return _run(["termux-share", "-a", "send", str(path)], detach=True)
    return False


def call(uri: str) -> bool:
    """Fire an arbitrary Android intent, e.g. `android.intent.action.VIEW`."""
    if available("termux-open-url"):
        return _run(["termux-open-url", uri], detach=True)
    return False


# --------------------------------------------------------------------------
# player integration
# --------------------------------------------------------------------------

def play(path_or_url: str, player: str | None = None) -> bool:
    """Play media with mpv/vlc if present, otherwise hand it to Android."""
    from .paths import which as _which

    player = player or os.environ.get("ANINEXT_PLAYER")
    if not player:
        if _which("mpv"):
            player = "mpv"
        elif _which("vlc"):
            player = "vlc"
    if player:
        exe = shutil.which(player) or player
        cmd = [exe, path_or_url]
        if "mpv" in player and not is_termux():
            cmd.append("--force-window=yes")
        try:
            return subprocess.run(cmd, check=False).returncode == 0
        except OSError:
            pass
    return open_path(path_or_url)
