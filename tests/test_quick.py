"""The `aninext <name>` flow: argv rewriting, pickers, seasons and playback.

Pickers are replaced with deterministic stubs so the whole flow can be walked
through offline against the demo provider - including the season step, which
the demo catalogue now has (Severance, two seasons).
"""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from aninext import cli, picker
from aninext.config import get_config, set_config
from aninext.db import reset_db
from aninext.models import Episode, Title


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class TestArgvRewriting(unittest.TestCase):
    def test_a_name_becomes_a_search(self) -> None:
        self.assertEqual(cli._rewrite_argv(["frieren"]), ["quick", "frieren"])
        self.assertEqual(cli._rewrite_argv(["spy", "x", "family"]),
                         ["quick", "spy", "x", "family"])

    def test_commands_are_left_alone(self) -> None:
        for argv in (["search", "frieren"], ["info", "demo:frieren"], ["doctor"],
                     ["library", "add", "x"], ["--help"], ["--version"], ["trending"]):
            self.assertEqual(cli._rewrite_argv(list(argv)), argv)

    def test_global_flags_before_or_after(self) -> None:
        self.assertEqual(cli._rewrite_argv(["-p", "demo", "frieren"]),
                         ["-p", "demo", "quick", "frieren"])
        self.assertEqual(cli._rewrite_argv(["-p", "demo", "search", "x"]),
                         ["-p", "demo", "search", "x"])
        self.assertEqual(cli._rewrite_argv(["--json", "frieren"]),
                         ["--json", "quick", "frieren"])
        self.assertEqual(cli._rewrite_argv(["--provider=demo", "frieren"]),
                         ["--provider=demo", "quick", "frieren"])
        self.assertEqual(cli._rewrite_argv(["-q", "frieren"]), ["-q", "quick", "frieren"])

    def test_flags_after_the_name_stay_there(self) -> None:
        self.assertEqual(cli._rewrite_argv(["frieren", "-e", "2"]),
                         ["quick", "frieren", "-e", "2"])

    def test_nothing_to_rewrite(self) -> None:
        self.assertEqual(cli._rewrite_argv([]), [])


class TestSeasonChoices(unittest.TestCase):
    def test_provider_seasons_win(self) -> None:
        title = Title(title="Severance", provider="tmdb", kind="series", seasons=[
            {"season": 0, "name": "Specials", "episodes": 3, "year": ""},
            {"season": 1, "name": "Season 1", "episodes": 9, "year": "2022"},
            {"season": 2, "name": "Season 2", "episodes": 10, "year": "2025"},
        ])
        seasons = cli._season_choices(title)
        self.assertEqual([s["season"] for s in seasons], [1, 2])
        self.assertEqual(seasons[1]["episodes"], 10)

    def test_single_season_is_not_a_choice(self) -> None:
        title = Title(title="Frieren", episodes=[Episode(number=1), Episode(number=2)])
        self.assertEqual(cli._season_choices(title), [])

    def test_seasons_derived_from_episodes(self) -> None:
        title = Title(title="X", episodes=[
            Episode(number=1, season=1), Episode(number=2, season=1),
            Episode(number=1, season=2), Episode(number=2, season=2), Episode(number=3, season=2),
        ])
        seasons = cli._season_choices(title)
        self.assertEqual([(s["season"], s["episodes"]) for s in seasons], [(1, 2), (2, 3)])
        self.assertEqual(seasons[0]["name"], "Season 1")


class TestEpisodePicks(unittest.TestCase):
    def setUp(self) -> None:
        reset_db()
        self.title = Title(title="Frieren", provider="demo", id="frieren", episodes=[
            Episode(number=n, title=f"Episode {n}", season=1) for n in range(1, 6)
        ])

    def tearDown(self) -> None:
        reset_db()

    def test_newest_first_with_watched_markers(self) -> None:
        from aninext.db import get_db

        db = get_db()
        db.library_add(self.title)
        db.episode_mark(self.title.key, 1, season=1)
        picks = cli._episode_picks(self.title, list(self.title.episodes))
        self.assertEqual([p.key for p in picks], ["5", "4", "3", "2", "1"])  # newest first
        by_key = {p.key: p.label for p in picks}
        self.assertIn("watched", by_key["1"])
        self.assertIn("next up", by_key["2"])
        self.assertIn("Episode 5", by_key["5"])

    def test_works_without_a_database(self) -> None:
        with mock.patch("aninext.db.get_db", side_effect=RuntimeError("no db")):
            picks = cli._episode_picks(self.title, list(self.title.episodes))
        self.assertEqual(len(picks), 5)


