"""Downloads with resume, progress, notifications and a wake lock.

Backends, chosen automatically (all optional except the last):

    aria2c   plain files, best speed, multi-connection, resumable
    ffmpeg   HLS (.m3u8) / DASH -> remuxed mp4, also used to fix up streams
    yt-dlp   embed pages that need a real extractor
    urllib   always available fallback (pure python, resumable)

Everything targets Termux first: `termux-wake-lock` keeps Android from
freezing a long download, and progress can be pushed to a notification.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import log, termux
from .config import download_dir as configured_download_dir
from .config import get_config
from .models import DownloadLink, Episode, Stream, Title, slugify
from .net import Client, HttpError, client as default_client, host_of
from .paths import which

ProgressFn = Callable[[dict], None]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def safe_name(text: str, fallback: str = "file", maxlen: int = 90) -> str:
    text = (text or "").strip()
    text = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" .")
    if len(text) > maxlen:
        text = text[:maxlen].rsplit(" ", 1)[0]
    return text or fallback


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def human_time(seconds: float) -> str:
    if seconds <= 0:
        return "--:--"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def episode_filename(title: Title, episode: Episode, quality: str = "", ext: str = "mp4",
                     *, season: int | None = None) -> str:
    show = safe_name(title.title, "show")
    season_no = season if season is not None else (episode.season or (1 if title.kind != "movie" else 0))
    number = episode.number
    if title.kind == "movie" or (episode.number == 1 and len(title.episodes) <= 1 and title.kind == "movie"):
        base = show
    else:
        ep_tag = f"E{int(number):02d}" if float(number).is_integer() else f"E{number:g}"
        if season_no:
            ep_tag = f"S{int(season_no):02d}{ep_tag}"
        clean_ep = safe_name(re.sub(rf"^Episode\s*{int(number)}\s*[-–:]?\s*", "", episode.title), "", maxlen=60)
        base = f"{show} - {ep_tag}"
        if clean_ep:
            base += f" - {clean_ep}"
    if quality:
        base += f" [{quality}]"
    return f"{base}.{ext}"


def target_path(title: Title, episode: Episode, *, quality: str = "", ext: str = "mp4",
                directory: str | None = None) -> Path:
    root = Path(directory or configured_download_dir())
    show_dir = root / safe_name(title.title, "show")
    return show_dir / episode_filename(title, episode, quality, ext)


def find_existing(title: Title, episode: Episode, *, directory: str | None = None) -> Path | None:
    """Return an already downloaded file for this episode, if any."""
    root = Path(directory or configured_download_dir()) / safe_name(title.title, "show")
    if not root.is_dir():
        return None
    stem_prefix = episode_filename(title, episode, "", "mp4").rsplit(".", 1)[0]
    try:
        for f in sorted(root.iterdir()):
            if f.suffix.lower() not in (".mp4", ".mkv", ".webm", ".m4v", ".ts", ".avi"):
                continue
            if f.name.startswith(stem_prefix):
                return f
    except OSError:
        return None
    return None


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------

def has_aria2() -> bool:
    return which("aria2c") is not None


def has_ffmpeg() -> bool:
    return which("ffmpeg") is not None


def has_ytdlp() -> bool:
    return which("yt-dlp", "youtube-dl") is not None


def _ffmpeg_headers(headers: dict[str, str]) -> list[str]:
    blob = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items() if v)
    return ["-headers", blob] if blob else ["-headers", "Referer: https://www.google.com/\r\n"]


@dataclass
class DownloadResult:
    ok: bool
    path: Path | None = None
    size: int = 0
    backend: str = ""
    error: str = ""

    def __bool__(self) -> bool:  # pragma: no cover
        return self.ok


def _report(progress: ProgressFn | None, **fields) -> None:
    if progress:
        try:
            progress(fields)
        except Exception:  # noqa: BLE001
            pass


def _run_streaming(cmd: Sequence[str], *, on_line: Callable[[str], None] | None = None,
                   timeout: float | None = None) -> tuple[int, str]:
    """Run a command, feeding stdout lines to on_line. Returns (rc, tail)."""
    tail: list[str] = []
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return 127, str(exc)
    started = time.time()
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            if len(tail) > 40:
                tail.pop(0)
            if on_line:
                on_line(line)
            if timeout and time.time() - started > timeout:
                proc.kill()
                return 124, "timeout"
        proc.wait(timeout=30)
    except KeyboardInterrupt:
        proc.kill()
        raise
    return proc.returncode or 0, "\n".join(tail[-8:])


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------

def download_stream(
    stream: Stream,
    dest: Path,
    *,
    http: Client | None = None,
    progress: ProgressFn | None = None,
    quality: str | None = None,
    force: bool = False,
    extra_headers: dict[str, str] | None = None,
    quiet: bool = False,
) -> DownloadResult:
    """Download one stream to `dest`, picking the best available backend."""
    cfg = get_config()
    dest = Path(dest)
    if stream.kind not in ("mp4", "hls", "dash", "webm", "mkv", "other", ""):
        return DownloadResult(False, dest, 0, "urllib",
                              error=f"not a downloadable stream ({stream.kind or 'unknown'})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    http = http or default_client()

    if dest.exists() and not force:
        return DownloadResult(True, dest, dest.stat().st_size, "cached")

    headers = dict(stream.headers or {})
    if stream.referer:
        headers.setdefault("Referer", stream.referer)
    headers.update(extra_headers or {})

    tmp = dest.with_suffix(dest.suffix + ".part")

    # --- HLS / DASH -> ffmpeg -------------------------------------------------
    if stream.kind in ("hls", "dash") and cfg.get_bool("use_ffmpeg", True) and has_ffmpeg():
        result = _ffmpeg_download(stream, dest, headers, progress=progress, quiet=quiet)
        if result.ok:
            return result
        log.debug("ffmpeg failed (%s), trying other backends", result.error)

    # --- HLS with yt-dlp (handles trickier manifests) --------------------------
    if stream.kind in ("hls", "dash") and cfg.get_bool("yt_dlp_fallback", True) and has_ytdlp():
        result = _ytdlp_download(stream, dest, headers, progress=progress, quiet=quiet)
        if result.ok:
            return result

    # --- plain files -----------------------------------------------------------
    if stream.kind in ("mp4", "webm", "mkv", "other") or not (has_ffmpeg() or has_ytdlp()):
        if cfg.get_bool("use_aria2", True) and has_aria2():
            result = _aria2_download(stream, dest, headers, progress=progress, quiet=quiet)
            if result.ok:
                return result
            log.debug("aria2c failed: %s", result.error)
        return _urllib_download(stream, dest, http=http, headers=headers, progress=progress)

    # --- last resort -----------------------------------------------------------
    if has_ytdlp():
        result = _ytdlp_download(stream, dest, headers, progress=progress, quiet=quiet)
        if result.ok:
            return result
    return DownloadResult(False, None, 0, "none",
                          f"nothing could download {stream.kind} stream ({stream.url[:60]})")


def _aria2_download(stream: Stream, dest: Path, headers: dict[str, str],
                    *, progress: ProgressFn | None = None, quiet: bool = False) -> DownloadResult:
    exe = which("aria2c")
    if not exe:
        return DownloadResult(False, error="aria2c not installed")
    tmp = dest.with_suffix(dest.suffix + ".part")
    cmd = [
        exe,
        "--continue=true",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        "--file-allocation=none",
        "--auto-file-renaming=false",
        "--allow-overwrite=false",
        "--summary-interval=1",
        "--console-log-level=warn",
        "--download-result=hide",
        f"--dir={dest.parent}",
        f"--out={tmp.name}",
        "--check-certificate=false",
    ]
    for key, value in headers.items():
        if value:
            cmd.append(f"--header={key}: {value}")
    if progress:
        cmd.append("--show-console-readout=true")
    cmd.append(stream.url)

    pattern = re.compile(r"\((\d+)%\)")

    def on_line(line: str) -> None:
        m = pattern.search(line)
        if m:
            _report(progress, downloaded=int(m.group(1)), total=100, percent=int(m.group(1)), backend="aria2")

    rc, tail = _run_streaming(cmd, on_line=on_line)
    if rc == 0 and tmp.exists():
        tmp.replace(dest)
        return DownloadResult(True, dest, dest.stat().st_size, "aria2")
    return DownloadResult(False, None, 0, "aria2", tail or f"aria2c exit {rc}")


def _urllib_download(stream: Stream, dest: Path, *, http: Client, headers: dict[str, str],
                     progress: ProgressFn | None = None) -> DownloadResult:
    tmp = dest.with_suffix(dest.suffix + ".part")
    last: dict[str, float] = {"t": 0.0}

    def on_progress(done: int, total: int) -> None:
        now = time.time()
        if progress and (now - last["t"] > 0.3 or done == total):
            last["t"] = now
            percent = int(done / total * 100) if total else 0
            _report(progress, downloaded=done, total=total, percent=percent, backend="urllib")

    try:
        old = http.headers
        http.headers = {**old, **{k: v for k, v in headers.items() if v}}
        http.download(stream.url, dest, referer=stream.referer, progress=on_progress)
        http.headers = old
    except HttpError as exc:
        return DownloadResult(False, None, 0, "urllib", str(exc))
    return DownloadResult(True, dest, dest.stat().st_size if dest.exists() else 0, "urllib")


def _ffmpeg_download(stream: Stream, dest: Path, headers: dict[str, str],
                     *, progress: ProgressFn | None = None, quiet: bool = False) -> DownloadResult:
    exe = which("ffmpeg")
    if not exe:
        return DownloadResult(False, error="ffmpeg not installed")
    cfg = get_config()
    url = stream.url

    # pick the best variant of an HLS master playlist
    if ".m3u8" in url.lower():
        try:
            from .resolve import parse_manifest

            variants = parse_manifest(url)
            if variants and len(variants) > 1:
                def key(v: dict) -> tuple:
                    return (v.get("bandwidth") or 0, v.get("resolution") or "")

                best = max(variants, key=key)
                if best.get("url"):
                    log.debug("hls master: using variant %s", best.get("resolution") or best.get("bandwidth"))
                    url = str(best["url"])
        except Exception as exc:  # noqa: BLE001
            log.debug("manifest parse failed: %s", exc)

    tmp = dest.with_suffix(dest.suffix + ".part.mp4")
    cmd = [
        exe, "-hide_banner", "-loglevel", "error", "-nostdin",
        *_ffmpeg_headers(headers),
        "-user_agent", headers.get("User-Agent", "Mozilla/5.0"),
        "-i", url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-y", str(tmp),
    ]
    if cfg.get_int("concurrent_fragments", 16) > 1:
        cmd.insert(1, "-threads")
        cmd.insert(2, str(cfg.get_int("concurrent_fragments", 16)))

    duration = _probe_duration(url, headers)
    total_us = duration * 1_000_000 if duration else 0

    def on_line(line: str) -> None:
        if not progress:
            return
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                value = int(line.split("=", 1)[1])
            except ValueError:
                return
            seconds = value / (1_000_000 if line.startswith("out_time_us") else 1_000)
            percent = int(seconds / duration * 100) if duration else 0
            _report(progress, downloaded=int(seconds), total=int(duration or 0),
                    percent=min(99, percent), backend="ffmpeg")

    rc, tail = _run_streaming(cmd, on_line=on_line)
    if rc == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(dest)
        return DownloadResult(True, dest, dest.stat().st_size, "ffmpeg")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    return DownloadResult(False, None, 0, "ffmpeg", tail or f"ffmpeg exit {rc}")


def _probe_duration(url: str, headers: dict[str, str]) -> float:
    exe = which("ffprobe")
    if not exe:
        return 0.0
    cmd = [exe, "-v", "error", "-show_entries", "format=duration", "-of",
           "default=noprint_wrappers=1:nokey=1", *_ffmpeg_headers(headers), url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=25, check=False).stdout.strip()
        return float(out)
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0.0


def _ytdlp_download(stream: Stream, dest: Path, headers: dict[str, str],
                    *, progress: ProgressFn | None = None, quiet: bool = False) -> DownloadResult:
    exe = which("yt-dlp", "youtube-dl")
    if not exe:
        return DownloadResult(False, error="yt-dlp not installed")
    tmp_dir = dest.parent / f".{dest.stem}.ytdl"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        "--no-warnings",
        "--no-playlist",
        "--newline",
        "-o", str(tmp_dir / "%(title).80s.%(ext)s"),
        "--merge-output-format", "mp4",
        "-f", "bv*+ba/b",
    ]
    if stream.referer:
        cmd += ["--referer", stream.referer]
    for key, value in headers.items():
        if value.lower() in ("referer", "cookie"):
            cmd += ["--add-header", f"{key}:{value}"]
    cmd.append(stream.url)

    pattern = re.compile(r"\[download\]\s+([\d.]+)%")

    def on_line(line: str) -> None:
        m = pattern.search(line)
        if m:
            _report(progress, percent=float(m.group(1)), backend="yt-dlp")

    rc, tail = _run_streaming(cmd, on_line=on_line)
    if rc == 0:
        produced = sorted(tmp_dir.glob("*"), key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
        if produced:
            final = produced[0]
            target = dest if final.suffix.lower() == dest.suffix.lower() else dest.with_suffix(final.suffix)
            shutil.move(str(final), str(target))
            for leftover in tmp_dir.glob("*"):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            try:
                tmp_dir.rmdir()
            except OSError:
                pass
            return DownloadResult(True, target, target.stat().st_size, "yt-dlp")
    return DownloadResult(False, None, 0, "yt-dlp", tail or f"yt-dlp exit {rc}")


# --------------------------------------------------------------------------
# subtitle sidecars
# --------------------------------------------------------------------------

def download_subtitles(subs: Iterable, dest_base: Path, *, http: Client | None = None,
                       languages: Sequence[str] | None = None) -> list[Path]:
    """Save subtitle tracks next to the video as .vtt/.srt sidecars."""
    http = http or default_client()
    languages = [l.lower() for l in (languages or [])]
    written: list[Path] = []
    for sub in subs or []:
        url = getattr(sub, "url", "") or (sub.get("url") if isinstance(sub, dict) else "")
        if not url:
            continue
        lang = (getattr(sub, "lang", "") or (sub.get("lang") if isinstance(sub, dict) else "") or "").lower()
        label = (getattr(sub, "label", "") or (sub.get("label") if isinstance(sub, dict) else "") or "")
        if languages and lang and lang[:2] not in languages:
            continue
        suffix = ".vtt" if ".vtt" in url.lower() else ".srt"
        extra = f".{lang}" if lang else (f".{label[:8]}" if label else "")
        path = dest_base.with_suffix(dest_base.suffix + extra + suffix)
        try:
            resp = http.get(url, cache=False)
        except HttpError as exc:
            log.debug("subtitle download failed %s: %s", url, exc)
            continue
        text = resp.text
        if suffix == ".srt" or "WEBVTT" not in text[:200]:
            from .subtitles import to_srt

            text = to_srt(text, fmt=_detect_sub_format(text))
            if suffix == ".vtt":
                path = dest_base.with_suffix(dest_base.suffix + extra + ".srt")
        try:
            path.write_text(text, "utf-8")
            written.append(path)
        except OSError as exc:
            log.debug("could not write subtitle %s: %s", path, exc)
    return written


def _detect_sub_format(text: str) -> str:
    head = text.lstrip()[:400]
    if head.startswith("WEBVTT"):
        return "vtt"
    if "[Script Info]" in head or "Dialogue:" in head:
        return "ass"
    return "srt"


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------

@dataclass
class QueueItem:
    title: Title
    episode: Episode
    stream: Stream | None = None
    quality: str = ""
    directory: str = ""
    subtitles: list = field(default_factory=list)
    id: int = 0
    status: str = "queued"
    error: str = ""

    @property
    def label(self) -> str:
        return f"{self.title.title} - {self.episode.label}"


def run_queue(items: Sequence[QueueItem], *, workers: int = 2, notify: bool | None = None,
              progress: ProgressFn | None = None, force: bool = False) -> list[DownloadResult]:
    """Download a batch, with a wake lock + notifications around it."""
    cfg = get_config()
    if notify is None:
        notify = cfg.get_bool("notifications", True)
    if cfg.get_bool("wake_lock", True) and items:
        termux.wake_lock()

    from .db import get_db
    from .net import parallel
    from concurrent.futures import ThreadPoolExecutor, as_completed

    db = get_db()
    results: list[DownloadResult] = []
    total = len(items)
    done = 0

    def job(item: QueueItem) -> DownloadResult:
        nonlocal done
        if item.id:
            db.download_update(item.id, status="downloading")
        stream = item.stream
        if stream is None:
            return DownloadResult(False, None, 0, "", "no stream selected")
        dest = target_path(item.title, item.episode, quality=item.quality or stream.quality,
                           ext=("mp4" if stream.kind in ("hls", "dash", "mp4", "other") else stream.kind),
                           directory=item.directory or None)
        last_report = {"t": 0.0}

        def on_progress(fields: dict) -> None:
            percent = fields.get("percent")
            if item.id and percent is not None:
                now = time.time()
                if now - last_report["t"] > 1.0:
                    last_report["t"] = now
                    db.download_update(item.id, status="downloading")
                    if notify and percent:
                        termux.notify_progress(
                            f"Downloading {item.title.title}",
                            f"{item.episode.label} - {percent:.0f}%",
                            int(percent),
                        )
            if progress:
                progress({"item": item, **fields})

        result = download_stream(stream, dest, progress=on_progress, force=force)
        if result.ok and result.path and item.subtitles:
            download_subtitles(item.subtitles, result.path, languages=cfg.get_list("subtitles"))
        if item.id:
            db.download_update(
                item.id,
                status="done" if result.ok else "failed",
                path=str(result.path or ""),
                size=result.size,
                error=result.error[:300],
            )
        if notify:
            if result.ok:
                termux.notify(
                    "aninext: download finished",
                    f"{item.label} ({human_size(result.size)})",
                    id_="aninext-dl",
                )
            else:
                termux.notify("aninext: download failed", f"{item.label}: {result.error[:120]}", id_="aninext-dl")
        return result

    workers = max(1, min(int(workers or 1), max(1, total)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(job, item): item for item in items}
        for fut in as_completed(futures):
            done += 1
            item = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.error("download failed for %s: %s", item.label, exc)
                result = DownloadResult(False, None, 0, "", str(exc))
            results.append(result)
            if notify:
                termux.notify_progress("aninext downloads", f"{done}/{total} complete",
                                       int(done / total * 100) if total else 100)

    if notify and total:
        termux.notify_remove("aninext-progress")
    if cfg.get_bool("wake_lock", True) and items:
        termux.wake_unlock()
    return results


def prepare_item(title: Title, episode: Episode, stream: Stream, *, quality: str = "",
                 directory: str = "", subtitles: Sequence = ()) -> QueueItem:
    """Create (and record) a queue entry for an episode download."""
    from .db import get_db

    dest = target_path(title, episode, quality=quality or stream.quality, directory=directory or None)
    db = get_db()
    dl_id = db.download_add(
        title_key=title.key,
        title=title.title,
        season=episode.season,
        episode=episode.number,
        quality=quality or stream.quality,
        url=stream.url,
        path=str(dest),
        provider=title.provider or stream.provider,
    )
    return QueueItem(title=title, episode=episode, stream=stream, quality=quality or stream.quality,
                     directory=directory, subtitles=list(subtitles), id=dl_id)
