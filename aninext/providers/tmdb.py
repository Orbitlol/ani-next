"""Optional TMDb metadata provider (movies & series).

Set an API key to enable it - TMDb is free for non-commercial use:

    export TMDB_API_KEY=xxxx
    aninext providers            # tmdb should now be listed

Only metadata is used (posters, synopsis, ratings, seasons/episodes); the
provider never touches video and stays disabled without a key.
"""
from __future__ import annotations

import os
from typing import Any

from .. import log
from ..models import Episode, SearchResult, Title
from .base import MediaProvider, ProviderError, SearchQuery, parse_episode_number

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"
POSTER_SIZE = "w500"
BACKDROP_SIZE = "w1280"


class TmdbProvider(MediaProvider):
    id = "tmdb"
    name = "TMDb"
    kinds = ("movie", "series")
    priority = 70
    meta_only = True
    requires_key = True
    note = "Movie & TV metadata (requires a free TMDB_API_KEY)."
    home = "https://www.themoviedb.org"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.api_key = os.environ.get("TMDB_API_KEY") or self.config.get("api_key") or ""
        self.language = os.environ.get("TMDB_LANG") or self.config.get("language") or "en-US"

    def is_enabled(self) -> bool:
        return bool(self.api_key) and super().is_enabled()

    # -- internals ---------------------------------------------------------

    def _get(self, path: str, **params: Any) -> dict:
        params.setdefault("language", self.language)
        params["api_key"] = self.api_key
        from ..net import add_query

        url = add_query(API + path, params)
        data = self.get_json(url, cache=True, cache_ttl=1800)
        return data if isinstance(data, dict) else {}

    # -- interface ---------------------------------------------------------

    def search(self, query: SearchQuery) -> list[SearchResult]:
        if not self.api_key:
            raise ProviderError("tmdb: TMDB_API_KEY is not set")
        pages = max(1, min(int(query.limit or 20) // 20 + 1, 3))
        out: list[SearchResult] = []
        kinds = ["movie", "tv"] if not query.kind or query.kind in ("auto", "all") else (
            ["movie"] if query.kind == "movie" else ["tv"]
        )
        for kind in kinds:
            for page in range(1, pages + 1):
                data = self._get(
                    f"/search/{kind}",
                    query=query.text or "",
                    page=page,
                    **({"year": query.year} if query.year and kind == "movie" else {}),
                )
                for row in data.get("results") or []:
                    out.append(self._row_to_result(row, kind))
                if not data.get("results"):
                    break
        if query.sort == "popular":
            return self.trending(kind=query.kind, limit=query.limit)
        return out[: max(1, query.limit)]

    def _row_to_result(self, row: dict[str, Any], kind: str) -> SearchResult:
        title = row.get("title") or row.get("name") or "?"
        date = row.get("release_date") or row.get("first_air_date") or ""
        return SearchResult(
            title=title,
            url=f"https://www.themoviedb.org/{kind}/{row.get('id')}",
            provider=self.id,
            id=f"{kind}/{row.get('id')}",
            poster=f"{IMG}/{POSTER_SIZE}{row['poster_path']}" if row.get("poster_path") else "",
            year=date[:4],
            kind="movie" if kind == "movie" else "series",
            score=f"{row['vote_average']:.1f}" if row.get("vote_average") else "",
            status="",
            synopsis=(row.get("overview") or "").strip(),
        )

    def details(self, url_or_id: str) -> Title:
        if not self.api_key:
            raise ProviderError("tmdb: TMDB_API_KEY is not set")
        kind, ident = self._split(self.clean_ref(url_or_id))
        data = self._get(f"/{kind}/{ident}", append_to_response="external_ids,recommendations,credits")
        if not data or data.get("success") is False:
            raise ProviderError(f"tmdb: {url_or_id} not found")

        is_movie = kind == "movie"
        title = Title(
            title=data.get("title") or data.get("name") or "?",
            url=f"https://www.themoviedb.org/{kind}/{ident}",
            kind="movie" if is_movie else "series",
            provider=self.id,
            id=f"{kind}/{ident}",
            alt_titles=[t for t in (data.get("original_title"), data.get("original_name")) if t and t != (data.get("title") or data.get("name"))],
            synopsis=(data.get("overview") or "").strip(),
            poster=f"{IMG}/{POSTER_SIZE}{data['poster_path']}" if data.get("poster_path") else "",
            banner=f"{IMG}/{BACKDROP_SIZE}{data['backdrop_path']}" if data.get("backdrop_path") else "",
            year=((data.get("release_date") or data.get("first_air_date") or "")[:4]),
            status=data.get("status") or "",
            score=f"{data['vote_average']:.1f}" if data.get("vote_average") else "",
            genres=[g.get("name", "") for g in data.get("genres") or []],
            studios=[c.get("name", "") for c in (data.get("production_companies") or [])][:4],
            country=(data.get("production_countries") or [{}])[0].get("iso_3166_1", "") if data.get("production_countries") else "",
            duration=f"{data['runtime']}m" if data.get("runtime") else "",
            type_="Movie" if is_movie else "TV",
            total_episodes=(
                sum(s.get("episode_count", 0) for s in data.get("seasons") or []) or
                data.get("number_of_episodes") or 0
            ),
        )
        if is_movie:
            # a movie is a one-episode "show" everywhere in aninext, so play,
            # download and history work without special cases
            title.episodes.append(
                Episode(
                    number=1,
                    title=data.get("title") or title.title,
                    url=f"https://www.themoviedb.org/movie/{ident}",
                    synopsis=title.synopsis,
                    duration=title.duration,
                    thumbnail=title.poster,
                    provider=self.id,
                    id=f"movie/{ident}",
                )
            )
        else:
            title.seasons = [
                {
                    "season": s.get("season_number", 0),
                    "name": s.get("name", ""),
                    "episodes": s.get("episode_count", 0),
                    "year": (s.get("air_date") or "")[:4],
                    "poster": f"{IMG}/{POSTER_SIZE}{s['poster_path']}" if s.get("poster_path") else "",
                }
                for s in data.get("seasons") or []
            ]
        for rec in ((data.get("recommendations") or {}).get("results") or [])[:8]:
            rec_kind = "movie" if rec.get("media_type") == "movie" else "series"
            title.related.append(
                {
                    "title": rec.get("title") or rec.get("name") or "?",
                    "url": f"https://www.themoviedb.org/{rec_kind}/{rec.get('id')}",
                    "type": "recommendation",
                    "poster": f"{IMG}/{POSTER_SIZE}{rec['poster_path']}" if rec.get("poster_path") else "",
                }
            )
        title.raw["imdb"] = (data.get("external_ids") or {}).get("imdb_id") or data.get("imdb_id")
        title.raw["seasons"] = title.seasons
        return title

    def episodes(self, url_or_id: str) -> list[Episode]:
        if not self.api_key:
            raise ProviderError("tmdb: TMDB_API_KEY is not set")
        kind, ident = self._split(url_or_id)
        if kind == "movie":
            return self.details(url_or_id).episodes

        detail = self._get(f"/tv/{ident}")
        out: list[Episode] = []
        for season in [s for s in detail.get("seasons") or [] if (s.get("season_number") or 0) > 0]:
            data = self._get(f"/tv/{ident}/season/{season['season_number']}")
            for row in data.get("episodes") or []:
                out.append(
                    Episode(
                        number=parse_episode_number(str(row.get("episode_number") or 0)),
                        title=row.get("name") or "",
                        url=f"https://www.themoviedb.org/tv/{ident}/season/{season['season_number']}/episode/{row.get('episode_number')}",
                        thumbnail=f"{IMG}/w300{row['still_path']}" if row.get("still_path") else "",
                        synopsis=(row.get("overview") or "").strip(),
                        aired=row.get("air_date") or "",
                        duration=f"{row['runtime']}m" if row.get("runtime") else "",
                        season=int(row.get("season_number") or season.get("season_number") or 0),
                        provider=self.id,
                        id=f"s{season['season_number']}e{row.get('episode_number')}",
                    )
                )
        return out

    def trending(self, kind: str | None = None, limit: int = 20) -> list[SearchResult]:
        if not self.api_key:
            raise ProviderError("tmdb: TMDB_API_KEY is not set")
        results: list[SearchResult] = []
        for window in ("day",):
            for k in (["movie", "tv"] if not kind or kind in ("auto", "all") else (
                ["movie"] if kind == "movie" else ["tv"]
            )):
                data = self._get(f"/trending/{k}/{window}")
                results.extend(self._row_to_result(row, k) for row in data.get("results") or [])
        return results[: max(1, limit)]

    def schedule(self, *, days: int = 7, limit: int = 40) -> list[dict]:
        if not self.api_key:
            return []
        data = self._get("/tv/on_the_air")
        return [
            {
                "title": row.get("name") or "?",
                "episode": None,
                "time": (row.get("first_air_date") or ""),
                "url": f"https://www.themoviedb.org/tv/{row.get('id')}",
                "poster": f"{IMG}/{POSTER_SIZE}{row['poster_path']}" if row.get("poster_path") else "",
                "provider": self.id,
            }
            for row in (data.get("results") or [])[:limit]
        ]

    @staticmethod
    def _split(url_or_id: str) -> tuple[str, str]:
        import re

        text = str(url_or_id or "").strip()
        m = re.search(r"themoviedb\.org/(movie|tv)/(\d+)", text)
        if m:
            return m.group(1), m.group(2)
        if "/" in text:
            kind, _, ident = text.partition("/")
            if kind in ("movie", "tv") and ident.isdigit():
                return kind, ident
        if text.isdigit():
            return "movie", text
        raise ProviderError(f"tmdb: cannot parse {url_or_id!r}")

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": "metadata",
            "kinds": list(self.kinds),
            "base": self.home,
            "enabled": self.is_enabled(),
            "note": self.note + ("" if self.api_key else " (no API key - disabled)"),
        }


def build() -> TmdbProvider | None:
    provider = TmdbProvider()
    if not provider.api_key:
        log.debug("tmdb: no TMDB_API_KEY, provider stays disabled")
        return provider  # registered but disabled, so `providers` can show why
    return provider