class TestQuickFlow(unittest.TestCase):
    """Drive `aninext <name>` end to end with stubbed pickers."""

    def setUp(self) -> None:
        reset_db()
        set_config(None)  # type: ignore[arg-type]
        cfg = get_config(reload=True)
        cfg.set("colour", "never")
        cfg.set("wake_lock", False)
        cfg.set("history", True)
        set_config(cfg)
        self.played: list[dict] = []

        def fake_play(title, episode, stream, **kwargs):
            self.played.append({
                "title": title.title,
                "episode": episode.number,
                "season": episode.season,
                "url": episode.url,
                "stream": stream.url,
                "mark": kwargs.get("mark_watched"),
            })
            return True

        patcher = mock.patch("aninext.player.play_episode", side_effect=fake_play)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.choices: list[str | None] = []      # what each picker returns (by index)

    def _stub_pickers(self, *keys: str | None) -> list[list[picker.Pick]]:
        """Answer each picker call in order with the entry whose key matches."""
        seen: list[list[picker.Pick]] = []
        queue = list(keys)

        def matches(entry, want: str) -> bool:
            if entry.key == want or entry.key.endswith(want):
                return True
            return want.split(":")[-1] in entry.key        # "demo:frieren" -> ".../anime/frieren"

        def fake_pick_one(entries, **kwargs):
            entries = list(entries)
            seen.append(entries)
            # answers are given in order; ones that do not fit this picker were
            # meant for a picker that got skipped (a single search result, -e, -s)
            while queue:
                want = queue.pop(0)
                if want is None:
                    return None
                for entry in entries:
                    if matches(entry, want):
                        return entry
            return None

        for name in ("pick_one",):
            patcher = mock.patch.object(picker, name, side_effect=fake_pick_one)
            patcher.start()
            self.addCleanup(patcher.stop)
        return seen

    def _interactive(self, value: bool = True):
        patcher = mock.patch.object(picker, "interactive", return_value=value)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- the happy path ----------------------------------------------------
    def test_pick_title_then_episode_then_play(self) -> None:
        self._interactive(True)
        seen = self._stub_pickers("demo:frieren", "3")
        code, out, err = run("-p", "demo", "frieren")
        self.assertEqual(code, 0, err)
        # one search hit, so there was no title picker - straight to the episodes
        self.assertEqual(len(seen), 1)
        self.assertEqual([p.key for p in seen[0]], ["8", "7", "6", "5", "4", "3", "2", "1"])
        self.assertEqual(self.played[0]["episode"], 3.0)
        self.assertIn("frieren-3", self.played[0]["stream"])
        self.assertTrue(self.played[0]["url"].endswith("/watch/frieren-3"))
        self.assertTrue(self.played[0]["mark"])

    def test_several_results_show_a_picker(self) -> None:
        self._interactive(True)
        seen = self._stub_pickers("demo:blade-runner-2049", "1")
        code, _, err = run("-p", "demo", "e", "-k", "all")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(seen), 2)
        self.assertGreater(len(seen[0]), 1)          # "e" matches several demo titles
        self.assertIn("Blade Runner 2049", seen[0][2].label)
        self.assertEqual(self.played[0]["title"], "Blade Runner 2049")

    def test_season_step_only_when_several_seasons(self) -> None:
        self._interactive(True)
        seen = self._stub_pickers("demo:severance", "2", "2")
        code, out, err = run("-p", "demo", "severance")
        self.assertEqual(code, 0, err)
        # the first picker was the season step, and it offered both seasons
        self.assertEqual([p.key for p in seen[0]], ["1", "2"])
        self.assertIn("Season 2", seen[0][1].label)
        # the second only listed season 2 episodes, so "2" there is S2E2
        self.assertEqual([p.key for p in seen[1]], ["4", "3", "2", "1"])
        self.assertEqual((self.played[0]["season"], self.played[0]["episode"]), (2, 2.0))
        self.assertTrue(self.played[0]["url"].endswith("/watch/severance-s2e2"))

    def test_episode_flag_alone_plays_from_the_first_season(self) -> None:
        self._interactive(True)
        seen = self._stub_pickers()
        code, _, err = run("-p", "demo", "severance", "-e", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, [])                       # no pickers at all
        self.assertEqual((self.played[0]["season"], self.played[0]["episode"]), (1, 2.0))

    def test_reference_is_not_searched(self) -> None:
        self._interactive(True)
        seen = self._stub_pickers("2", "2")     # season 2, then its episode 2
        code, _, err = run("-p", "demo", "demo:severance")
        self.assertEqual(code, 0, err)
        self.assertIn("loaded Severance", err)
        self.assertEqual([p.key for p in seen[0]], ["1", "2"])   # straight to the seasons
        self.assertEqual((self.played[0]["season"], self.played[0]["episode"]), (2, 2.0))

    def test_season_flag_skips_the_season_picker(self) -> None:
        self._interactive(True)
        seen = self._stub_pickers("demo:severance", "3")
        code, _, err = run("-p", "demo", "severance", "-s", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual([p.key for p in seen[0]], ["4", "3", "2", "1"])
        self.assertEqual((self.played[0]["season"], self.played[0]["episode"]), (2, 3.0))
        self.assertTrue(self.played[0]["url"].endswith("/watch/severance-s2e3"))

    def test_episode_flag_skips_both_pickers(self) -> None:
        self._interactive(True)
        seen = self._stub_pickers("demo:severance")
        code, _, err = run("-p", "demo", "severance", "-e", "1", "-s", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, [])                    # nothing had to be picked
        self.assertEqual((self.played[0]["season"], self.played[0]["episode"]), (2, 1.0))
        self.assertTrue(self.played[0]["url"].endswith("/watch/severance-s2e1"))

    def test_single_result_skips_the_title_picker(self) -> None:
        self._interactive(True)
        seen = self._stub_pickers("5")
        code, _, err = run("-p", "demo", "frieren")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(seen), 1)                # straight to the episodes
        self.assertEqual(self.played[0]["episode"], 5.0)
        self.assertIn("found Frieren: Beyond Journey's End", err)

    def test_cancelling_the_title_picker_changes_nothing(self) -> None:
        self._interactive(True)
        self._stub_pickers(None)
        code, out, _ = run("-p", "demo", "e")
        self.assertEqual(code, 0)
        self.assertEqual(self.played, [])
        self.assertIn("cancelled", out)

    def test_cancelling_the_episode_picker_changes_nothing(self) -> None:
        self._interactive(True)
        self._stub_pickers("demo:frieren", None)
        code, _, _ = run("-p", "demo", "frieren")
        self.assertEqual(code, 0)
        self.assertEqual(self.played, [])

    def test_no_play_prints_the_links(self) -> None:
        self._interactive(True)
        self._stub_pickers("demo:frieren", "1")
        code, out, err = run("-p", "demo", "frieren", "--no-play")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.played, [])
        self.assertIn("Streams", out)
        self.assertIn("1080p", out)
        self.assertIn("Subtitles", out)

    # -- non-interactive ----------------------------------------------------
    def test_piping_falls_back_to_the_result_list(self) -> None:
        self._interactive(False)
        code, out, _ = run("-p", "demo", "frieren")
        self.assertEqual(code, 0)
        self.assertIn("Frieren: Beyond Journey's End", out)
        self.assertEqual(self.played, [])

    def test_json_is_machine_readable_and_never_prompts(self) -> None:
        self._interactive(True)          # even with a tty, --json stays a search
        code, out, _ = run("-p", "demo", "frieren", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data[0]["provider"], "demo")
        self.assertEqual(self.played, [])

    def test_no_hits_exits_one(self) -> None:
        self._interactive(False)
        code, out, err = run("-p", "demo", "zzzznothing")
        self.assertEqual(code, 1)
        self.assertIn("no results", err.lower())

    def test_missing_query_is_an_error(self) -> None:
        code, _, err = run("quick", "")
        self.assertEqual(code, 2)
        self.assertIn("what should i look for", err.lower())


