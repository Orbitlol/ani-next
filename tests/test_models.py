"""Model helpers: quality ranking, slugs, stream selection, episodes."""
from __future__ import annotations

import unittest

from aninext.models import (
    DownloadLink,
    Episode,
    HistoryEntry,
    SearchResult,
    Stream,
    Title,
    dedupe_streams,
    guess_kind,
    norm_quality,
    quality_rank,
    slugify,
)


class TestHelpers(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(slugify("Frieren: Beyond Journey's End"), "frieren-beyond-journeys-end")
        self.assertEqual(slugify("  --Weird__Name!!  "), "weird-name")
        self.assertEqual(slugify(""), "untitled")
        self.assertEqual(slugify("Blade Runner 2049"), "blade-runner-2049")

    def test_quality(self) -> None:
        self.assertEqual(norm_quality("Full HD 1080p WEB"), "1080p")
        self.assertEqual(norm_quality("2160p"), "4k")
        self.assertGreater(quality_rank("1080p"), quality_rank("720p"))
        self.assertGreater(quality_rank("4k"), quality_rank("1080p"))

    def test_guess_kind(self) -> None:
        self.assertEqual(guess_kind("https://x/y.m3u8"), "hls")
        self.assertEqual(guess_kind("https://x/y.mp4?a=1"), "mp4")
        self.assertEqual(guess_kind("https://x/y.mpd"), "dash")
        self.assertEqual(guess_kind("magnet:?xt=urn:btih:x"), "torrent")
        self.assertEqual(guess_kind("https://x/embed/abc"), "embed")


class TestStream(unittest.TestCase):
    def test_playable(self) -> None:
        self.assertTrue(Stream(url="https://x/a.m3u8", kind="hls").playable)
        self.assertFalse(Stream(url="https://x/embed/a", kind="embed").playable)
        self.assertFalse(Stream(url="https://x/a.m3u8", kind="hls", drm=True).playable)

    def test_quality_inferred_from_url(self) -> None:
        self.assertEqual(Stream(url="https://x/movie-1080p.mp4").quality, "1080p")

    def test_dedupe(self) -> None:
        streams = [
            Stream(url="https://x/a.m3u8?t=1", quality="1080p"),
            Stream(url="https://x/a.m3u8?t=2", quality="1080p"),
            Stream(url="https://x/b.mp4", quality="720p"),
        ]
        self.assertEqual(len(dedupe_streams(streams)), 2)

    def test_to_dict(self) -> None:
        data = Stream(url="https://x/a.m3u8", quality="720p").to_dict()
        self.assertEqual(data["kind"], "hls")
        self.assertEqual(data["quality"], "720p")
        self.assertIn("url", data)


class TestEpisodeAndTitle(unittest.TestCase):
    def test_episode_label(self) -> None:
        self.assertEqual(Episode(number=3).label, "Episode 3")
        self.assertEqual(Episode(number=3, title="Mage Killer").label, "Episode 3 - Mage Killer")
        self.assertEqual(Episode(number=1.5).label, "Episode 1.5")

    def test_episode_best_stream(self) -> None:
        episode = Episode(number=1)
        episode.streams = [
            Stream(url="https://x/480.mp4", quality="480p"),
            Stream(url="https://x/1080.mp4", quality="1080p"),
            Stream(url="https://x/embed", kind="embed"),
        ]
        self.assertEqual(episode.best.quality, "1080p")

    def test_title_key_and_slug(self) -> None:
        title = Title(title="Frieren", provider="demo")
        self.assertEqual(title.slug, "frieren")
        self.assertEqual(title.key, "demo:frieren")
        self.assertEqual(Title(title="X", id="abc").key, "abc")

    def test_sorted_episodes_and_lookup(self) -> None:
        title = Title(title="X", episodes=[Episode(number=2), Episode(number=1), Episode(number=10)])
        self.assertEqual([e.number for e in title.sorted_episodes()], [1, 2, 10])
        self.assertIsNotNone(title.episode(10))
        self.assertIsNone(title.episode(3))

    def test_total_episodes_autofilled(self) -> None:
        title = Title(title="X", episodes=[Episode(number=1), Episode(number=2)])
        self.assertEqual(title.total_episodes, 2)

    def test_to_dict(self) -> None:
        title = Title(title="X", genres=["A"], episodes=[Episode(number=1, title="t")])
        data = title.to_dict()
        self.assertEqual(data["genres"], ["A"])
        self.assertEqual(len(data["episodes"]), 1)
        self.assertEqual(data["episodes"][0]["number"], 1.0)
        self.assertNotIn("episodes", title.to_dict(with_episodes=False))

    def test_search_result(self) -> None:
        result = SearchResult(title="Frieren", provider="demo", year="2023")
        self.assertEqual(result.key, "demo:frieren")
        self.assertIn("2023", str(result))

    def test_history_entry_percent(self) -> None:
        entry = HistoryEntry(title="X", episode=1, position=300, duration=1200)
        self.assertAlmostEqual(entry.percent, 25.0)
        self.assertEqual(HistoryEntry(title="X", episode=1).percent, 0.0)

    def test_download_link_rank(self) -> None:
        self.assertGreater(
            DownloadLink(url="a.mp4", quality="1080p").rank,
            DownloadLink(url="a.mp4", quality="480p").rank,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
