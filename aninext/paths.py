"""Filesystem layout, Termux aware.

Everything lives under a single root so the whole app can be moved or wiped:

    ~/.aninext/                 (or $ANINEXT_HOME)
        config.json
        aninext.db              library / history / cache
        sources/                user defined scraper rules (JSON)
        cache/                  http cache
        logs/
        downloads/              only when no shared storage is found
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

APP = "aninext"


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if raw:
        return Path(raw).expanduser()
    return None


def is_termux() -> bool:
    """Best-effort Termux/Android detection."""
    if os.environ.get("TERMUX_VERSION"):
        return True
    if "com.termux" in os.environ.get("PREFIX", ""):
        return True
    if Path("/data/data/com.termux/files/usr").exists():
        return True
    return bool(os.environ.get("ANDROID_ROOT") and os.environ.get("ANDROID_DATA"))


def termux_prefix() -> Path:
    return Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))


def home() -> Path:
    return _env_path("ANINEXT_HOME") or (Path.home() / f".{APP}")


def config_file() -> Path:
    return home() / "config.json"


def db_file() -> Path:
    return home() / "aninext.db"


def cache_dir() -> Path:
    return home() / "cache"


def log_dir() -> Path:
    return home() / "logs"


def sources_dir() -> Path:
    """Directory holding user supplied scraper rule files."""
    return home() / "sources"


def bundled_sources_dir() -> Path:
    return Path(__file__).resolve().parent / "sources.d"


def ensure_dirs() -> None:
    for d in (home(), cache_dir(), log_dir(), sources_dir()):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# downloads
# --------------------------------------------------------------------------

_SHARED_CANDIDATES = (
    "~/storage/downloads",
    "~/storage/shared/Download",
    "~/storage/shared/Downloads",
    "/sdcard/Download",
    "/sdcard/Downloads",
    "/storage/emulated/0/Download",
)


def shared_storage() -> Path | None:
    """Return Termux shared-storage root if `termux-setup-storage` was run."""
    for cand in _SHARED_CANDIDATES:
        p = Path(cand).expanduser()
        try:
            if p.is_dir() and os.access(p, os.W_OK):
                return p
        except OSError:
            continue
    return None


def default_download_dir() -> Path:
    override = _env_path("ANINEXT_DOWNLOAD_DIR")
    if override:
        return override
    shared = shared_storage()
    if shared is not None:
        # keep our own folder so the gallery/media scanner can find it
        return shared / "aninext"
    return home() / "downloads"


# --------------------------------------------------------------------------
# external tools
# --------------------------------------------------------------------------

def which(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        # Termux binaries sometimes miss the shim; look in $PREFIX/bin
        cand = termux_prefix() / "bin" / name
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def tool_report() -> dict[str, str | None]:
    return {
        "ffmpeg": which("ffmpeg"),
        "ffprobe": which("ffprobe"),
        "aria2c": which("aria2c"),
        "yt-dlp": which("yt-dlp", "youtube-dl"),
        "curl": which("curl"),
        "termux-open": which("termux-open"),
        "termux-notification": which("termux-notification"),
        "termux-wake-lock": which("termux-wake-lock"),
        "mpv": which("mpv"),
        "vlc": which("vlc"),
    }
