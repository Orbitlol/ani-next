"""Provider mapping tests.

The sandbox (and CI) has no route to archive.org / anilist.co, so every test
here stubs the provider's HTTP layer with a realistic fixture payload and
checks the mapping onto aninext's models.  Nothing touches the network.
"""
from __future__ import annotations

import unittest

from aninext.models import Episode, Title
from aninext.providers import all_providers, enabled, get, ids, load_builtin, require, resolve
from aninext.providers.base import SearchQuery
from aninext.providers.rules import json_path, json_to_str


class TestRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_builtin()

    def test_builtin_ids(self) -> None:
        for pid in ("demo", "anilist", "archive", "tmdb", "consumet"):
            self.assertIn(pid, ids(), pid)
            self.assertIsNotNone(get(pid))

    def test_get_and_require(self) -> None:
        from aninext.providers.base import ProviderError

        self.assertIsNone(get("no-such-provider"))
        with self.assertRaises(ProviderError):
            require("no-such-provider")
        self.assertEqual(require("demo").id, "demo")

    def test_priority_order(self) -> None:
        order = [p.id for p in all_providers()]
        self.assertLess(order.index("anilist"), order.index("archive"))
        self.assertLess(order.index("archive"), order.index("demo"))

    def test_resolve_prefers_id_then_kind(self) -> None:
        self.assertEqual(resolve("demo").id, "demo")
        self.assertEqual(resolve(None).id, resolve(None).id)  # stable default
        self.assertIn("anime", resolve(None, kind="anime").kinds)
        self.assertTrue(enabled())

    def test_describe_shape(self) -> None:
        info = require("demo").describe()
        for key in ("id", "name", "kinds", "enabled"):
            self.assertIn(key, info)


