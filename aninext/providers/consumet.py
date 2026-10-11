"""Optional Consumet-style JSON API provider.

If you run a self-hosted aggregator API (the Consumet API is open source and
easy to host), point aninext at it and every one of its upstream sites becomes
available through a single, stable JSON interface:

    export CONSUMET_API=http://127.0.0.1:3000
    aninext providers
    aninext --provider consumet search "frieren"

Without that variable the provider is registered but disabled, so nothing is
requested behind your back.
"""
from __future__ import annotations

import os
import re
from typing import Any

from .. import log
from ..models import DownloadLink, Episode, SearchResult, Stream, Subtitle, Title
from .base import MediaProvider, ProviderError, SearchQuery, quality_from_text

ANIME_SITES = ("gogoanime", "zoro", "animepahe", "animekai", "hianime")
MOVIE_SITES = ("flixhq", "sflix", "dramacool", "himovies")


class ConsumetProvider(MediaProvider):
    id = "consumet"
    name = "Consumet API"
    kinds = ("anime", "movie", "series")
    priority = 65
    note = "Self-hosted JSON aggregator API (set CONSUMET_API)."
    home = "https://github.com/consumet/api.consumet.org"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.api = (
            os.environ.get("CONSUMET_API")
            or self.config.get("api")
            or ""
        ).rstrip("/")
        self.anime_site = os.environ.get("CONSUMET_ANIME_SITE") or self.config.get("anime_site") or "gogoanime"
        self.movie_site = os.environ.get("CONSUMET_MOVIE_SITE") or self.config.get("movie_site") or "flixhq"

    def is_enabled(self) -> bool:
        return bool(self.api) and super().is_enabled()

    def _get(self, path: str) -> Any:
        if not self.api:
            raise ProviderError("consumet: CONSUMET_API is not set")
        return self.get_json(self.api + path, cache=True, cache_ttl=900)

    # -- interface ---------------------------------------------------------

    def search(self, query: SearchQuery) -> list[SearchResult]:
        kind = (query.kind or "anime").lower()
        if kind in ("movie", "series"):
            site = self.movie_site
            data = self._get(f"/movies/{site}/{query.text}")
            rows = (data or {}).get("results") or []
            out = [
                SearchResult(
                    title=row.get("title") or "?",
                    url=f"{site}:{row.get('id')}",
                    provider=self.id,
                    id=str(row.get("id") or ""),
                    poster=row.get("image") or "",
                    year=str(row.get("releaseDate") or "")[:4],
                    kind="series" if row.get("type", "").lower() == "tv" else "movie",
                    synopsis=row.get("description") or "",
                )
                for row in rows
            ]
            return out[: max(1, query.limit)]

        data = self._get(f"/anime/{self.anime_site}/{query.text}")
        rows = (data or {}).get("results") or []
        out = [
            SearchResult(
                title=row.get("title") or "?",
                url=f"{self.anime_site}:{row.get('id')}",
                provider=self.id,
                id=str(row.get("id") or ""),
                poster=row.get("image") or "",
                year=str(row.get("releaseDate") or "")[:4],
                kind="anime",
                episodes=int(row.get("totalEpisodes") or row.get("episodes") or 0)
                if str(row.get("totalEpisodes") or row.get("episodes") or "").isdigit()
                else 0,
            )
            for row in rows
        ]
        return out[: max(1, query.limit)]

    def details(self, url_or_id: str) -> Title:
        site, ident = self._split(self.clean_ref(url_or_id))
        if site in MOVIE_SITES:
            data = self._get(f"/movies/{site}/info?id={ident}")
        else:
            data = self._get(f"/anime/{site}/info?id={ident}")
        if not data or not data.get("title"):
            raise ProviderError(f"consumet: {url_or_id} not found")

        title = Title(
            title=data.get("title") or "?",
            url=f"{site}:{ident}",
            kind="movie" if site in MOVIE_SITES and data.get("type", "").lower() != "tv" else (
                "series" if site in MOVIE_SITES else "anime"
            ),
            provider=self.id,
            id=str(ident),
            synopsis=(data.get("description") or "").strip(),
            poster=data.get("image") or "",
            banner=data.get("cover") or "",
            year=str(data.get("releaseDate") or "")[:4],
            status=data.get("status") or "",
            score=str(data.get("rating") or ""),
            genres=list(data.get("genres") or []),
            studios=[data.get("studio")] if data.get("studio") else [],
            country=data.get("country") or "",
            duration=f"{data.get('duration')}m" if data.get("duration") else "",
            total_episodes=int(data.get("totalEpisodes") or len(data.get("episodes") or [])),
            type_=data.get("type") or "",
        )
        for row in data.get("episodes") or []:
            number = _number(row.get("number") or 0)
            title.episodes.append(
                Episode(
                    number=number,
                    title=row.get("title") or f"Episode {number:g}",
                    url=row.get("id") or "",
                    thumbnail=data.get("image") or "",
                    synopsis=row.get("description") or "",
                    aired=str(row.get("releaseDate") or ""),
                    provider=self.id,
                    id=str(row.get("id") or ""),
                )
            )
        return title

    def episodes(self, url_or_id: str) -> list[Episode]:
        return self.details(url_or_id).episodes

    def streams(self, episode: Episode, *, title: Title | None = None) -> list[Stream]:
        if not episode.url:
            return []
        site = title.provider_site if hasattr(title, "provider_site") else self._split(title.url)[0] if title and title.url else self.anime_site
        endpoint = "movies" if site in MOVIE_SITES else "anime"
        data = self._get(f"/{endpoint}/{site}/watch?episodeId={_quote(episode.url)}")
        if not data:
            return []
        out: list[Stream] = []
        for src in data.get("sources") or []:
            url = src.get("url") or ""
            if not url:
                continue
            out.append(
                Stream(
                    url=url,
                    quality=quality_from_text(src.get("quality") or "") or (src.get("quality") or ""),
                    kind="hls" if (src.get("isM3U8") or ".m3u8" in url.lower()) else "mp4",
                    server=host_from(url),
                    provider=self.id,
                    headers=dict(data.get("headers") or {}),
                    referer=data.get("referer") or "",
                    extra={"subtitles": len(data.get("subtitles") or [])},
                )
            )
        for sub in data.get("subtitles") or []:
            if not sub.get("url"):
                continue
            episode.subtitles.append(
                Subtitle(label=sub.get("lang") or "Subtitle", lang=(sub.get("lang") or "")[:2].lower(),
                         url=sub["url"])
            )
        episode.streams = out
        return out

    def subtitles(self, episode: Episode, *, title: Title | None = None) -> list[Subtitle]:
        if not episode.subtitles:
            self.streams(episode, title=title)
        return list(episode.subtitles)

    def downloads(self, episode: Episode, *, title: Title | None = None) -> list[DownloadLink]:
        return [
            DownloadLink(url=s.url, quality=s.quality, server=s.server, kind=s.kind,
                         headers=dict(s.headers), referer=s.referer)
            for s in self.streams(episode, title=title)
            if s.kind in ("mp4", "hls")
        ]

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _split(url_or_id: str) -> tuple[str, str]:
        text = str(url_or_id or "").strip()
        if ":" in text and "/" not in text.split(":")[0]:
            site, _, ident = text.partition(":")
            return site, ident
        m = re.search(r"/(?:anime|movies)/([^/]+)/(?:info\?id=)?([^/?#]+)", text)
        if m:
            return m.group(1), m.group(2)
        return ANIME_SITES[0], text

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": "api",
            "kinds": list(self.kinds),
            "base": self.api or "(not configured)",
            "enabled": self.is_enabled(),
            "note": self.note,
        }


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        m = re.search(r"\d+(?:\.\d+)?", str(value or ""))
        return float(m.group(0)) if m else 0.0


def host_from(url: str) -> str:
    from ..net import host_of

    return host_of(url)


def _quote(value: str) -> str:
    import urllib.parse

    return urllib.parse.quote(str(value), safe="")


def build() -> ConsumetProvider | None:
    provider = ConsumetProvider()
    if not provider.api:
        log.debug("consumet: CONSUMET_API not set, provider stays disabled")
    return provider
