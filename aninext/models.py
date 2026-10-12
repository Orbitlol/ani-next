"""Data models shared across providers, the CLI and the API."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

KINDS = ("anime", "movie", "series", "tv", "cartoon", "donghua")

_QUALITY_ORDER = {"4k": 5, "2160p": 5, "1440p": 4, "1080p": 3, "720p": 2, "480p": 1, "360p": 0, "": 0}


def norm_quality(q: str) -> str:
    q = (q or "").lower().strip()
    for tag in ("2160p", "1440p", "1080p", "720p", "480p", "360p", "4k"):
        if tag in q:
            return "4k" if tag == "2160p" else tag
    return q


def quality_rank(q: str) -> int:
    return _QUALITY_ORDER.get(norm_quality(q), 0)


def quality_from_url(url: str) -> str:
    """Infer a quality tag from a media URL ('.../master.m3u8' -> 'auto')."""
    low = (url or "").lower()
    for tag in ("2160p", "1440p", "1080p", "720p", "480p", "360p", "4k"):
        if tag in low:
            return norm_quality(tag)
    if "master.m3u8" in low or "/hls/" in low:
        return "auto"
    return ""


def slugify(text: str, fallback: str = "untitled") -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^\w\s\-.]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text).strip("-.")
    text = re.sub(r"-{2,}", "-", text)
    return text[:90] or fallback


def _clean(text: Any) -> str:
    if text is None:
        return ""
    text = str(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@dataclass
class Subtitle:
    label: str = ""
    url: str = ""
    lang: str = ""
    kind: str = "subtitle"  # subtitle | dub

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Stream:
    """A playable video source (direct file, HLS/DASH manifest or embed page)."""

    url: str
    quality: str = ""
    label: str = ""
    kind: str = "hls"  # hls | dash | mp4 | embed | iframe | torrent | other
    mime: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    referer: str = ""
    subtitles: list[Subtitle] = field(default_factory=list)
    size: int = 0
    server: str = ""
    language: str = ""
    provider: str = ""
    drm: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.quality = norm_quality(self.quality) or norm_quality(self.label) or quality_from_url(self.url)
        if not self.kind or self.kind == "other":
            self.kind = guess_kind(self.url)

    @property
    def rank(self) -> int:
        return quality_rank(self.quality)

    @property
    def playable(self) -> bool:
        return self.kind in ("mp4", "hls", "dash", "webm", "mkv") and not self.drm

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind
        return d

    def __str__(self) -> str:  # pragma: no cover
        return f"[{self.quality or '?':>6}] {self.kind:<6} {self.url[:80]}"


def guess_kind(url: str) -> str:
    u = (url or "").lower()
    if ".m3u8" in u:
        return "hls"
    if ".mpd" in u:
        return "dash"
    if ".mp4" in u or ".m4v" in u:
        return "mp4"
    if ".webm" in u:
        return "webm"
    if ".mkv" in u:
        return "mkv"
    if u.startswith("magnet:"):
        return "torrent"
    if any(k in u for k in ("embed", "player", "iframe", "/e/")) or "/v/" in u:
        return "embed"
    return "other"


@dataclass
class DownloadLink:
    url: str
    quality: str = ""
    size: int = 0
    server: str = ""
    kind: str = "file"
    headers: dict[str, str] = field(default_factory=dict)
    referer: str = ""

    @property
    def rank(self) -> int:
        return quality_rank(self.quality)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Episode:
    number: float = 0
    title: str = ""
    url: str = ""
    thumbnail: str = ""
    duration: str = ""
    synopsis: str = ""
    aired: str = ""
    season: int = 0
    filler: bool = False
    recap: bool = False
    audio: list[str] = field(default_factory=list)  # e.g. ["sub","dub"]
    streams: list[Stream] = field(default_factory=list)
    downloads: list[DownloadLink] = field(default_factory=list)
    subtitles: list[Subtitle] = field(default_factory=list)
    provider: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        self.title = _clean(self.title)
        try:
            self.number = float(self.number or 0)
        except (TypeError, ValueError):
            self.number = 0.0

    @property
    def label(self) -> str:
        n = self.number
        num = str(int(n)) if float(n).is_integer() else f"{n:g}"
        return f"Episode {num}" + (f" - {self.title}" if self.title else "")

    @property
    def best(self) -> Stream | None:
        if not self.streams:
            return None
        playable = [s for s in self.streams if s.playable]
        pool = playable or self.streams
        return max(pool, key=lambda s: (s.rank, s.kind == "mp4"))

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "thumbnail": self.thumbnail,
            "duration": self.duration,
            "synopsis": self.synopsis,
            "aired": self.aired,
            "season": self.season,
            "filler": self.filler,
            "recap": self.recap,
            "audio": self.audio,
            "provider": self.provider,
            "id": self.id,
            "label": self.label,
            "streams": [s.to_dict() for s in self.streams],
            "downloads": [d.to_dict() for d in self.downloads],
            "subtitles": [s.to_dict() for s in self.subtitles],
        }


@dataclass
class Title:
    """A show, movie or series."""

    title: str
    url: str = ""
    kind: str = "anime"
    provider: str = ""
    id: str = ""
    slug: str = ""
    alt_titles: list[str] = field(default_factory=list)
    synopsis: str = ""
    poster: str = ""
    banner: str = ""
    year: str = ""
    status: str = ""          # ongoing | completed | upcoming | airing
    score: str = ""           # "8.7"
    genres: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    country: str = ""
    duration: str = ""
    total_episodes: int = 0
    type_: str = ""           # TV | Movie | OVA | ONA
    rating: str = ""
    episodes: list[Episode] = field(default_factory=list)
    related: list[dict[str, str]] = field(default_factory=list)
    seasons: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = _clean(self.title)
        if not self.slug and self.title:
            self.slug = slugify(self.title)
        self.kind = (self.kind or "anime").lower()
        self.genres = [_clean(g) for g in self.genres if _clean(g)]
        try:
            self.total_episodes = int(self.total_episodes or 0)
        except (TypeError, ValueError):
            self.total_episodes = 0
        if not self.total_episodes and self.episodes:
            self.total_episodes = len(self.episodes)

    @property
    def url_slug(self) -> str:
        """Our slug for the title, derived lazily.

        Providers build a `Title` and only assign the name afterwards, so the
        slug cannot always be computed in `__post_init__`.
        """
        if self.slug and self.slug != "untitled":
            return self.slug
        return slugify(self.title) if self.title else (self.slug or "untitled")

    @property
    def key(self) -> str:
        """Stable identifier used for library/history rows."""
        return self.id or f"{self.provider}:{self.url_slug}"

    def sorted_episodes(self) -> list[Episode]:
        return sorted(self.episodes, key=lambda e: (e.season, e.number))

    def episode(self, number: float) -> Episode | None:
        for e in self.episodes:
            if abs(e.number - float(number)) < 1e-6:
                return e
        return None

    def to_dict(self, *, with_episodes: bool = True) -> dict:
        data = {
            "title": self.title,
            "url": self.url,
            "kind": self.kind,
            "provider": self.provider,
            "id": self.id,
            "slug": self.url_slug,
            "key": self.key,
            "alt_titles": self.alt_titles,
            "synopsis": self.synopsis,
            "poster": self.poster,
            "banner": self.banner,
            "year": self.year,
            "status": self.status,
            "score": self.score,
            "genres": self.genres,
            "studios": self.studios,
            "country": self.country,
            "duration": self.duration,
            "total_episodes": self.total_episodes,
            "type": self.type_,
            "rating": self.rating,
            "related": self.related,
            "seasons": self.seasons,
        }
        if with_episodes:
            data["episodes"] = [e.to_dict() for e in self.episodes]
        return data


@dataclass
class SearchResult:
    title: str
    url: str = ""
    provider: str = ""
    poster: str = ""
    year: str = ""
    kind: str = "anime"
    score: str = ""
    status: str = ""
    episodes: int = 0
    synopsis: str = ""
    id: str = ""

    @property
    def key(self) -> str:
        return self.id or f"{self.provider}:{slugify(self.title)}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d

    def __str__(self) -> str:  # pragma: no cover
        bits = [f"{self.title}"]
        if self.year:
            bits.append(f"({self.year})")
        if self.provider:
            bits.append(f"[{self.provider}]")
        return " ".join(bits)


@dataclass
class HistoryEntry:
    title: str
    episode: float
    season: int = 0
    episode_title: str = ""
    provider: str = ""
    title_key: str = ""
    url: str = ""
    position: float = 0.0
    duration: float = 0.0
    watched_at: float = 0.0

    @property
    def percent(self) -> float:
        if self.duration > 0:
            return min(100.0, self.position / self.duration * 100.0)
        return 0.0


def dedupe_streams(streams: Iterable[Stream]) -> list[Stream]:
    seen: set[tuple[str, str]] = set()
    out: list[Stream] = []
    for s in streams:
        key = (s.url.split("?")[0], s.quality)
        if key in seen or not s.url:
            continue
        seen.add(key)
        out.append(s)
    return out