class TestArchiveProvider(unittest.TestCase):
    def setUp(self) -> None:
        from aninext.providers.archive import ArchiveProvider

        self.provider = ArchiveProvider()
        self.provider.enabled = True

    SEARCH = {
        "response": {
            "numFound": 2,
            "docs": [
                {"identifier": "night_of_the_living_dead",
                 "title": "Night of the Living Dead",
                 "year": "1968", "subject": ["Horror", "Feature films"],
                 "description": "A ragtag group barricades itself in a farmhouse.",
                 "downloads": 1234},
                {"identifier": "popeye_cartoons",
                 "title": "Popeye the Sailor - Classic Cartoons",
                 "year": "1948", "subject": ["animation", "cartoon"],
                 "description": ["Public domain cartoons."]},
            ],
        }
    }

    METADATA = {
        "metadata": {
            "identifier": "night_of_the_living_dead",
            "title": "Night of the Living Dead",
            "date": "1968-10-01",
            "subject": "Horror;Feature films",
            "description": "A ragtag group barricades itself in a farmhouse.",
            "creator": ["George A. Romero"],
            "licenseurl": "https://creativecommons.org/publicdomain/mark/1.0/",
        },
        "files": [
            {"name": "night_of_the_living_dead.mp4", "format": "h.264", "size": "734003200",
             "length": "5760.0"},
            {"name": "night_of_the_living_dead.srt", "format": "SubRip", "size": "52000"},
            {"name": "night_of_the_living_dead.ogv", "format": "Ogg Video", "size": "1000",
             "length": "5760.0"},
            {"name": "__ia_thumb.jpg", "format": "JPEG Thumb", "size": "9000"},
        ],
    }

    def test_search_maps_docs(self) -> None:
        self.provider.get_json = lambda url, **kw: self.SEARCH
        results = self.provider.search(SearchQuery(text="night", limit=10))
        self.assertEqual(len(results), 2)
        first = results[0]
        self.assertEqual(first.title, "Night of the Living Dead")
        self.assertEqual(first.year, "1968")
        self.assertEqual(first.kind, "movie")
        self.assertEqual(first.id, "night_of_the_living_dead")
        self.assertIn("archive.org/services/img", first.poster)
        self.assertIn("farmhouse", first.synopsis)
        # subjects with "cartoon"/"animation" are anime, not movies
        self.assertEqual(results[1].kind, "anime")

    def test_search_builds_a_scoped_query(self) -> None:
        seen: list[str] = []

        def fake(url, **kw):
            seen.append(url)
            return self.SEARCH

        self.provider.get_json = fake
        self.provider.search(SearchQuery(text="popeye", kind="anime", year="1948", limit=5))
        self.assertIn("mediatype%3A%28movies%29", seen[0])
        self.assertIn("subject%3Aanimation", seen[0])
        self.assertIn("year%3A1948", seen[0])
        self.assertIn("rows=5", seen[0])

    def test_details_builds_episodes_and_streams(self) -> None:
        self.provider.get_json = lambda url, **kw: self.METADATA
        title = self.provider.details("night_of_the_living_dead")
        self.assertEqual(title.title, "Night of the Living Dead")
        self.assertEqual(title.year, "1968")
        self.assertEqual(title.studios, ["George A. Romero"])
        self.assertIn("Horror", title.genres)
        # one video -> a movie with a single episode, thumbnails skipped
        self.assertEqual(len(title.episodes), 2)  # mp4 + ogv, minus the jpeg thumb
        self.assertEqual(title.kind, "movie")
        episode = title.episodes[0]
        self.assertTrue(episode.streams)
        stream = episode.streams[0]
        self.assertTrue(stream.url.startswith("https://archive.org/download/"))
        self.assertEqual(stream.headers.get("Referer"), "https://archive.org/")
        self.assertGreater(stream.size, 0)

    def test_details_follows_urls(self) -> None:
        urls: list[str] = []

        def fake(url, **kw):
            urls.append(url)
            return self.METADATA

        self.provider.get_json = fake
        title = self.provider.details("https://archive.org/details/night_of_the_living_dead")
        self.assertIn("metadata/night_of_the_living_dead", urls[0])
        self.assertEqual(title.provider, "archive")

    def test_details_without_videos_raises(self) -> None:
        self.provider.get_json = lambda url, **kw: {"metadata": {"identifier": "x"}, "files": []}
        from aninext.providers.base import ProviderError

        with self.assertRaises(ProviderError):
            self.provider.details("x")

    def test_subtitles_are_attached(self) -> None:
        self.provider.get_json = lambda url, **kw: self.METADATA
        title = self.provider.details("night_of_the_living_dead")
        subs = [s for ep in title.episodes for s in ep.subtitles]
        self.assertTrue(subs)
        self.assertTrue(subs[0].url.endswith("night_of_the_living_dead.srt"))

    def test_streams_reuses_episode_streams(self) -> None:
        self.provider.get_json = lambda url, **kw: self.METADATA
        title = self.provider.details("night_of_the_living_dead")
        episode = title.episodes[0]
        self.assertEqual(self.provider.streams(episode, title=title), episode.streams)
        self.assertTrue(self.provider.downloads(episode, title=title))

    def test_verify_identifier_helper(self) -> None:
        from aninext.providers.archive import ArchiveProvider as A

        self.assertEqual(A._identifier("https://archive.org/details/foo"), "foo")
        self.assertEqual(A._identifier("archive:foo"), "foo")
        self.assertEqual(A._identifier("foo"), "foo")
        self.assertEqual(A._identifier("https://archive.org/download/foo/bar.mp4"), "foo")

    def test_collections(self) -> None:
        ids_ = [c["id"] for c in self.provider.collections()]
        self.assertIn("feature_films", ids_)
        self.assertIn("anime", ids_)


