"""Rule engine + demo provider: the end-to-end scraping pipeline (offline)."""
from __future__ import annotations

import unittest

from aninext.providers.base import SearchQuery
from aninext.providers.demo import CATALOG, RULE, build
from aninext.providers.rules import RuleProvider, apply_transforms, json_path, parse_rule, split_fallbacks, validate_rule


class TestRuleProvider(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.provider = build()

    def test_search(self) -> None:
        results = self.provider.search(SearchQuery(text="frieren", limit=10))
        self.assertEqual(len(results), 1)
        hit = results[0]
        self.assertEqual(hit.title, "Frieren: Beyond Journey's End")
        self.assertEqual(hit.year, "2023")
        self.assertEqual(hit.episodes, 8)
        self.assertEqual(hit.score, "9.0")
        self.assertEqual(hit.kind, "anime")
        self.assertTrue(hit.poster.startswith("https://demo.aninext.local/img/"))

    def test_search_pagination(self) -> None:
        results = self.provider.search(SearchQuery(text="", limit=10))
        self.assertEqual(len(results), len(CATALOG))
        self.assertEqual(results[0].title, "Frieren: Beyond Journey's End")

    def test_search_by_genre(self) -> None:
        results = self.provider.search(SearchQuery(text="sci-fi", limit=10))
        titles = [r.title for r in results]
        self.assertIn("Severance", titles)
        self.assertIn("Blade Runner 2049", titles)

    def test_search_respects_limit(self) -> None:
        results = self.provider.search(SearchQuery(text="", limit=1))
        self.assertEqual(len(results), 1)

    def test_details(self) -> None:
        title = self.provider.details("demo:frieren")
        self.assertEqual(title.title, "Frieren: Beyond Journey's End")
        self.assertEqual(title.kind, "anime")
        self.assertEqual(title.type_, "TV")
        self.assertEqual(title.year, "2023")
        self.assertEqual(title.status, "Completed")
        self.assertEqual(title.score, "9.0")
        self.assertEqual(title.total_episodes, 8)
        self.assertIn("Fantasy", title.genres)
        self.assertIn("Madhouse", title.studios)
        self.assertIn("elven mage", title.synopsis)
        self.assertTrue(title.poster.endswith("/img/frieren.jpg"))

    def test_details_by_url(self) -> None:
        title = self.provider.details("https://demo.aninext.local/anime/severance")
        self.assertEqual(title.title, "Severance")
        self.assertEqual(title.kind, "series")

    def test_episode_pagination(self) -> None:
        title = self.provider.details("demo:frieren")
        numbers = [e.number for e in title.episodes]
        self.assertEqual(numbers, [1, 2, 3, 4, 5, 6, 7, 8])
        first = title.episodes[0]
        self.assertEqual(first.title, "The Journey's End")
        self.assertEqual(first.duration, "24m")
        self.assertEqual(first.aired, "2023-09-29")
        self.assertEqual(first.audio, ["sub", "dub"])
        self.assertTrue(first.url.endswith("/watch/frieren-1"))

    def test_movie_has_one_episode(self) -> None:
        title = self.provider.details("demo:blade-runner-2049")
        self.assertEqual(title.kind, "movie")
        self.assertEqual(len(title.episodes), 1)

    def test_streams_resolve_packed_js(self) -> None:
        title = self.provider.details("demo:frieren")
        streams = self.provider.streams(title.episodes[0], title=title)
        urls = [s.url for s in streams]
        self.assertTrue(any("master.m3u8" in u for u in urls), urls)          # from packed javascript
        self.assertTrue(any(u.endswith("-1080p.mp4") for u in urls), urls)     # direct server
        kinds = {s.kind for s in streams}
        self.assertIn("hls", kinds)
        self.assertIn("mp4", kinds)

    def test_streams_decode_base64_embed(self) -> None:
        title = self.provider.details("demo:frieren")
        streams = self.provider.streams(title.episodes[1], title=title)
        self.assertTrue(any("frieren-2-1080p.mp4" in s.url for s in streams), [s.url for s in streams])

    def test_downloads(self) -> None:
        title = self.provider.details("demo:frieren")
        links = self.provider.downloads(title.episodes[0], title=title)
        self.assertEqual(len(links), 3)
        self.assertEqual([l.quality for l in links], ["1080p", "720p", "480p"])
        self.assertGreater(links[0].size, 1_000_000)

    def test_subtitles(self) -> None:
        title = self.provider.details("demo:frieren")
        subs = self.provider.subtitles(title.episodes[0], title=title)
        self.assertEqual([s.lang for s in subs], ["en", "es"])
        self.assertTrue(all(s.url.endswith(".vtt") for s in subs))

    def test_related(self) -> None:
        title = self.provider.details("demo:frieren")
        titles = [r["title"] for r in title.related]
        self.assertIn("Severance", titles)


class TestTransforms(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(apply_transforms("  Hi  ", ["trim", "lower"]), "hi")
        self.assertEqual(apply_transforms("Ep 12", ["regex:(\\d+)"]), "12")
        self.assertEqual(apply_transforms("1080p WEB-DL", ["quality"]), "1080p")
        self.assertEqual(apply_transforms("1.4 GB", ["size"]), str(int(1.4 * 1024**3)))
        self.assertEqual(apply_transforms("a-b-c", ["split:-"]), "a")
        self.assertEqual(apply_transforms("abc", ["prefix:x", "suffix:y"]), "xabcy")

    def test_url_transform(self) -> None:
        self.assertEqual(
            apply_transforms("/img/a.jpg", ["url"], "https://site.example/anime/x"),
            "https://site.example/img/a.jpg",
        )

    def test_date_transform(self) -> None:
        self.assertEqual(apply_transforms("Sep 29, 2023", ["date"]), "2023-09-29")


class TestJsonPath(unittest.TestCase):
    def setUp(self) -> None:
        self.data = {"data": {"media": [{"title": {"romaji": "Frieren"}, "id": 1, "genres": ["Fantasy"]},
                                       {"title": {"romaji": "Bleach"}, "id": 2}]}}

    def test_paths(self) -> None:
        self.assertEqual(json_path(self.data, "data.media[0].id"), 1)
        self.assertEqual(json_path(self.data, "data.media[*].id"), [1, 2])
        self.assertEqual(json_path(self.data, "data.media[1].title.romaji"), "Bleach")
        self.assertEqual(json_path(self.data, "@data.media[0].genres"), ["Fantasy"])
        self.assertEqual(json_path(self.data, "nope.nothing"), [])

    def test_fallbacks(self) -> None:
        self.assertEqual(split_fallbacks("a @href | b @href"), ["a @href", "b @href"])
        self.assertEqual(split_fallbacks("a@href||b"), ["a@href", "b"])
        self.assertEqual(split_fallbacks("[lang|=en]"), ["[lang|=en]"])


class TestValidation(unittest.TestCase):
    def test_valid_rule(self) -> None:
        self.assertEqual(validate_rule(RULE), [])

    def test_missing_fields(self) -> None:
        problems = validate_rule({"id": "x", "search": {}})
        self.assertTrue(any("base" in p for p in problems))
        self.assertTrue(any("items" in p for p in problems))

    def test_parse_rule_sets_id(self) -> None:
        provider = parse_rule({"base": "https://x.example", "search": {"url": "/s", "items": "li"}})
        self.assertIsInstance(provider, RuleProvider)
        self.assertEqual(provider.id, "rule")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
