"""Cross-provider search, resolution and fallback logic.

`aninext` speaks to several providers at once and hides the differences:

    search "frieren"            -> merged, de-duplicated, ranked results
    watch frieren --episode 3   -> best result -> details -> episode -> stream

A "reference" accepted by `resolve_title()` can be any of:

    frieren                              free text (searched across providers)
    anilist:154587                       provider:id
    archive:night_of_the_living_dead     provider:url-fragment
    https://archive.org/details/xyz      full URL (host routed to its provider)
    demo:frieren
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Iterable, Sequence

from . import log
from .models import DownloadLink, Episode, SearchResult, Stream, Subtitle, Title, slugify
from .net import parallel
from .providers import MediaProvider, ProviderError, SearchQuery, all_providers, available, get, require
from .providers.base import dedupe_results

_HOST_MAP = {
    "archive.org": "archive",
    "www.archive.org": "archive",
    "anilist.co": "anilist",
    "themoviedb.org": "tmdb",
    "www.themoviedb.org": "tmdb",
    "demo.aninext.local": "demo",
}


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

def search(
    text: str = "",
    *,
    providers: Sequence[str] | None = None,
    kind: str | None = None,
    year: str | None = None,
    genre: str | None = None,
    limit: int = 24,
    page: int = 1,
    sort: str | None = None,
    workers: int = 4,
) -> list[SearchResult]:
    """Search every enabled provider in parallel and merge the results."""
    pool = _select_providers(providers, kind=kind)
    if not pool:
        raise ProviderError(
            "no providers available - enable one with `aninext config set provider <id>`"
        )

    query = SearchQuery(text=text, kind=kind, year=year, genre=genre, limit=limit, page=page, sort=sort)
    per_provider = max(4, limit)

    def job(provider: MediaProvider) -> list[SearchResult]:
        try:
            results = provider.search(
                SearchQuery(text=text, kind=kind, year=year, genre=genre,
                            limit=per_provider, page=page, sort=sort)
            )
        except ProviderError as exc:
            log.debug("%s search failed: %s", provider.id, exc)
            return []
        except Exception as exc:  # noqa: BLE001
            log.warn("%s search error: %s", provider.id, exc)
            return []
        for r in results:
            r.provider = r.provider or provider.id
        return results

    groups = parallel(job, pool, workers=max(1, workers)) if len(pool) > 1 else [job(pool[0])]

    # interleave by provider priority so the best source shows up first
    merged: list[SearchResult] = []
    ranked = sorted(
        zip(pool, groups),
        key=lambda pair: -pair[0].priority,
    )
    for _provider, results in ranked:
        merged.extend(results or [])

    deduped = _dedupe_merged(merged)
    if sort in ("score", "rating"):
        deduped.sort(key=lambda r: float(r.score or 0), reverse=True)
    elif sort == "newest":
        deduped.sort(key=lambda r: r.year or "0", reverse=True)
    return deduped[: max(1, limit)]


def _dedupe_merged(results: list[SearchResult]) -> list[SearchResult]:
    """Prefer the richest of two results describing the same show."""
    best: dict[str, SearchResult] = {}
    order: list[str] = []
    for r in results:
        key = slugify(r.title)
        if not key:
            continue
        current = best.get(key)
        if current is None:
            best[key] = r
            order.append(key)
            continue
        if _richness(r) > _richness(current):
            best[key] = r
    return [best[k] for k in order]


def _richness(r: SearchResult) -> int:
    score = 0
    for attr, weight in (("poster", 3), ("synopsis", 2), ("year", 1), ("score", 1), ("episodes", 1)):
        value = getattr(r, attr, None)
        if value:
            score += weight
    try:
        from .providers import available

        provider = get(r.provider)
        if provider and provider.priority:
            score += provider.priority / 100.0
    except Exception:  # noqa: BLE001
        pass
    return int(score * 10)


def _select_providers(ids: Sequence[str] | None, *, kind: str | None = None) -> list[MediaProvider]:
    from . import db

    if ids:
        out = []
        for pid in ids:
            provider = get(pid)
            if provider is None:
                raise ProviderError(f"unknown provider: {pid}")
            if not provider.is_enabled():
                raise ProviderError(f"provider {pid} is disabled")
            out.append(provider)
        return out
    pool = available(kind=kind)
    if not pool:
        pool = available()
    return pool


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def score_match(query: str, title: Title | SearchResult) -> float:
    """0..1 similarity, with a nudge for year/episode data."""
    if not query:
        return 0.0
    q = _normalise(query)
    candidates = [_normalise(title.title)]
    if isinstance(title, Title):
        candidates.extend(_normalise(a) for a in title.alt_titles)
    best = 0.0
    for cand in candidates:
        if not cand:
            continue
        if cand == q:
            return 1.0
        best = max(best, difflib.SequenceMatcher(None, q, cand).ratio())
        if q in cand:
            best = max(best, 0.92 - 0.01 * abs(len(cand) - len(q)))
        elif cand in q:
            best = max(best, 0.88)
    return round(best, 4)


def _normalise(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.sub(r"\b(sub|dub|eng|hd|1080p|720p|480p|full|movie|series|anime)\b", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def best_match(results: Iterable[SearchResult], query: str) -> SearchResult | None:
    pool = list(results)
    if not pool:
        return None
    if not query:
        return pool[0]
    scored = sorted(pool, key=lambda r: score_match(query, r), reverse=True)
    return scored[0]


# --------------------------------------------------------------------------
# resolving a reference into a Title
# --------------------------------------------------------------------------

def resolve_title(
    reference: str,
    *,
    providers: Sequence[str] | None = None,
    kind: str | None = None,
    refresh: bool = False,
) -> tuple[Title, MediaProvider]:
    """Turn a user reference into a fully loaded Title plus its provider."""
    reference = (reference or "").strip()
    if not reference:
        raise ProviderError("nothing to resolve (empty reference)")

    provider_id, ident = _split_reference(reference)
    if provider_id:
        provider = require(provider_id)
        title = provider.details(ident)
        title.provider = title.provider or provider_id
        return title, provider

    if reference.startswith(("http://", "https://")):
        pid = _host_provider(reference)
        if pid:
            provider = require(pid)
            title = provider.details(reference)
            title.provider = title.provider or pid
            return title, provider
        # unknown host: try rule providers that declare it, else search by text
        for provider in _select_providers(providers):
            base = getattr(provider, "base", "")
            if base and base in reference:
                try:
                    return provider.details(reference), provider
                except ProviderError:
                    continue
        reference = reference.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")

    results = search(reference, providers=providers, kind=kind, limit=12)
    if not results:
        raise ProviderError(f"no results for {reference!r}")
    match = best_match(results, reference) or results[0]
    confidence = score_match(reference, match)
    if confidence < 0.34:
        log.warn("weak match (%.2f) for %r -> %s", confidence, reference, match.title)
    provider = require(match.provider)
    title = provider.details(match.url or match.id)
    title.provider = title.provider or match.provider
    title.slug = title.slug or slugify(title.title)
    title.raw["match"] = {"query": reference, "confidence": confidence}
    return title, provider


def _split_reference(reference: str) -> tuple[str | None, str]:
    """'provider:rest' -> (provider, rest)."""
    if re.match(r"^[a-z][a-z0-9_]*:", reference):
        head, _, rest = reference.partition(":")
        if head in {p.id for p in all_providers()} and rest:
            return head, rest
    return None, reference


def _host_provider(url: str) -> str | None:
    from .net import host_of

    host = host_of(url)
    if host in _HOST_MAP:
        return _HOST_MAP[host]
    for variant, pid in _HOST_MAP.items():
        if host.endswith(variant):
            return pid
    return None


# --------------------------------------------------------------------------
# episodes / streams / downloads
# --------------------------------------------------------------------------

def load_episodes(title: Title, provider: MediaProvider, *, force: bool = False) -> list[Episode]:
    if title.episodes and not force:
        return title.episodes
    try:
        episodes = provider.episodes(title.url or title.id)
    except ProviderError as exc:
        log.debug("episode listing failed for %s: %s", title.title, exc)
        return list(title.episodes)
    if episodes:
        title.episodes = episodes
    return list(title.episodes)


def pick_episode(title: Title, episode: float | None = None, *, latest: bool = False) -> Episode:
    episodes = title.sorted_episodes()
    if not episodes:
        raise ProviderError(f"no episodes found for {title.title!r}")
    if latest:
        return episodes[-1]
    if episode is None:
        from .db import get_db

        nxt = None
        try:
            nxt = get_db().next_unwatched(title.key)
        except Exception:  # noqa: BLE001
            nxt = None
        if title.kind == "movie" or len(episodes) == 1:
            return episodes[0]
        target = nxt if nxt is not None else episodes[0].number
        for ep in episodes:
            if abs(ep.number - float(target)) < 1e-6:
                return ep
        return episodes[0]
    for ep in episodes:
        if abs(ep.number - float(episode)) < 1e-6:
            return ep
    raise ProviderError(
        f"episode {episode:g} not found in {title.title!r} "
        f"(have {episodes[0].number:g}-{episodes[-1].number:g})"
    )


def get_streams(
    title: Title,
    episode: Episode,
    provider: MediaProvider,
    *,
    allow_fallback: bool = True,
    kind: str | None = None,
) -> list[Stream]:
    """Streams for one episode, optionally falling back to other providers."""
    streams = _safe(lambda: provider.streams(episode, title=title), provider, "streams")
    if any(s.playable for s in streams):
        return _rank(streams)

    if not allow_fallback:
        return _rank(streams)

    from .resolve import upgrade_embeds, resolve_with_ytdlp, yt_dlp_available

    # 1. embed/player pages: unpack them ourselves first
    embeds = [s for s in streams if s.kind == "embed" or not s.playable]
    if embeds:
        pairs = [(s.label or s.server or "embed", s.url) for s in embeds if s.url]
        resolved = _safe(
            lambda: upgrade_embeds(pairs, provider=provider.id,
                                   referer=title.url or episode.url),
            provider, "resolve",
        )
        if resolved and any(s.playable for s in resolved):
            keep = [s for s in resolved if s.playable]
            return _rank(keep)

    # 2. last resort for hosts only yt-dlp understands
    if embeds and yt_dlp_available():
        for embed in embeds:
            extra = _safe(lambda e=embed: resolve_with_ytdlp(e.url, referer=title.url), provider, "yt-dlp")
            if extra and any(s.playable for s in extra):
                return _rank(extra)

    # 3. other providers that claim to have the same show
    candidates = _fallback_providers(provider, kind=kind or title.kind)
    if not candidates:
        return _rank(streams)
    log.debug("trying %d fallback providers for %s", len(candidates), title.title)
    for other in candidates:
        try:
            found = other.search(SearchQuery(text=title.title, limit=5, kind=kind or title.kind))
        except Exception as exc:  # noqa: BLE001
            log.debug("fallback search failed on %s: %s", other.id, exc)
            continue
        if not found:
            continue
        match = best_match(found, title.title)
        if match is None or score_match(title.title, match) < 0.6:
            continue
        try:
            other_title = other.details(match.url or match.id)
        except ProviderError:
            continue
        target = None
        for ep in other_title.episodes:
            if abs(ep.number - episode.number) < 1e-6:
                target = ep
                break
        if target is None and other_title.kind == "movie":
            target = other_title.episodes[0] if other_title.episodes else None
        if target is None:
            continue
        streams = _safe(lambda t=target, ot=other_title, o=other: o.streams(t, title=ot), other, "streams")
        if streams:
            log.info("streams found on fallback provider %s", other.id)
            return _rank(streams)
    return _rank(streams)


def _rank(streams: list[Stream]) -> list[Stream]:
    from .config import get_config

    preferred = str(get_config().get("quality") or "")

    def key(s: Stream) -> tuple:
        return (
            1 if s.playable else 0,
            s.rank,
            1 if (preferred and s.quality == preferred) else 0,
            -len(s.url),
        )

    return sorted(streams, key=key, reverse=True)


def _fallback_providers(exclude: MediaProvider, *, kind: str | None = None) -> list[MediaProvider]:
    pool = [p for p in available(kind=kind) if p.id != exclude.id and not p.meta_only]
    pool.sort(key=lambda p: -p.priority)
    return pool


def get_downloads(title: Title, episode: Episode, provider: MediaProvider) -> list[DownloadLink]:
    links = _safe(lambda: provider.downloads(episode, title=title), provider, "downloads") or []
    if links:
        return sorted(links, key=lambda d: d.rank, reverse=True)
    return [
        DownloadLink(url=s.url, quality=s.quality, size=s.size, server=s.server, kind=s.kind,
                     headers=dict(s.headers), referer=s.referer)
        for s in get_streams(title, episode, provider, allow_fallback=False)
        if s.playable
    ]


def get_subtitles(title: Title, episode: Episode, provider: MediaProvider) -> list[Subtitle]:
    subs = _safe(lambda: provider.subtitles(episode, title=title), provider, "subtitles") or []
    if subs:
        return subs
    for stream in episode.streams:
        if stream.subtitles:
            subs.extend(stream.subtitles)
    return subs


def _safe(fn, provider: MediaProvider, what: str):
    try:
        return fn()
    except ProviderError as exc:
        log.debug("%s %s failed: %s", provider.id, what, exc)
    except Exception as exc:  # noqa: BLE001
        log.warn("%s %s error: %s", provider.id, what, exc)
    return None


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def trending(*, kind: str | None = None, limit: int = 20, providers: Sequence[str] | None = None) -> list[SearchResult]:
    pool = _select_providers(providers, kind=kind)
    def job(provider: MediaProvider) -> list[SearchResult]:
        return _safe(lambda: provider.trending(kind, limit), provider, "trending") or []

    groups = parallel(job, pool, workers=min(4, len(pool))) if len(pool) > 1 else [job(pool[0])]
    merged = [r for group in groups for r in (group or [])]
    return _dedupe_merged(merged)[:limit]


def schedule(*, days: int = 7, limit: int = 40, providers: Sequence[str] | None = None) -> list[dict]:
    pool = [p for p in _select_providers(providers) if hasattr(p, "schedule")]
    rows: list[dict] = []
    for provider in pool:
        rows.extend(_safe(lambda p=provider: p.schedule(days=days, limit=limit), provider, "schedule") or [])
    rows.sort(key=lambda r: r.get("time") or "")
    return rows[:limit]


def season(*, year: int, season_name: str, limit: int = 24) -> list[SearchResult]:
    from .providers import require

    provider = require("anilist")
    return _safe(lambda: provider.season(year, season_name, limit=limit), provider, "season") or []