class TestAniListProvider(unittest.TestCase):
    MEDIA = {
        "id": 158309,
        "title": {"romaji": "Sousou no Frieren", "english": "Frieren: Beyond Journey's End",
                  "native": "葬送のフリーレン"},
        "synonyms": ["Frieren at the Funeral"],
        "startDate": {"year": 2023, "month": 9, "day": 29},
        "averageScore": 90,
        "popularity": 500000,
        "format": "TV",
        "status": "FINISHED",
        "episodes": 28,
        "duration": 24,
        "countryOfOrigin": "JP",
        "genres": ["Adventure", "Drama", "Fantasy"],
        "coverImage": {"extraLarge": "https://img.anilist.co/frieren-xl.jpg", "large": "https://img.anilist.co/frieren.jpg"},
        "bannerImage": "https://img.anilist.co/frieren-banner.jpg",
        "description": "<p>After the <i>heroes</i> defeated the Demon King&hellip;</p>",
        "studios": {"nodes": [{"name": "Madhouse"}, {"name": "Shogakukan"}]},
        "streamingEpisodes": [
            {"title": "Episode 1 - The Journey's End", "url": "https://crunchyroll.com/x",
             "thumbnail": "https://img.anilist.co/t1.jpg"},
            {"title": "Episode 2 - It Didn't Have to Be Magic", "url": "https://crunchyroll.com/y",
             "thumbnail": "https://img.anilist.co/t2.jpg"},
        ],
        "externalLinks": [{"type": "STREAMING", "site": "Crunchyroll", "url": "https://crunchyroll.com/frieren"}],
        "relations": {"edges": [{"relationType": "SEQUEL",
                                 "node": {"id": 1, "title": {"romaji": "Frieren Season 2"}}}]},
        "recommendations": {"nodes": [{"mediaRecommendation": {
            "id": 2, "title": {"english": "Mushoku Tensei"},
            "coverImage": {"large": "https://img.anilist.co/mt.jpg"}}}]},
    }

    def setUp(self) -> None:
        from aninext.providers.anilist import AniListProvider

        self.provider = AniListProvider()

    def test_gql_unwraps_data(self) -> None:
        self.provider.post_json = lambda url, payload, **kw: {"data": {"Page": {"media": [self.MEDIA]}}}
        data = self.provider.gql("query", {})
        self.assertIn("Page", data)

    def test_gql_raises_on_graphql_errors(self) -> None:
        from aninext.providers.base import ProviderError

        self.provider.post_json = lambda url, payload, **kw: {"errors": [{"message": "Too many requests"}]}
        with self.assertRaises(ProviderError) as ctx:
            self.provider.gql("query", {})
        self.assertIn("Too many requests", str(ctx.exception))

    def test_search_and_result_mapping(self) -> None:
        captured: dict = {}

        def fake(url, payload, **kw):
            captured.update(payload)
            return {"data": {"Page": {"media": [self.MEDIA]}}}

        self.provider.post_json = fake
        results = self.provider.search(SearchQuery(text="frieren", limit=5))
        self.assertEqual(captured["variables"]["search"], "frieren")
        self.assertEqual(captured["variables"]["perPage"], 5)
        self.assertEqual(results[0].title, "Frieren: Beyond Journey's End")
        self.assertEqual(results[0].year, "2023")
        self.assertEqual(results[0].score, "9.0")
        self.assertEqual(results[0].kind, "anime")
        self.assertEqual(results[0].status, "Completed")
        self.assertEqual(results[0].episodes, 28)
        self.assertIn("heroes", results[0].synopsis)

    def test_search_movie_kind_filters_format(self) -> None:
        captured: dict = {}

        def fake(url, payload, **kw):
            captured.update(payload)
            return {"data": {"Page": {"media": []}}}

        self.provider.post_json = fake
        self.provider.search(SearchQuery(text="akira", kind="movie"))
        self.assertEqual(captured["variables"]["format"], "MOVIE")

    def test_details_mapping(self) -> None:
        self.provider.post_json = lambda url, payload, **kw: {"data": {"Media": self.MEDIA}}
        title = self.provider.details("158309")
        self.assertEqual(title.title, "Frieren: Beyond Journey's End")
        self.assertEqual(title.kind, "anime")
        self.assertEqual(title.type_, "TV")
        self.assertEqual(title.total_episodes, 28)
        self.assertEqual(title.duration, "24m")
        self.assertEqual(title.studios, ["Madhouse", "Shogakukan"])
        self.assertIn("Sousou no Frieren", title.alt_titles)
        self.assertIn("Frieren at the Funeral", title.alt_titles)
        self.assertNotIn("<i>", title.synopsis)
        # episodes come from streamingEpisodes
        self.assertEqual([e.number for e in title.episodes], [1.0, 2.0])
        self.assertEqual(title.episodes[0].title, "The Journey's End")
        # official links + relations + recommendations land in .related
        types = {r["type"] for r in title.related}
        self.assertIn("streaming", types)
        self.assertIn("SEQUEL", types)
        self.assertIn("recommendation", types)

    def test_details_by_url_and_id(self) -> None:
        self.provider.post_json = lambda url, payload, **kw: {"data": {"Media": self.MEDIA}}
        self.assertEqual(self.provider.details("https://anilist.co/anime/158309").id, "158309")
        self.assertEqual(self.provider.details("anilist:158309").id, "158309")

    def test_streams_are_official_embeds_only(self) -> None:
        self.provider.post_json = lambda url, payload, **kw: {"data": {"Media": self.MEDIA}}
        title = self.provider.details("158309")
        streams = self.provider.streams(title.episodes[0], title=title)
        self.assertTrue(streams)
        self.assertTrue(all(s.kind == "embed" and s.extra.get("official") for s in streams))
        self.assertFalse(any(s.playable for s in streams))

    def test_helpers(self) -> None:
        from aninext.providers.anilist import _clean, _episode_number, _pretty_format, _pretty_status

        self.assertEqual(_pretty_status("FINISHED"), "Completed")
        self.assertEqual(_pretty_status("RELEASING"), "Ongoing")
        self.assertEqual(_pretty_status("NOT_YET_RELEASED"), "Upcoming")
        self.assertEqual(_pretty_format("TV_SHORT"), "TV Short")
        self.assertEqual(_pretty_format("MOVIE"), "Movie")
        self.assertEqual(_episode_number("Episode 12 - Title"), 12.0)
        self.assertEqual(_episode_number("no number here"), 0.0)
        self.assertNotIn("&hellip;", _clean("<p>a &amp; b&hellip;</p>"))

    def test_id_from(self) -> None:
        from aninext.providers.anilist import AniListProvider as A

        self.assertEqual(A._id_from("https://anilist.co/anime/21"), 21)
        self.assertEqual(A._id_from("anilist:21"), 21)
        self.assertEqual(A._id_from("21"), 21)
        self.assertIsNone(A._id_from("one piece"))

    def test_schedule_rows(self) -> None:
        payload = {"data": {"Page": {"airingSchedules": [
            {"episode": 3, "airingAt": 1700000000,
             "media": {"id": 9, "title": {"romaji": "Some Anime"},
                       "coverImage": {"large": "https://img.anilist.co/x.jpg"},
                       "siteUrl": "https://anilist.co/anime/9"}},
        ]}}}
        self.provider.post_json = lambda url, p, **kw: payload
        rows = self.provider.schedule(days=7, limit=5)
        self.assertEqual(rows[0]["title"], "Some Anime")
        self.assertEqual(rows[0]["episode"], 3)
        self.assertEqual(rows[0]["provider"], "anilist")


