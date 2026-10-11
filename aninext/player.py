"""Playback: mpv/vlc when available, otherwise Android intents / xdg-open.

On Termux the usual workflow is:

    aninext play frieren --episode 3        -> resolve stream, open with mpv
    aninext play frieren -e 3 --open        -> hand the URL to Android (VLC, MX)
    aninext play frieren -e 3 --playlist    -> write a .m3u8/.m3u and open it

HLS streams are written to a temporary playlist when the target player cannot
take a bare URL, which makes choosing a player on Android painless.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from . import log, termux
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
        "mpv": which("mpv"),
        "vlc": which("vlc"),
        "am": which("am"),
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

    mime = MIME.get(kind, "video/mp4")
    if termux.open_path(target, mime):
        return True
    if termux.open_url(target):
        return True
    log.warn("no player found - install mpv (`pkg install mpv`) or termux-api")
    log.plain(target)
    return False


def play_stream(stream: Stream, *, player: str = "auto", title: str = "", playlist: bool = False,
                open_with: bool = False, extra_args: Sequence[str] = ()) -> bool:
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
    )


def play_episode(
    title: Title,
    episode: Episode,
    stream: Stream,
    *,
    player: str = "auto",
    playlist: bool = False,
    open_with: bool = False,
    record: bool = True,
) -> bool:
    """Play one episode and (optionally) remember it in the history."""
    label = f"{title.title} - {episode.label}"
    log.info("playing %s", label)
    log.info("stream: %s %s", stream.quality or stream.kind, stream.url[:110])
    if record:
        try:
            from .db import get_db
            from .models import HistoryEntry

            get_db().history_add(
                HistoryEntry(
                    title=title.title,
                    episode=episode.number,
                    episode_title=episode.title,
                    provider=title.provider,
                    title_key=title.key,
                    url=title.url,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("could not record history: %s", exc)
    return play_stream(stream, player=player, title=label, playlist=playlist, open_with=open_with)


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
