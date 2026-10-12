"""Android integration: launch players and detect installed apps.

mpv-android (`is.xyz.mpv`) is the nicest way to watch something on a phone, so
`aninext <name>` hands the stream straight to it when it is installed.  Its
intent API is documented at http://mpv-android.github.io/mpv-android/intent.html :

    am start -a android.intent.action.VIEW -t video/any -p is.xyz.mpv -d URL \\
        --es title "Show - S01E03" --ei position 90000 --esa subs "url1,url2"

Everything here is Android-only and silently unavailable elsewhere: the module
reports "not installed" instead of raising, so the player chain can just move on
to mpv/vlc/xdg-open.

Notes
-----
* `title` and `position` (milliseconds) are honoured by the app; `subs` expects
  a ParcelableArray of Uri, which `am` cannot build, so the string array we pass
  is best-effort - the app ignores it if it cannot cast it.
* The app has no way to set HTTP headers (upstream TODO), so streams that need a
  `Referer` may refuse to play there; pass `--player mpv` for those.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import log

__all__ = [
    "MPV_ANDROID",
    "VLC_ANDROID",
    "am_binary",
    "build_mpv_intent",
    "open_in",
    "package_installed",
    "packages",
    "play_in_mpv_android",
    "mpv_android_available",
]

MPV_ANDROID = "is.xyz.mpv"
VLC_ANDROID = "org.videolan.vlc"
MX_PLAYER = "com.mxtech.videoplayer.ad"

_AM_CANDIDATES = ("termux-am", "am", "/system/bin/am", "/system/xbin/am")
_PM_CANDIDATES = ("pm", "/system/bin/pm", "/system/xbin/pm")

_package_cache: dict[str, bool] = {}
_am_checked = False
_am_path: str | None = None


def _tool(candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if os.path.isabs(name):
            if Path(name).exists() and os.access(name, os.X_OK):
                return name
            continue
        found = shutil.which(name)
        if found:
            return found
    return None


def am_binary() -> str | None:
    """`termux-am` (preferred on Android 10+), then the system `am`."""
    global _am_checked, _am_path
    if not _am_checked:
        _am_checked = True
        _am_path = _tool(_AM_CANDIDATES)
        if not _am_path:
            log.debug("no `am` binary - Android intents are unavailable")
    return _am_path


_children: list[subprocess.Popen] = []


def _spawn(cmd: list[str]) -> subprocess.Popen:
    """Start a detached process and keep a reference so it is not GC'd."""
    global _children
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _children = [p for p in _children if p.poll() is None]
    _children.append(proc)
    return proc


def _run(cmd: list[str], *, timeout: float = 12.0, detach: bool = True) -> bool:
    try:
        if detach:
            _spawn(cmd)
            return True
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=timeout, check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("command failed (%s): %s", cmd[0], exc)
        return False


def packages(pattern: str = "") -> list[str]:
    """Installed package names, e.g. ['is.xyz.mpv'] (empty when unavailable)."""
    pm = _tool(_PM_CANDIDATES)
    if not pm:
        return []
    cmd = [pm, "list", "packages"] + ([pattern] if pattern else [])
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("pm list packages failed: %s", exc)
        return []
    if proc.returncode != 0:
        return []
    return [
        line.split(":", 1)[1].strip()
        for line in proc.stdout.splitlines()
        if line.startswith("package:") and ":" in line
    ]


def package_installed(package: str) -> bool:
    """Is `package` installed?  Cached; overridable with ANINEXT_FAKE_PACKAGES."""
    fake = os.environ.get("ANINEXT_FAKE_PACKAGES")
    if fake is not None:
        return package in {p.strip() for p in fake.split(",") if p.strip()}
    if package not in _package_cache:
        _package_cache[package] = package in packages(package)
    return _package_cache[package]


def mpv_android_available() -> bool:
    """True when the mpv-android app can actually be launched."""
    override = os.environ.get("ANINEXT_MPV_ANDROID", "").strip().lower()
    if override in ("0", "false", "no", "off"):
        return False
    if override in ("1", "true", "yes", "on"):
        return am_binary() is not None
    if not am_binary():
        return False
    return package_installed(MPV_ANDROID)


def build_mpv_intent(
    url: str,
    *,
    title: str = "",
    subs: list[str] | tuple[str, ...] = (),
    position_ms: int = 0,
    mime: str = "video/any",
    package: str = MPV_ANDROID,
) -> list[str]:
    """The argv to hand `url` to mpv-android (pure function, easy to test)."""
    am = am_binary() or "am"
    cmd = [
        am, "start",
        "-a", "android.intent.action.VIEW",
        "-t", mime,
        "-p", package,
        "-d", url,
    ]
    if title:
        cmd += ["--es", "title", title]
    if position_ms and position_ms > 0:
        cmd += ["--ei", "position", str(int(position_ms))]
    clean = [s for s in subs if s and "," not in s]
    if clean:
        cmd += ["--esa", "subs", ",".join(clean)]
    return cmd


def play_in_mpv_android(
    url: str,
    *,
    title: str = "",
    subs: list[str] | tuple[str, ...] = (),
    position_ms: int = 0,
) -> bool:
    """Open `url` in the mpv-android app.  Returns False if that is not possible."""
    if not mpv_android_available():
        return False
    cmd = build_mpv_intent(url, title=title, subs=subs, position_ms=position_ms)
    log.debug("android intent: %s", " ".join(cmd))
    if _run(cmd):
        log.info("opened in mpv-android")
        return True
    log.debug("mpv-android intent failed")
    return False


def open_in(package: str, url: str, *, mime: str = "video/any", title: str = "") -> bool:
    """Hand a URL to any app that can view it (VLC, MX Player, ...)."""
    if not am_binary():
        return False
    cmd = build_mpv_intent(url, title=title, mime=mime, package=package)
    return _run(cmd)