class TestTMDbProvider(unittest.TestCase):
    def setUp(self) -> None:
        from aninext.providers.tmdb import TmdbProvider

        self.provider = TmdbProvider({"api_key": "test-key"})

    def test_disabled_without_key(self) -> None:
        from aninext.providers.tmdb import TmdbProvider

        provider = TmdbProvider({})
        provider.api_key = ""
        self.assertFalse(provider.is_enabled())
        with self.assertRaises(Exception):
            provider.search(SearchQuery(text="dune"))

    def test_search_movies_and_series(self) -> None:
        def fake(path, **params):
            if path == "/search/movie":
                return {"results": [{"id": 693134, "title": "Dune: Part Two", "release_date": "2024-02-27",
                                     "vote_average": 8.2, "overview": "Paul Atreides unites with the Fremen.",
                                     "poster_path": "/dune2.jpg"}]}
            return {"results": [{"id": 1399, "name": "Game of Thrones", "first_air_date": "2011-04-17",
                                 "vote_average": 8.4, "overview": "Nine noble families fight.",
                                 "poster_path": "/got.jpg"}]}

        self.provider._get = fake
        results = self.provider.search(SearchQuery(text="dune", limit=10))
        self.assertEqual(len(results), 2)
        movie = next(r for r in results if r.kind == "movie")
        self.assertEqual(movie.title, "Dune: Part Two")
        self.assertEqual(movie.year, "2024")
        self.assertEqual(movie.score, "8.2")
        self.assertIn("image.tmdb.org", movie.poster)
        series = next(r for r in results if r.kind == "series")
        self.assertEqual(series.year, "2011")

    def test_details_movie(self) -> None:
        self.provider._get = lambda path, **params: {
            "id": 693134, "title": "Dune: Part Two", "runtime": 167,
            "release_date": "2024-02-27", "vote_average": 8.2, "status": "Released",
            "overview": "Paul Atreides unites with the Fremen.",
            "genres": [{"name": "Science Fiction"}],
            "production_companies": [{"name": "Legendary Pictures"}],
            "tagline": "Long live the fighters.",
            "imdb_id": "tt15239678",
        }
        title = self.provider.details("movie/693134")
        self.assertEqual(title.kind, "movie")
        self.assertEqual(title.title, "Dune: Part Two")
        self.assertEqual(title.genres, ["Science Fiction"])
        self.assertIn("tt15239678", str(title.raw))
        self.assertTrue(title.episodes)  # a movie is a one-episode "show"

    def test_details_rejects_unknown(self) -> None:
        self.provider._get = lambda path, **params: {"success": False}
        with self.assertRaises(Exception):
            self.provider.details("movie/1")


