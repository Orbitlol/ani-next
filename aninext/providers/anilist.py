"""AniList metadata provider (GraphQL, no API key required).

Gives aninext accurate titles, posters, synopses, scores, genres, airing
schedule and - importantly - the *official* streaming links each show is
legally available on (Crunchyroll, Netflix, ...), which `aninext watch`
can open in a browser.

AniList is metadata only: it is registered as a `meta_only` provider, so it
enriches results without pretending to serve video.
"""
from __future__ import annotations

from typing import Any

from .. import log
from ..models import Episode, SearchResult, Stream, Title
from .base import MediaProvider, ProviderError, SearchQuery

API = "https://graphql.anilist.co"

_MEDIA_FIELDS = """
  id
  idMal
  title { romaji english native }
  synonyms
  description(asHtml: false)
  coverImage { extraLarge large color }
  bannerImage
  startDate { year month day }
  endDate { year }
  season
  seasonYear
  status
  format
  episodes
  duration
  countryOfOrigin
  averageScore
  meanScore
  popularity
  genres
  studios(isMain: true) { nodes { name } }
  trailer { id site }
  nextAiringEpisode { episode airingAt }
"""

_SEARCH_QUERY = """
query ($search: String, $page: Int, $perPage: Int, $type: MediaType, $format: MediaFormat, $sort: [MediaSort]) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { total currentPage lastPage hasNextPage }
    media(search: $search, type: $type, format: $format, sort: $sort, isAdult: false) {
      %s
    }
  }
}
""" % _MEDIA_FIELDS

