"""Internet Archive provider.

archive.org hosts a huge, *legal* collection of public-domain films, classic
animation, old TV and Creative-Commons material.  It has clean JSON APIs, so
this is a real, working scraper rather than a demo:

    search    https://archive.org/advancedsearch.php   (JSON)
    metadata  https://archive.org/metadata/<id>        (JSON)
    files     https://archive.org/download/<id>/<file>

kinds: movie, anime (classic/pd animation), series, documentary
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .. import log
from ..models import DownloadLink, Episode, SearchResult, Stream, Subtitle, Title
from ..net import add_query
from .base import MediaProvider, ProviderError, SearchQuery, parse_size, quality_from_text

ADVANCED_SEARCH = "https://archive.org/advancedsearch.php"
METADATA = "https://archive.org/metadata/{identifier}"
DOWNLOAD = "https://archive.org/download/{identifier}/{name}"
EMBED = "https://archive.org/embed/{identifier}"

VIDEO_EXT = (".mp4", ".m4v", ".webm", ".ogv", ".mkv", ".avi", ".mpeg", ".mpg", ".mov")
SUB_EXT = (".srt", ".vtt", ".ass", ".ssa")


class ArchiveProvider(MediaProvider):
    id = "archive"
    name = "Internet Archive"
    kinds = ("movie", "anime", "series", "documentary")
    priority = 60
    note = "Public-domain and Creative Commons films, animation and TV."
    home = "https://archive.org"

    # -- search ------------------------------------------------------------

    def search(self, query: SearchQuery) -> list[SearchResult]:
        limit = max(1, min(int(query.limit or 24), 100))
        parts = [f"mediatype:(movies)"]
        if query.text:
            safe = re.sub(r'[^\w\s\-:\'"().]', " ", query.text).strip()
            if safe:
                parts.append(f"({safe})")
        if query.year:
            parts.append(f"year:{query.year}")
        if query.extra.get("collection"):
            parts.append(f"collection:{query.extra['collection']}")
        if query.kind == "anime":
            parts.append("(subject:animation OR subject:anime OR subject:cartoon)")
        elif query.kind == "documentary":
            parts.append("(subject:documentary)")
        if query.genre:
            parts.append(f"subject:{query.genre}")

        params: list[tuple[str, str]] = [("q", " AND ".join(parts))]
        for field in ("identifier", "title", "year", "description", "creator", "mediatype",
                      "downloads", "subject", "runtime", "licenseurl"):
            params.append(("fl[]", field))
        for sort in ("downloads", "title", "year"):
            params.append(("sort[]", sort))
        params.append(("rows", str(limit)))
        params.append(("page", str(max(1, int(query.page or 1)))))
        params.append(("output", "json"))

        url = add_query(ADVANCED_SEARCH, {}) + "?" + "&".join(
            f"{k}={_q(v)}" for k, v in params
        )
        data = self.get_json(url, cache=True, cache_ttl=1800)
        docs = (((data or {}).get("response") or {}).get("docs")) or []
        results: list[SearchResult] = []
        for doc in docs:
            identifier = doc.get("identifier")
            if not identifier:
                continue
            results.append(
                SearchResult(
                    title=_first(doc.get("title")) or identifier,
                    url=identifier,
                    provider=self.id,
                    id=identifier,
                    poster=f"https://archive.org/services/img/{identifier}",
                    year=str(_first(doc.get("year")) or "")[:4],
                    kind=self._kind_for(doc),
                    score="",
                    synopsis=_clean_description(_first(doc.get("description"))),
                )
            )
        return results

    def _kind_for(self, doc: dict[str, Any]) -> str:
        subjects = _as_list(doc.get("subject"))
        blob = " ".join(str(s).lower() for s in subjects)
        if "anime" in blob or "animation" in blob or "cartoon" in blob:
            return "anime"
        if "documentary" in blob:
            return "documentary"
        if "television" in blob or "tv" in blob:
            return "series"
        return "movie"

    # -- details -----------------------------------------------------------

    def details(self, url_or_id: str) -> Title:
        identifier = self._identifier(self.clean_ref(url_or_id))
        data = self.get_json(METADATA.format(identifier=identifier), cache=True, cache_ttl=3600)
        if not data or not data.get("metadata"):
            raise ProviderError(f"archive.org item not found: {identifier}")

        meta = data.get("metadata") or {}
        files = data.get("files") or []
        title = Title(
            title=_first(meta.get("title")) or identifier,
            url=identifier,
            kind=self._kind_for({"subject": meta.get("subject")}),
            provider=self.id,
            id=identifier,
            poster=f"https://archive.org/services/img/{identifier}",
            year=str(_first(meta.get("year")) or _first(meta.get("date")) or "")[:4],
            synopsis=_clean_description(_first(meta.get("description"))),
            genres=[str(s) for s in _as_list(meta.get("subject"))][:12],
            studios=[str(c) for c in _as_list(meta.get("creator"))][:4],
            country=_first(meta.get("country")) or "",
            rating=_first(meta.get("licenseurl")) or "",
            alt_titles=[str(a) for a in _as_list(meta.get("alternative_title"))][:5],
        )

        videos = [f for f in files if _is_video(f)]
        videos.sort(key=lambda f: _natural_key(str(f.get("name", ""))))
        subtitles = [f for f in files if _is_subtitle(f)]

        for index, f in enumerate(videos, start=1):
            name = str(f.get("name") or "")
            length = _parse_runtime(f.get("length")) or _parse_runtime(f.get("runtime"))
            number = _episode_number(name, index)
            ep = Episode(
                number=number,
                title=_episode_title(name) or f"Part {index}",
                url=identifier,
                duration=_format_duration(length),
                provider=self.id,
                id=f"{identifier}/{name}",
                thumbnail=f"https://archive.org/services/img/{identifier}",
            )
            for sub in subtitles:
                sub_name = str(sub.get("name") or "")
                base = re.sub(r"\.[a-z0-9]+$", "", name.lower())
                if sub_name.lower().startswith(base[:12]) or base.startswith(
                    re.sub(r"\.[a-z0-9]+$", "", sub_name.lower())[:12]
                ):
                    ep.subtitles.append(
                        Subtitle(
                            label=_episode_title(sub_name) or "Subtitle",
                            url=DOWNLOAD.format(identifier=identifier, name=sub_name),
                            lang=_lang_from_name(sub_name),
                        )
                    )
            ep.streams = [self._stream_for(identifier, f)]
            title.episodes.append(ep)

        if not title.episodes:
            raise ProviderError(f"no video files in archive.org item {identifier}")
        title.total_episodes = len(title.episodes)
        title.type_ = "Movie" if len(title.episodes) == 1 else "TV"
        if len(title.episodes) == 1:
            title.kind = title.kind if title.kind in ("anime", "documentary") else "movie"
        title.raw["files"] = [
            {"name": f.get("name"), "size": f.get("size"), "format": f.get("format")}
            for f in videos
        ]
        return title

    # -- streams -----------------------------------------------------------

    def _stream_for(self, identifier: str, f: dict[str, Any]) -> Stream:
        name = str(f.get("name") or "")
        url = DOWNLOAD.format(identifier=identifier, name=name)
        fmt = str(f.get("format") or "")
        kind = "mp4"
        low = name.lower()
        if ".m3u8" in low:
            kind = "hls"
        elif low.endswith(".webm"):
            kind = "webm"
        elif low.endswith(".ogv"):
            kind = "other"
        elif low.endswith(".mkv"):
            kind = "mkv"
        quality = quality_from_text(fmt) or quality_from_text(name) or ("1080p" if "h.264" in fmt.lower() else "")
        headers = {"Referer": "https://archive.org/"}
        return Stream(
            url=url,
            quality=quality,
            kind=kind,
            server="archive.org",
            label=fmt or name.rsplit(".", 1)[-1].upper(),
            headers=headers,
            referer="https://archive.org/",
            size=parse_size(f.get("size") or ""),
            provider=self.id,
            extra={"format": fmt, "name": name},
        )

    def streams(self, episode: Episode, *, title: Title | None = None) -> list[Stream]:
        if episode.streams:
            return episode.streams
        identifier = self._identifier(episode.url)
        data = self.get_json(METADATA.format(identifier=identifier), cache=True, cache_ttl=3600)
        files = [f for f in (data or {}).get("files", []) if _is_video(f)]
        name = (episode.id or "").split("/", 1)[-1]
        for f in files:
            if str(f.get("name")) == name:
                return [self._stream_for(identifier, f)]
        return []

    def downloads(self, episode: Episode, *, title: Title | None = None) -> list[DownloadLink]:
        out: list[DownloadLink] = []
        for s in self.streams(episode, title=title):
            out.append(
                DownloadLink(
                    url=s.url,
                    quality=s.quality,
                    size=s.size,
                    server="archive.org",
                    kind=s.kind,
                    headers=dict(s.headers),
                    referer=s.referer,
                )
            )
        return out

    # -- discovery ---------------------------------------------------------

    def trending(self, kind: str | None = None, limit: int = 20) -> list[SearchResult]:
        return self.search(SearchQuery(kind=kind or "anime", limit=limit, sort="popular"))

    def schedule(self, **kw) -> list[dict]:
        return []

    def collections(self) -> list[dict[str, str]]:
        """A few hand-picked, genuinely useful public-domain collections."""
        return [
            {"id": "animationandcartoons", "name": "Animation & Cartoons"},
            {"id": "classic_cartoons", "name": "Classic Cartoons"},
            {"id": "feature_films", "name": "Feature Films"},
            {"id": "film_noir", "name": "Film Noir"},
            {"id": "scifi_horror", "name": "Sci-Fi / Horror"},
            {"id": "classic_tv", "name": "Classic TV"},
            {"id": "anime", "name": "Anime (public domain scans)"},
        ]

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _identifier(url_or_id: str) -> str:
        value = (url_or_id or "").strip()
        if value.lower().startswith("archive:"):
            value = value.split(":", 1)[1].strip()
        m = re.search(r"archive\.org/(?:details|download|embed|metadata|services/img)/([^/?#]+)", value)
        if m:
            return m.group(1)
        return value.rstrip("/")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _q(value: str) -> str:
    import urllib.parse

    return urllib.parse.quote(str(value), safe="")


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return "" if value is None else str(value)


def _as_list(value: Any) -> list[Any]:
    """archive.org returns subject/creator as a list, a string, or "a;b"."""
    if value is None:
        return []
    if isinstance(value, list):
        out: list[Any] = []
        for item in value:
            out.extend(_as_list(item))
        return out
    text = str(value).strip()
    if not text:
        return []
    if ";" in text:
        return [part.strip() for part in text.split(";") if part.strip()]
    return [text]


def _clean_description(text: str) -> str:
    if not text:
        return ""
    from ..net import strip_html

    text = strip_html(str(text))
    return text[:1500]


def _is_video(f: dict[str, Any]) -> bool:
    name = str(f.get("name") or "").lower()
    if not name.endswith(VIDEO_EXT):
        return False
    if name.endswith("_thumb.mp4"):  # generated thumbnails
        return False
    return True


def _is_subtitle(f: dict[str, Any]) -> bool:
    name = str(f.get("name") or "").lower()
    return name.endswith(SUB_EXT)


def _parse_runtime(value: Any) -> float:
    """'1225.5' or '1:02:05' or '20:31' -> seconds."""
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    seconds = 0.0
    for num in nums:
        seconds = seconds * 60 + num
    return seconds


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return ""
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _natural_key(text: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


def _episode_number(name: str, index: int) -> float:
    stem = re.sub(r"\.[a-z0-9]+$", "", name)
    patterns = (
        r"(?:ep|episode|e)[\s._-]*(\d{1,3})\b",
        r"(\d{1,3})\s*of\s*\d+",
        r"\bpart[\s._-]*(\d{1,3})\b",
        r"^\D*(\d{1,3})\D*$",
    )
    for pat in patterns:
        m = re.search(pat, stem, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    numbers = re.findall(r"\d{1,3}", stem)
    if len(numbers) == 1:
        return float(numbers[0])
    return float(index)


def _episode_title(name: str) -> str:
    stem = re.sub(r"\.[a-z0-9]+$", "", name)
    stem = re.sub(r"[_\s]+", " ", stem)
    stem = re.sub(r"^\s*\d{1,3}[\s.\-–]+", "", stem)
    stem = re.sub(r"\b(1080p|720p|480p|x264|h\.?264|aac|mp4|webm)\b", "", stem, flags=re.I)
    return re.sub(r"\s{2,}", " ", stem).strip(" -–_")


def _lang_from_name(name: str) -> str:
    low = name.lower()
    for code, names in (
        ("en", ("eng", "english", "en.")),
        ("es", ("spa", "spanish", "es.")),
        ("fr", ("fre", "french", "fra")),
        ("de", ("ger", "german", "deu")),
        ("pt", ("por", "portuguese")),
        ("ar", ("ara", "arabic")),
        ("ja", ("jpn", "japanese")),
    ):
        if any(n in low for n in names):
            return code
    return ""


def build() -> ArchiveProvider:
    return ArchiveProvider()