class TestConsumetProvider(unittest.TestCase):
    def setUp(self) -> None:
        from aninext.providers.consumet import ConsumetProvider

        self.provider = ConsumetProvider({"api": "http://127.0.0.1:3000"})

    def test_disabled_without_api(self) -> None:
        from aninext.providers.consumet import ConsumetProvider

        provider = ConsumetProvider({})
        provider.api = ""
        self.assertFalse(provider.is_enabled())

    def test_anime_search(self) -> None:
        self.provider.get_json = lambda url, **kw: {
            "results": [{"id": "frieren-xyz", "title": "Frieren: Beyond Journey's End",
                         "image": "https://gogocdn.net/frieren.jpg", "releaseDate": "2023",
                         "totalEpisodes": "28"}]
        }
        results = self.provider.search(SearchQuery(text="frieren", kind="anime"))
        self.assertEqual(results[0].title, "Frieren: Beyond Journey's End")
        self.assertEqual(results[0].kind, "anime")
        self.assertEqual(results[0].episodes, 28)
        self.assertEqual(results[0].id, "frieren-xyz")

    def test_movie_search(self) -> None:
        self.provider.get_json = lambda url, **kw: {
            "results": [{"id": "m1", "title": "Big Buck Bunny", "type": "movie",
                         "image": "https://x/y.jpg", "releaseDate": "2008"}]
        }
        results = self.provider.search(SearchQuery(text="bunny", kind="movie"))
        self.assertEqual(results[0].kind, "movie")

    def test_details_and_episodes(self) -> None:
        self.provider.get_json = lambda url, **kw: {
            "id": "frieren-xyz", "title": "Frieren", "description": "An elf mage.",
            "image": "https://x/p.jpg", "cover": "https://x/b.jpg", "releaseDate": "2023",
            "status": "Completed", "rating": "9.0", "genres": ["Adventure"],
            "studio": "Madhouse", "type": "TV", "totalEpisodes": 2,
            "episodes": [{"id": "ep-1", "number": 1, "title": "The Journey's End"},
                         {"id": "ep-2", "number": 2, "title": "Magic"}],
        }
        title = self.provider.details("frieren-xyz")
        self.assertEqual(title.title, "Frieren")
        self.assertEqual(title.kind, "anime")
        self.assertEqual(title.genres, ["Adventure"])
        self.assertEqual([e.number for e in title.episodes], [1.0, 2.0])
        self.assertEqual(title.episodes[0].title, "The Journey's End")

    def test_streams_prefer_m3u8(self) -> None:
        episode = Episode(number=1, id="ep-1", url="gogoanime:frieren-xyz")
        self.provider.get_json = lambda url, **kw: {"sources": [
            {"url": "https://cdn/x/480.mp4", "quality": "480p", "isM3U8": False},
            {"url": "https://cdn/x/master.m3u8", "quality": "auto", "isM3U8": True},
        ]}
        streams = self.provider.streams(episode)
        kinds = {s.kind for s in streams}
        self.assertEqual(kinds, {"hls", "mp4"})
        hls = next(s for s in streams if s.kind == "hls")
        self.assertIn("m3u8", hls.url)
        self.assertEqual(hls.provider, "consumet")
        self.assertTrue(all(s.playable for s in streams))

    def test_split_helper(self) -> None:
        from aninext.providers.consumet import ConsumetProvider as C

        self.assertEqual(C._split("hianime:one-piece-100"), ("hianime", "one-piece-100"))
        self.assertEqual(C._split("a/b"), ("gogoanime", "a/b"))


class TestRuleProviderJson(unittest.TestCase):
    """json_path() backs rules written against JSON APIs."""

    DOC = {"data": {"items": [{"title": "A", "nested": {"x": 1}}, {"title": "B"}]}}

    def test_json_path(self) -> None:
        self.assertEqual(json_path(self.DOC, "$.data.items[*].title"), ["A", "B"])
        self.assertEqual(json_path(self.DOC, "data.items[0].title"), "A")
        self.assertEqual(json_path(self.DOC, "$.data.items[0].nested.x"), 1)
        self.assertEqual(json_path(self.DOC, "@data.items[1].title"), "B")
        self.assertEqual(json_path(self.DOC, "$.data.nope"), [])
        self.assertEqual(json_path(self.DOC, ""), self.DOC)

    def test_json_path_text_and_root(self) -> None:
        self.assertEqual(json_path("x", "::text"), "x")
        self.assertEqual(json_path({"a": "x"}, "$"), {"a": "x"})

    def test_json_to_str(self) -> None:
        self.assertEqual(json_to_str(None), "")
        self.assertEqual(json_to_str(12), "12")
        self.assertEqual(json_to_str(["a", "b"]), "a, b")
        self.assertEqual(json_to_str("  hi  "), "hi")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
