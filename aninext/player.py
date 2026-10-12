"""Playback: the mpv-android app / mpv / vlc, otherwise Android intents, xdg-open.

On a phone the nicest player is the mpv-android app, so it comes first:

    aninext frieren                     -> whole flow, plays the pick in mpv-android
    aninext play frieren --episode 3    -> resolve stream, open with mpv
    aninext play frieren -e 3 --open    -> hand the URL to Android (VLC, MX)
    aninext play frieren -e 3 --playlist -> write a .m3u8/.m3u and open that

`--player mpv-android|mpv|vlc|vlc-android|intent` picks explicitly; anything
unavailable is skipped, so the chain degrades instead of failing.  HLS streams
are written to a temporary playlist when the target player cannot take a bare
URL, which makes choosing a player on Android painless.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from . import android, log, termux
from .models import Episode, Stream, Title
from .paths import which

MIME = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "hls": "application/vnd.apple.mpegurl",
    "dash": "application/dash+xml",
}


def players() -> dict[str, str | None]:
    return {
        "mpv-android": android.MPV_ANDROID if android.mpv_android_available() else None,
        "mpv": which("mpv"),
        "vlc": which("vlc"),
        "vlc-android": android.VLC_ANDROID if android.package_installed(android.VLC_ANDROID) else None,
        "am": android.am_binary(),
        "xdg-open": which("xdg-open"),
        "termux-open": which("termux-open"),
    }


def write_playlist(stream: Stream, dest: Path | None = None) -> Path:
    """Write an .m3u8/.m3u playlist for a stream (some players want a file)."""
    if dest is None:
        suffix = ".m3u8" if stream.kind == "hls" else ".m3u"
        handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, prefix="aninext-")
        dest = Path(handle.name)
        handle.close()
    lines = ["#EXTM3U"]
    if stream.kind == "hls":
        lines.append("#EXT-X-VERSION:3")
    lines.append(f"#EXTINF:-1,{stream.label or stream.quality or 'stream'}")
    lines.append(stream.url)
    dest.write_text("\n".join(lines) + "\n", "utf-8")
    return dest


def play_url(
    url: str,
    *,
    kind: str = "",
    referer: str = "",
    headers: dict[str, str] | None = None,
    player: str = "auto",
    title: str = "",
    extra_args: Sequence[str] = (),
    playlist: bool = False,
    open_with: bool = False,
    subs: Sequence[str] = (),
    position_ms: int = 0,
) -> bool:
    """Open a media URL with the best available player."""
    headers = dict(headers or {})
    if referer:
        headers.setdefault("Referer", referer)

    target = url
    if playlist and kind in ("hls", "dash"):
        target = str(write_playlist(Stream(url=url, kind=kind or "hls", referer=referer)))

    chosen = (player or "auto").lower()
    if chosen == "intent":
        open_with = True
        chosen = "auto"

    # the mpv-android app is the nicest option on a phone
    if not open_with and chosen in ("auto", "mpv-android", "android", "app"):
        if android.play_in_mpv_android(target, title=title, subs=list(subs), position_ms=position_ms):
            return True
        if chosen == "mpv-android":
            log.warn("mpv-android is not available (install it, or pass --player mpv)")

    if not open_with:
        if chosen in ("auto", "mpv") and which("mpv"):
            args = [which("mpv") or "mpv", target]
            if referer:
                args.append(f"--http-header-fields=Referer: {referer}")
            if title:
                args.append(f"--force-media-title={title}")
            args += list(extra_args)
            log.debug("launching: %s", " ".join(args))
            try:
                return subprocess.run(args, check=False).returncode == 0
            except OSError as exc:
                log.debug("mpv failed: %s", exc)

        if chosen in ("auto", "vlc") and which("vlc"):
            args = [which("vlc") or "vlc", target]
            if referer:
                args.append(f"--http-referrer={referer}")
            args += list(extra_args)
            try:
                return subprocess.run(args, check=False).returncode in (0, 1)
            except OSError as exc:
                log.debug("vlc failed: %s", exc)

    # explicit Android apps, if the user asked for one
    for name, package in (("vlc-android", android.VLC_ANDROID), ("mx", android.MX_PLAYER)):
        if chosen == name and android.open_in(package, target, title=title):
            log.info("opened in %s", package)
            return True

    mime = MIME.get(kind, "video/mp4")
    if termux.open_path(target, mime):
        return True
    if termux.open_url(target):
        return True
    log.warn(
        "no player found - install mpv (`pkg install mpv`), "
        "the mpv-android app, or termux-api"
    )
    log.plain(target)
    return False


def play_stream(stream: Stream, *, player: str = "auto", title: str = "", playlist: bool = False,
                open_with: bool = False, extra_args: Sequence[str] = (),
                subs: Sequence[str] = (), position_ms: int = 0) -> bool:
    return play_url(
        stream.url,
        kind=stream.kind,
        referer=stream.referer,
        headers=stream.headers,
        player=player,
        title=title or stream.label,
        playlist=playlist,
        open_with=open_with,
        extra_args=extra_args,
        subs=subs,
        position_ms=position_ms,
    )


def resume_position(title_key: str, episode: float, season: int = 0) -> int:
    """Where the user stopped last time, in milliseconds (0 when unknown)."""
    try:
        from .db import get_db

        for row in get_db().history_list(limit=50, title_key=title_key):
            if abs(float(row.get("episode") or 0) - float(episode)) > 1e-6:
                continue
            if int(row.get("season") or 0) != int(season or 0):
                continue
            position = float(row.get("position") or 0)
            duration = float(row.get("duration") or 0)
            if position < 30:
                return 0
            if duration and position > duration - 30:
                return 0
            return int(position * 1000)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not look up resume position: %s", exc)
    return 0


def play_episode(
    title: Title,
    episode: Episode,
    stream: Stream,
    *,
    player: str = "auto",
    playlist: bool = False,
    open_with: bool = False,
    record: bool = True,
    subtitles: Sequence = (),
    resume: bool = True,
    mark_watched: bool = False,
) -> bool:
    """Play one episode, remember it in the history, optionally mark it seen."""
    label = f"{title.title} - {episode.label}"
    log.info("playing %s", label)
    log.info("stream: %s %s", stream.quality or stream.kind, stream.url[:110])
    position_ms = resume_position(title.key, episode.number, episode.season) if resume else 0
    if position_ms:
        log.info("resuming at %s", _clock(position_ms / 1000))
    if record:
        try:
            from .db import get_db
            from .models import HistoryEntry

            db = get_db()
            db.history_add(
                HistoryEntry(
                    title=title.title,
                    episode=episode.number,
                    season=episode.season,
                    episode_title=episode.title,
                    provider=title.provider,
                    title_key=title.key,
                    url=title.url,
                )
            )
            if mark_watched:
                db.episode_mark(title.key, episode.number, season=episode.season, watched=True)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not record history: %s", exc)
    return play_stream(
        stream,
        player=player,
        title=label,
        playlist=playlist,
        open_with=open_with,
        subs=[getattr(s, "url", "") or str(s) for s in subtitles],
        position_ms=position_ms,
    )


def _clock(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def open_in_browser(url: str) -> bool:
    """Open a page (e.g. an official streaming link) in the browser."""
    if termux.open_url(url):
        return True
    if which("xdg-open"):
        try:
            return subprocess.run([which("xdg-open") or "xdg-open", url], check=False).returncode == 0
        except OSError:
            return False
    log.plain(url)
    return False
