"""Library, history, watchlist, downloads and cache bookkeeping."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aninext.db import SCHEMA_VERSION, Database
from aninext.models import Episode, HistoryEntry, Title


def make_title(name: str = "Frieren: Beyond Journey's End", **kw) -> Title:
    episodes = kw.pop("episodes", None) or [
        Episode(number=1, title="The Journey's End", url="u1"),
        Episode(number=2, title="It Didn't Have to Be Magic", url="u2"),
    ]
    return Title(
        title=name,
        provider=kw.pop("provider", "demo"),
        kind=kw.pop("kind", "anime"),
        year=kw.pop("year", "2023"),
        score=kw.pop("score", "9.0"),
        genres=kw.pop("genres", ["Fantasy", "Adventure"]),
        alt_titles=kw.pop("alt_titles", ["Sousou no Frieren"]),
        synopsis=kw.pop("synopsis", "An elven mage reflects on the passing of time."),
        episodes=episodes,
        **kw,
    )


class TestDatabase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="aninext-db-")
        self.db = Database(Path(self.tmp) / "test.db")
        self.title = make_title()

    def tearDown(self) -> None:
        self.db.close()

    def test_schema_version_recorded(self) -> None:
        self.assertEqual(self.db.get_meta("schema_version"), str(SCHEMA_VERSION))

    def test_library_add_get_list(self) -> None:
        self.db.library_add(self.title, auto_download=True, tags=["best"])
        row = self.db.library_get(self.title.key)
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], self.title.title)
        self.assertEqual(row["total_eps"], 2)
        self.assertEqual(row["tags"], ["best"])
        self.assertEqual(row["auto_download"], 1)
        self.assertEqual(len(self.db.library_list()), 1)
        self.assertEqual(len(self.db.library_list(kind="movie")), 0)

    def test_library_update_keeps_added_at(self) -> None:
        self.db.library_add(self.title)
        first = self.db.library_get(self.title.key)["added_at"]
        self.title.title = "Frieren (S2)"
        self.db.library_add(self.title)
        row = self.db.library_get(self.title.key)
        self.assertEqual(row["added_at"], first)
        self.assertEqual(row["title"], "Frieren (S2)")

    def test_library_search_full_text(self) -> None:
        self.db.library_add(self.title)
        self.assertEqual(len(self.db.search_library("frieren")), 1)
        self.assertEqual(len(self.db.search_library("elven mage")), 1)     # synopsis
        self.assertEqual(len(self.db.search_library("sousou")), 1)         # alt title
        self.assertEqual(len(self.db.search_library("fantasy")), 1)        # genre
        self.assertEqual(len(self.db.search_library("bleach")), 0)

    def test_library_find_partial(self) -> None:
        self.db.library_add(self.title)
        self.assertEqual(len(self.db.library_find("frier")), 1)
        self.assertEqual(len(self.db.library_find("nope")), 0)

    def test_library_remove_cascades(self) -> None:
        self.db.library_add(self.title)
        self.db.episode_mark(self.title.key, 1, watched=True)
        self.db.library_remove(self.title.key)
        self.assertEqual(self.db.library_list(), [])
        self.assertEqual(self.db.episodes_list(self.title.key), [])
        self.assertEqual(self.db.search_library("frieren"), [])

    def test_episodes(self) -> None:
        self.db.library_add(self.title)
        rows = self.db.episodes_list(self.title.key)
        self.assertEqual([r["number"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["title"], "The Journey's End")
        self.assertEqual(self.db.next_unwatched(self.title.key), 1.0)
        self.db.episode_mark(self.title.key, 1, watched=True, progress=120, duration=1450)
        self.assertEqual(self.db.next_unwatched(self.title.key), 2.0)
        self.assertEqual(self.db.episode_progress(self.title.key)[(0, 1.0)]["progress"], 120)

    def test_episodes_upsert_is_idempotent(self) -> None:
        self.db.library_add(self.title)
        self.db.episodes_upsert(self.title)
        self.assertEqual(len(self.db.episodes_list(self.title.key)), 2)

    def test_history(self) -> None:
        self.db.library_add(self.title)
        self.db.history_add(HistoryEntry(title=self.title.title, episode=1, title_key=self.title.key,
                                         provider="demo", position=100, duration=1400))
        rows = self.db.history_list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["episode"], 1.0)
        self.assertEqual(rows[0]["position"], 100.0)
        last = self.db.history_last(self.title.key)
        self.assertIsNotNone(last)
        self.assertEqual(self.db.history_clear(self.title.key), 1)
        self.assertEqual(self.db.history_list(), [])

    def test_continue_watching(self) -> None:
        self.db.library_add(self.title)
        self.db.history_add({"title": self.title.title, "episode": 1, "title_key": self.title.key})
        rows = self.db.continue_watching()
        self.assertEqual(len(rows), 1)
        # watching episode 1 marks it seen, so the show resumes at episode 2
        self.assertEqual(rows[0]["next_episode"], 2.0)
        self.assertEqual(rows[0]["kind"], "anime")  # joined from the library
        self.assertEqual(rows[0]["title_key"], self.title.key)

    def test_watchlist(self) -> None:
        self.db.watchlist_add("demo:x", "Severance", status="watching")
        self.assertEqual(len(self.db.watchlist_list()), 1)
        self.assertEqual(len(self.db.watchlist_list("watching")), 1)
        self.assertEqual(len(self.db.watchlist_list("plan")), 0)
        self.assertEqual(self.db.watchlist_status("demo:x"), "watching")
        self.db.watchlist_bump("demo:x", 3)
        row = self.db.watchlist_list()[0]
        self.assertEqual(row["position"], 3)
        self.db.watchlist_remove("demo:x")
        self.assertEqual(self.db.watchlist_list(), [])

    def test_downloads(self) -> None:
        dl_id = self.db.download_add(title_key="demo:x", title="Severance", episode=1,
                                     quality="1080p", url="http://x/y.mp4")
        self.db.download_update(dl_id, status="done", path="/tmp/y.mp4", size=1234)
        rows = self.db.downloads_list()
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(rows[0]["size"], 1234)
        self.assertEqual(self.db.download_find(title_key="demo:x", episode=1)["path"], "/tmp/y.mp4")
        self.assertEqual(len(self.db.downloads_list(status="failed")), 0)
        self.db.download_delete(dl_id)
        self.assertEqual(self.db.downloads_list(), [])

    def test_stats_and_reset(self) -> None:
        self.db.library_add(self.title)
        self.db.watchlist_add("demo:x", "X")
        stats = self.db.stats()
        self.assertEqual(stats["library"], 1)
        self.assertEqual(stats["episodes"], 2)
        self.db.reset()
        self.assertEqual(self.db.stats()["library"], 0)

    def test_fts_matches_prefix(self) -> None:
        self.db.library_add(make_title("Blade Runner 2049", kind="movie"))
        self.assertEqual(len(self.db.search_library("blade")), 1)

    def test_shared_connection_per_thread(self) -> None:
        import threading

        errors: list[Exception] = []

        def worker() -> None:
            try:
                self.db.library_list()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestSeasonAwareProgress(unittest.TestCase):
    """Episode numbers restart every season, so watched state is per season."""

    def setUp(self) -> None:
        self.dir = TemporaryDirectory()
        self.db = Database(Path(self.dir.name) / "seasons.db")
        self.title = Title(title="Severance", provider="demo", id="demo:severance")
        self.db.library_add(self.title)
        for season, count in ((1, 5), (2, 4)):
            for number in range(1, count + 1):
                self.db.episode_mark(self.title.key, number, season=season, watched=False)

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def test_fresh_show_starts_at_the_first_season(self) -> None:
        self.assertEqual(self.db.next_unwatched(self.title.key), 1.0)

    def test_continues_the_season_being_watched(self) -> None:
        self.db.episode_mark(self.title.key, 1, season=1, watched=True)
        self.assertEqual(self.db.next_unwatched(self.title.key), 2.0)
        for number in range(1, 6):
            self.db.episode_mark(self.title.key, number, season=1, watched=True)
        self.assertEqual(self.db.next_unwatched(self.title.key), 1.0)        # season 2 starts
        self.db.episode_mark(self.title.key, 1, season=2, watched=True)
        self.assertEqual(self.db.next_unwatched(self.title.key), 2.0)        # ... and keeps going

    def test_explicit_season_stays_inside_it(self) -> None:
        for number in range(1, 6):
            self.db.episode_mark(self.title.key, number, season=1, watched=True)
        self.assertEqual(self.db.next_unwatched(self.title.key, season=1), 6.0)
        self.assertEqual(self.db.next_unwatched(self.title.key, season=2), 1.0)

    def test_progress_is_keyed_by_season_and_number(self) -> None:
        self.db.episode_mark(self.title.key, 1, season=2, progress=120, duration=1440)
        progress = self.db.episode_progress(self.title.key)
        self.assertEqual(progress[(2, 1.0)]["progress"], 120)
        self.assertEqual(progress[(1, 1.0)]["progress"], 0)
        self.assertIn((1, 5.0), progress)

    def test_two_seasons_do_not_overwrite_each_other(self) -> None:
        self.db.episodes_upsert(Title(
            title="Severance", provider="demo", id="demo:severance",
            episodes=[Episode(number=1, season=1, title="S1 one"),
                      Episode(number=1, season=2, title="S2 one")],
        ))
        rows = self.db.query(
            "SELECT season, number, title FROM episodes WHERE key=? AND title IS NOT NULL "
            "ORDER BY season, number",
            (self.title.key,),
        )
        self.assertEqual([(r["season"], r["number"], r["title"]) for r in rows],
                         [(1, 1.0, "S1 one"), (2, 1.0, "S2 one")])