_DETAIL_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    %s
    streamingEpisodes { title thumbnail url site }
    externalLinks { url site type }
    relations { edges { relationType node { id title { romaji english } format } } }
    recommendations(perPage: 8) { nodes { mediaRecommendation { id title { romaji english } coverImage { large } } } }
  }
}
""" % _MEDIA_FIELDS

_TRENDING_QUERY = """
query ($page: Int, $perPage: Int, $sort: [MediaSort]) {
  Page(page: $page, perPage: $perPage) {
    media(type: ANIME, sort: $sort, isAdult: false) { %s }
  }
}
""" % _MEDIA_FIELDS

_SCHEDULE_QUERY = """
query ($page: Int, $perPage: Int, $airingAtGreater: Int, $airingAtLesser: Int) {
  Page(page: $page, perPage: $perPage) {
    airingSchedules(airingAt_greater: $airingAtGreater, airingAt_lesser: $airingAtLesser,
                    sort: TIME) {
      episode
      airingAt
      media { id title { romaji english } coverImage { large } siteUrl }
    }
  }
}
"""

_KIND_FORMAT = {
    "movie": "MOVIE",
    "series": None,
    "anime": None,
    "tv": None,
}

_SORT = {
    "popular": ["POPULARITY_DESC"],
    "score": ["SCORE_DESC"],
    "newest": ["START_DATE_DESC"],
    "relevance": ["SEARCH_MATCH"],
    None: ["SEARCH_MATCH"],
    "": ["SEARCH_MATCH"],
}


class AniListProvider(MediaProvider):
    id = "anilist"
    name = "AniList"
    kinds = ("anime",)
    priority = 80
    meta_only = True
    note = "Metadata, scores and official streaming links for anime."
    home = "https://anilist.co"

    # -- graphql -----------------------------------------------------------

    def gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = {"query": query, "variables": variables}
        data = self.post_json(
            API,
            payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            referer="https://anilist.co/",
        )
        if not isinstance(data, dict):
            raise ProviderError("anilist: unexpected response")
        if data.get("errors"):
            message = (data["errors"][0] or {}).get("message", "unknown error")
            raise ProviderError(f"anilist: {message}")
        return data.get("data") or {}

    # -- mapping -----------------------------------------------------------

    def _to_result(self, media: dict[str, Any]) -> SearchResult:
        titles = media.get("title") or {}
        title = titles.get("english") or titles.get("romaji") or titles.get("native") or "?"
        year = str((media.get("startDate") or {}).get("year") or media.get("seasonYear") or "")
        score = ""
        if media.get("averageScore"):
            score = f"{media['averageScore'] / 10:.1f}"
        return SearchResult(
            title=title,
            url=f"https://anilist.co/anime/{media.get('id')}",
            provider=self.id,
            id=str(media.get("id") or ""),
            poster=(media.get("coverImage") or {}).get("extraLarge")
            or (media.get("coverImage") or {}).get("large") or "",
            year=year,
            kind="movie" if (media.get("format") or "") == "MOVIE" else "anime",
            score=score,
            status=_pretty_status(media.get("status") or ""),
            episodes=int(media.get("episodes") or 0),
            synopsis=_clean(media.get("description") or ""),
        )

    def _to_title(self, media: dict[str, Any]) -> Title:
        result = self._to_result(media)
        titles = media.get("title") or {}
        title = Title(
            title=result.title,
            url=result.url,
            kind=result.kind,
            provider=self.id,
            id=str(media.get("id") or ""),
            alt_titles=[
                t for t in {
                    titles.get("romaji"), titles.get("english"), titles.get("native"),
                    *(media.get("synonyms") or []),
                } if t
            ],
            synopsis=result.synopsis,
            poster=(media.get("coverImage") or {}).get("extraLarge") or "",
            banner=media.get("bannerImage") or "",
            year=result.year,
            status=result.status,
            score=result.score,
            genres=list(media.get("genres") or []),
            studios=[s["name"] for s in ((media.get("studios") or {}).get("nodes") or []) if s.get("name")],
            country=media.get("countryOfOrigin") or "",
            duration=f"{media.get('duration')}m" if media.get("duration") else "",
            total_episodes=int(media.get("episodes") or 0),
            type_=_pretty_format(media.get("format") or ""),
            raw={"idMal": media.get("idMal"), "rankings": media.get("popularity")},
        )
        for item in media.get("streamingEpisodes") or []:
            number = _episode_number(item.get("title") or "")
            if not number:
                continue
            ep = Episode(
                number=number,
                title=_clean_streming_title(item.get("title") or ""),
                url=item.get("url") or "",
                thumbnail=item.get("thumbnail") or "",
                provider=self.id,
                id=f"official-{number:g}",
            )
            title.episodes.append(ep)
        for link in media.get("externalLinks") or []:
            if link.get("type") == "STREAMING" and link.get("url"):
                title.related.append({"title": link.get("site") or "Official", "url": link["url"], "type": "streaming"})
        for edge in ((media.get("relations") or {}).get("edges") or []):
            node = edge.get("node") or {}
            rel_titles = node.get("title") or {}
            title.related.append(
                {
                    "title": rel_titles.get("english") or rel_titles.get("romaji") or "?",
                    "url": f"https://anilist.co/anime/{node.get('id')}",
                    "type": edge.get("relationType") or "RELATED",
                }
            )
        for node in ((media.get("recommendations") or {}).get("nodes") or []):
            rec = (node or {}).get("mediaRecommendation") or {}
            rec_titles = rec.get("title") or {}
            title.related.append(
                {
                    "title": rec_titles.get("english") or rec_titles.get("romaji") or "?",
                    "url": f"https://anilist.co/anime/{rec.get('id')}",
                    "type": "recommendation",
                    "poster": (rec.get("coverImage") or {}).get("large") or "",
                }
            )
        return title

    # -- provider interface ------------------------------------------------

    def search(self, query: SearchQuery) -> list[SearchResult]:
        variables: dict[str, Any] = {
            "search": query.text or None,
            "page": max(1, int(query.page or 1)),
            "perPage": max(1, min(int(query.limit or 24), 50)),
            "type": "ANIME",
            "sort": _SORT.get(query.sort or "relevance", ["SEARCH_MATCH"]),
        }
        if query.kind == "movie":
            variables["format"] = "MOVIE"
        elif query.kind == "series":
            variables["format"] = "TV"
        data = self.gql(_SEARCH_QUERY, variables)
        media = ((data.get("Page") or {}).get("media")) or []
        results = [self._to_result(m) for m in media]
        if query.year:
            results = [r for r in results if r.year == str(query.year)] or results
        return results

    def details(self, url_or_id: str) -> Title:
        url_or_id = self.clean_ref(url_or_id)
        ident = self._id_from(url_or_id)
        if ident is None:
            found = self.search(SearchQuery(text=url_or_id, limit=1))
            if not found:
                raise ProviderError(f"anilist: no match for {url_or_id!r}")
            ident = int(found[0].id)
        data = self.gql(_DETAIL_QUERY, {"id": int(ident)})
        media = data.get("Media") or {}
        if not media:
            raise ProviderError(f"anilist: id {ident} not found")
        return self._to_title(media)

    def episodes(self, url_or_id: str) -> list[Episode]:
        return self.details(url_or_id).episodes

    def streams(self, episode: Episode, *, title: Title | None = None) -> list[Stream]:
        """Official streaming links - opened in a browser, not scraped."""
        official = [
            r for r in (title.related if title else []) if r.get("type") == "streaming"
        ]
        out: list[Stream] = []
        for item in official:
            out.append(
                Stream(
                    url=item["url"],
                    kind="embed",
                    label=item.get("title", "Official"),
                    server=item.get("title", ""),
                    provider=self.id,
                    extra={"official": True},
                )
            )
        if episode.url and not out:
            out.append(
                Stream(url=episode.url, kind="embed", label="AniList", provider=self.id,
                       server="anilist", extra={"official": True})
            )
        return out

    def trending(self, kind: str | None = None, limit: int = 20) -> list[SearchResult]:
        data = self.gql(_TRENDING_QUERY, {"page": 1, "perPage": min(int(limit or 20), 50),
                                          "sort": ["TRENDING_DESC"]})
        media = ((data.get("Page") or {}).get("media")) or []
        return [self._to_result(m) for m in media]

    def season(self, year: int, season: str, **kw) -> list[SearchResult]:
        query = """
        query ($season: MediaSeason, $year: Int, $page: Int, $perPage: Int) {
          Page(page: $page, perPage: $perPage) {
            media(type: ANIME, season: $season, seasonYear: $year, sort: POPULARITY_DESC) {
              %s
            }
          }
        }
        """ % _MEDIA_FIELDS
        data = self.gql(query, {"season": season.upper(), "year": int(year), "page": 1,
                                "perPage": min(int(kw.get("limit", 24)), 50)})
        media = ((data.get("Page") or {}).get("media")) or []
        return [self._to_result(m) for m in media]

    def schedule(self, *, days: int = 7, limit: int = 40) -> list[dict]:
        import time as _time

        now = int(_time.time())
        data = self.gql(
            _SCHEDULE_QUERY,
            {"page": 1, "perPage": min(int(limit or 40), 50),
             "airingAtGreater": now, "airingAtLesser": now + max(1, days) * 86400},
        )
        rows = ((data.get("Page") or {}).get("airingSchedules")) or []
        out = []
        for row in rows:
            media = row.get("media") or {}
            titles = media.get("title") or {}
            out.append(
                {
                    "title": titles.get("english") or titles.get("romaji") or "?",
                    "episode": row.get("episode"),
                    "airing_at": row.get("airingAt"),
                    "time": _time.strftime("%a %d %b %H:%M", _time.localtime(row.get("airingAt") or 0)),
                    "url": media.get("siteUrl") or "",
                    "poster": (media.get("coverImage") or {}).get("large") or "",
                    "provider": self.id,
                }
            )
        return out

    @staticmethod
    def _id_from(url_or_id: str) -> int | None:
        import re

        text = str(url_or_id or "").strip()
        if text.lower().startswith("anilist:"):
            text = text.split(":", 1)[1].strip()
        if text.isdigit():
            return int(text)
        m = re.search(r"anilist\.co/anime/(\d+)", text)
        return int(m.group(1)) if m else None

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": "metadata",
            "kinds": list(self.kinds),
            "base": self.home,
            "enabled": self.is_enabled(),
            "note": self.note,
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _clean(text: str) -> str:
    if not text:
        return ""
    import re

    from ..net import strip_html

    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = strip_html(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_streming_title(text: str) -> str:
    import re

    return re.sub(r"^Episode\s*\d+\s*[-–:]\s*", "", text, flags=re.I).strip()


def _episode_number(text: str) -> float:
    import re

    m = re.search(r"[Ee]pisode\s*(\d+(?:\.\d+)?)", text or "")
    if m:
        return float(m.group(1))
    m = re.search(r"\b(\d{1,4})\b", text or "")
    return float(m.group(1)) if m else 0.0


def _pretty_status(status: str) -> str:
    return {
        "FINISHED": "Completed",
        "RELEASING": "Ongoing",
        "NOT_YET_RELEASED": "Upcoming",
        "CANCELLED": "Cancelled",
        "HIATUS": "Hiatus",
    }.get(status, status.title() if status else "")


def _pretty_format(fmt: str) -> str:
    return {
        "TV": "TV",
        "TV_SHORT": "TV Short",
        "MOVIE": "Movie",
        "SPECIAL": "Special",
        "OVA": "OVA",
        "ONA": "ONA",
        "MUSIC": "Music",
    }.get(fmt, fmt)


def build() -> AniListProvider:
    provider = AniListProvider()
    provider.config.setdefault("enabled", True)
    return provider
