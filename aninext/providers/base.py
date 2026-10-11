"""Provider base class and shared helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .. import log
from ..models import DownloadLink, Episode, SearchResult, Stream, Subtitle, Title
from ..net import Client, HttpError, client


class ProviderError(RuntimeError):
    """Raised when a provider cannot fulfil a request."""


class NotFound(ProviderError):
    pass


class Disabled(ProviderError):
    pass


@dataclass
class SearchQuery:
    text: str = ""
    kind: str | None = None       # anime | movie | series
    year: str | None = None
    genre: str | None = None
    status: str | None = None
    limit: int = 24
    page: int = 1
    sort: str | None = None       # relevance | popular | newest | score
    extra: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover
        return self.text or "(filter)"


class MediaProvider:
    """Base class. Subclasses implement search/details/episodes/streams."""

    id: str = "base"
    name: str = "Base"
    kinds: tuple[str, ...] = ("anime", "movie", "series")
    language: str = "en"
    priority: int = 50          # higher wins when merging results
    requires_key: bool = False
    home: str = ""
    note: str = ""
    meta_only: bool = False     # provides metadata, no streams

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._client: Client | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = self.make_client()
        return self._client

    def make_client(self) -> Client:
        base = client()
        return Client(
            user_agent=self.config.get("user_agent", base.user_agent),
            timeout=float(self.config.get("timeout", base.timeout)),
            retries=int(self.config.get("retries", base.retries)),
            delay=float(self.config.get("delay", base.delay)),
            use_cache=bool(self.config.get("use_cache", base.use_cache)),
            cache_ttl=int(self.config.get("cache_ttl", base.cache_ttl)),
            headers=dict(self.config.get("headers", {})),
        )

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def clean_ref(self, value: str) -> str:
        """Drop a leading '<provider>:' prefix so 'demo:frieren' works anywhere."""
        text = str(value or "").strip()
        prefix = f"{self.id}:"
        if text.lower().startswith(prefix):
            return text[len(prefix):]
        return text

    # -- interface ---------------------------------------------------------

    def search(self, query: SearchQuery) -> list[SearchResult]:
        raise NotImplementedError

    def details(self, url_or_id: str) -> Title:
        raise NotImplementedError

    def episodes(self, url_or_id: str) -> list[Episode]:
        title = self.details(url_or_id)
        return title.episodes

    def streams(self, episode: Episode, *, title: Title | None = None) -> list[Stream]:
        if episode.streams:
            return episode.streams
        return []

    def downloads(self, episode: Episode, *, title: Title | None = None) -> list[DownloadLink]:
        return list(episode.downloads)

    def subtitles(self, episode: Episode, *, title: Title | None = None) -> list[Subtitle]:
        return list(episode.subtitles)

    def trending(self, kind: str | None = None, limit: int = 20) -> list[SearchResult]:
        return self.search(SearchQuery(kind=kind, limit=limit, sort="popular"))

    def schedule(self, **kw) -> list[dict]:
        return []

    def season(self, year: int, season: str, **kw) -> list[SearchResult]:
        return []

    # -- helpers available to subclasses -----------------------------------

    def get(self, url: str, **kw):
        try:
            return self.client.get(url, **kw)
        except HttpError as exc:
            raise ProviderError(f"{self.id}: {exc}") from exc

    def get_html(self, url: str, **kw):
        from ..htmldom import parse

        try:
            resp = self.client.get(url, **kw)
        except HttpError as exc:
            raise ProviderError(f"{self.id}: {exc}") from exc
        if resp.status >= 400:
            raise ProviderError(f"{self.id}: HTTP {resp.status} for {url}")
        return parse(resp.text), resp

    def get_json(self, url: str, **kw) -> Any:
        try:
            resp = self.client.get(url, **kw)
        except HttpError as exc:
            raise ProviderError(f"{self.id}: {exc}") from exc
        if resp.status >= 400:
            raise ProviderError(f"{self.id}: HTTP {resp.status} for {url}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.id}: invalid JSON from {url}") from exc

    def post_json(self, url: str, payload: Any, **kw) -> Any:
        try:
            resp = self.client.post(url, json_body=payload, **kw)
        except HttpError as exc:
            raise ProviderError(f"{self.id}: {exc}") from exc
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.id}: invalid JSON from {url}") from exc

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.id}>"


# --------------------------------------------------------------------------
# normalisation helpers shared by providers
# --------------------------------------------------------------------------

_EP_RE = re.compile(r"(?:ep|episode|e)\s*\.?\s*(\d+(?:\.\d+)?)", re.I)
_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_episode_number(text: str, default: float = 0.0) -> float:
    """Best-effort episode number extraction from a label or URL."""
    if not text:
        return default
    text = str(text)
    m = _EP_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return default
    m = _NUM_RE.search(text.replace("-", " ").split("/")[-1] if "/" in text else text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return default
    return default


def parse_number(text: str, default: float = 0.0) -> float:
    m = _NUM_RE.search(str(text or "").replace(",", ""))
    if not m:
        return default
    try:
        return float(m.group(1))
    except ValueError:
        return default


def parse_size(text: str) -> int:
    """'1.4 GB' -> bytes."""
    if not text:
        return 0
    m = re.search(r"([\d.]+)\s*([KMGTP]i?B)", str(text), re.I)
    if not m:
        return int(parse_number(text)) if str(text).strip().isdigit() else 0
    value = float(m.group(1))
    unit = m.group(2).upper()
    factor = {
        "KB": 1 << 10, "KIB": 1 << 10,
        "MB": 1 << 20, "MIB": 1 << 20,
        "GB": 1 << 30, "GIB": 1 << 30,
        "TB": 1 << 40, "TIB": 1 << 40,
        "PB": 1 << 50, "PIB": 1 << 50,
    }.get(unit, 1)
    return int(value * factor)


def quality_from_text(text: str) -> str:
    """Best quality label found in a string, normalised like models.norm_quality."""
    text = (text or "").lower()
    for q in ("2160p", "1440p", "1080p", "720p", "480p", "360p"):
        if q in text:
            return "4k" if q == "2160p" else q
    if "4k" in text:
        return "4k"
    m = re.search(r"(\d{3,4})p", text)
    return f"{m.group(1)}p" if m else ""


def looks_like_video(url: str) -> bool:
    u = (url or "").lower()
    return any(ext in u for ext in (".m3u8", ".mp4", ".mpd", ".mkv", ".webm", ".ts?"))


def make_title_from_result(result: SearchResult, provider_id: str) -> Title:
    return Title(
        title=result.title,
        url=result.url,
        kind=result.kind,
        provider=provider_id,
        poster=result.poster,
        year=result.year,
        score=result.score,
        status=result.status,
        synopsis=result.synopsis,
        total_episodes=result.episodes,
    )


def dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    out: list[SearchResult] = []
    for r in results:
        key = re.sub(r"[^a-z0-9]", "", r.title.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


__all__ = [
    "MediaProvider",
    "ProviderError",
    "NotFound",
    "Disabled",
    "SearchQuery",
    "parse_episode_number",
    "parse_number",
    "parse_size",
    "quality_from_text",
    "looks_like_video",
    "make_title_from_result",
    "dedupe_results",
    "Title",
    "Episode",
    "Stream",
    "Subtitle",
    "DownloadLink",
    "SearchResult",
]
