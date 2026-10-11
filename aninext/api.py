"""Programmatic API - the same engine the CLI uses.

    from aninext import api

    api.search("frieren", limit=5)
    api.info("demo:frieren")
    api.episodes("frieren")
    api.streams("frieren", episode=1)
    api.download("frieren", episodes="1-3")

Everything returns plain dataclasses, so no JSON wrangling is needed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

from . import __version__, aggregate, log
from .config import get_config
from .models import DownloadLink, Episode, SearchResult, Stream, Subtitle, Title
from .providers import ProviderError, load_builtin

__all__ = [
    "search",
    "trending",
    "info",
    "episodes",
    "streams",
    "downloads",
    "subtitles",
    "resolve_embed",
    "download",
    "config",
    "version",
]


def _boot() -> None:
    load_builtin()


def version() -> str:
    return __version__


def config(**overrides: Any):
    cfg = get_config()
    for key, value in overrides.items():
        cfg.set(key, value)
    return cfg


def search(
    query: str,
    *,
    limit: int = 24,
    kind: str | None = None,
    year: str | None = None,
    genre: str | None = None,
    providers: Sequence[str] | None = None,
    sort: str | None = None,
) -> list[SearchResult]:
    """Search every enabled provider and return merged results."""
    _boot()
    return aggregate.search(query, limit=limit, kind=kind, year=year, genre=genre,
                            providers=providers, sort=sort)


def trending(*, kind: str | None = None, limit: int = 20,
             providers: Sequence[str] | None = None) -> list[SearchResult]:
    _boot()
    return aggregate.trending(kind=kind, limit=limit, providers=providers)


def info(reference: str, *, providers: Sequence[str] | None = None, kind: str | None = None) -> Title:
    """Load full details (including episodes) for a title reference."""
    _boot()
    title, provider = aggregate.resolve_title(reference, providers=providers, kind=kind)
    aggregate.load_episodes(title, provider)
    return title


def episodes(reference: str, *, providers: Sequence[str] | None = None,
             kind: str | None = None) -> list[Episode]:
    return info(reference, providers=providers, kind=kind).episodes


def streams(
    reference: str,
    *,
    episode: float | None = None,
    latest: bool = False,
    quality: str | None = None,
    fallback: bool = True,
    providers: Sequence[str] | None = None,
    kind: str | None = None,
) -> list[Stream]:
    """Direct playable streams for one episode, ranked best-first."""
    _boot()
    title, provider = aggregate.resolve_title(reference, providers=providers, kind=kind)
    aggregate.load_episodes(title, provider)
    target = aggregate.pick_episode(title, episode, latest=latest)
    found = aggregate.get_streams(title, target, provider, allow_fallback=fallback)
    if quality:
        ordered = sorted(found, key=lambda s: (s.quality == quality, s.rank, s.playable), reverse=True)
        return ordered
    return found


def downloads(reference: str, *, episode: float | None = None,
              providers: Sequence[str] | None = None) -> list[DownloadLink]:
    _boot()
    title, provider = aggregate.resolve_title(reference, providers=providers)
    aggregate.load_episodes(title, provider)
    target = aggregate.pick_episode(title, episode)
    return aggregate.get_downloads(title, target, provider)


def subtitles(reference: str, *, episode: float | None = None,
              providers: Sequence[str] | None = None) -> list[Subtitle]:
    _boot()
    title, provider = aggregate.resolve_title(reference, providers=providers)
    aggregate.load_episodes(title, provider)
    target = aggregate.pick_episode(title, episode)
    return aggregate.get_subtitles(title, target, provider)


def resolve_embed(url: str, *, referer: str = "", patterns: Iterable[str] = (),
                  depth: int = 2) -> list[Stream]:
    """Turn an embed/player page into direct media streams."""
    from .resolve import resolve_embed as _resolve

    return _resolve(url, referer=referer, patterns=list(patterns), depth=depth)


def download(
    reference: str,
    *,
    episode: float | None = None,
    episodes: str | Iterable[float] | None = None,
    quality: str | None = None,
    directory: str | Path | None = None,
    jobs: int | None = None,
    providers: Sequence[str] | None = None,
    force: bool = False,
) -> list[Path]:
    """Download episodes and return the written file paths."""
    from . import download as dl

    _boot()
    title, provider = aggregate.resolve_title(reference, providers=providers)
    aggregate.load_episodes(title, provider)

    if episodes is None or episodes == "":
        numbers = [aggregate.pick_episode(title, episode).number]
    elif isinstance(episodes, str):
        numbers = _expand_spec(episodes, title)
    else:
        numbers = [float(n) for n in episodes]

    wanted = set(numbers)
    queue = []
    for ep in title.sorted_episodes():
        if ep.number not in wanted:
            continue
        found = aggregate.get_streams(title, ep, provider)
        if not found:
            continue
        chosen = dl_stream = found[0]
        if quality:
            for candidate in found:
                if candidate.quality == quality:
                    chosen = candidate
                    break
        subs = aggregate.get_subtitles(title, ep, provider)
        queue.append(dl.prepare_item(title, ep, chosen, quality=quality or "",
                                     directory=str(directory or ""), subtitles=subs))

    results = dl.run_queue(queue, workers=jobs or get_config().get_int("download_workers", 2),
                           force=force)
    return [r.path for r in results if r.ok and r.path]


def _expand_spec(spec: str, title: Title) -> list[float]:
    import re

    out: list[float] = []
    for chunk in str(spec).replace(" ", "").split(","):
        if not chunk:
            continue
        if chunk in ("all", "*"):
            return [e.number for e in title.sorted_episodes()]
        m = re.match(r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$", chunk)
        if m:
            start, end = float(m.group(1)), float(m.group(2))
            step = 1.0 if end >= start else -1.0
            value = start
            while (step > 0 and value <= end + 1e-9) or (step < 0 and value >= end - 1e-9):
                out.append(value)
                value += step
        else:
            try:
                out.append(float(chunk))
            except ValueError:
                continue
    return out