class TestResumeAndPlayerOptions(unittest.TestCase):
    def test_resume_position_is_read_from_history(self) -> None:
        from aninext import player
        from aninext.db import get_db
        from aninext.models import HistoryEntry

        reset_db()
        title = Title(title="Frieren", provider="demo", id="frieren")
        get_db().history_add(HistoryEntry(title=title.title, episode=4, position=600, duration=1440,
                                          title_key=title.key))
        self.assertEqual(player.resume_position(title.key, 4), 600_000)
        self.assertEqual(player.resume_position(title.key, 5), 0)
        reset_db()

    def test_almost_finished_episodes_do_not_resume(self) -> None:
        from aninext import player
        from aninext.db import get_db
        from aninext.models import HistoryEntry

        reset_db()
        title = Title(title="X", provider="demo", id="x")
        get_db().history_add(HistoryEntry(title="X", episode=1, position=1440, duration=1440,
                                          title_key=title.key))
        self.assertEqual(player.resume_position(title.key, 1), 0)
        reset_db()

    def test_mpv_android_is_preferred(self) -> None:
        from aninext import player
        from aninext.models import Stream

        stream = Stream(url="https://cdn.example/frieren-1.mp4", kind="mp4", quality="1080p")
        with mock.patch("aninext.android.play_in_mpv_android", return_value=True) as mpv, \
             mock.patch("aninext.paths.which", return_value=None):
            self.assertTrue(player.play_stream(stream, title="Frieren - Episode 1"))
        self.assertEqual(mpv.call_args[0][0], stream.url)
        self.assertEqual(mpv.call_args[1]["title"], "Frieren - Episode 1")

    def test_falls_back_when_the_app_is_missing(self) -> None:
        from aninext import player
        from aninext.models import Stream

        stream = Stream(url="https://cdn.example/frieren-1.mp4", kind="mp4", quality="1080p")
        with mock.patch("aninext.android.play_in_mpv_android", return_value=False), \
             mock.patch("aninext.termux.open_url", return_value=True) as open_url:
            self.assertTrue(player.play_stream(stream))
        open_url.assert_called_once()

    def test_subs_and_position_reach_the_intent(self) -> None:
        from aninext import android

        with mock.patch.object(android, "am_binary", return_value="am"):
            cmd = android.build_mpv_intent("https://x/a.mp4", subs=["https://x/en.vtt"],
                                           position_ms=5000)
        self.assertIn("https://x/en.vtt", cmd)
        self.assertIn("5000", cmd)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
