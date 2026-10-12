"""End-to-end CLI tests against the built-in offline demo provider."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path

from aninext import cli
from aninext.config import get_config, set_config
from aninext.db import reset_db
from aninext.models import Episode, Stream, Title
from aninext.paths import home


LAST_ERR = ""


def run(*argv: str) -> tuple[int, str]:
    """Run the CLI in-process; returns (exit code, stdout). Log lines are kept
    in ``LAST_ERR`` because they are written to stderr."""
    global LAST_ERR
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:      # argparse: `--help`, `--version`, bad args
            code = int(exc.code or 0)
    LAST_ERR = err.getvalue()
    return code, buf.getvalue()


class TestCliHelpers(unittest.TestCase):
    def test_parse_episode_spec(self) -> None:
        title = Title(title="X", episodes=[Episode(number=n) for n in range(1, 13)])
        self.assertEqual(cli._parse_episode_spec("1-5,8", title), [1, 2, 3, 4, 5, 8])
        self.assertEqual(cli._parse_episode_spec("3-1", title), [3, 2, 1])
        self.assertEqual(cli._parse_episode_spec("all", title), [float(n) for n in range(1, 13)])
        self.assertEqual(cli._parse_episode_spec("2, 4 ,6", title), [2, 4, 6])
        self.assertEqual(cli._parse_episode_spec("bogus", title), [])

    def test_pick_stream(self) -> None:
        streams = [
            Stream(url="https://x/480.mp4", quality="480p"),
            Stream(url="https://x/1080.mp4", quality="1080p"),
            Stream(url="https://x/720.m3u8", quality="720p", kind="hls"),
        ]
        self.assertEqual(cli._pick_stream(streams, "1080p").quality, "1080p")
        self.assertEqual(cli._pick_stream(streams, "best").quality, "1080p")
        self.assertEqual(cli._pick_stream(streams, None, prefer="hls").quality, "720p")
        self.assertIsNone(cli._pick_stream([], "1080p"))


class TestCliCommands(unittest.TestCase):
    def setUp(self) -> None:
        reset_db()
        os.environ["ANINEXT_HOME"] = os.environ["ANINEXT_HOME"]  # set by tests/__init__
        set_config(None)  # type: ignore[arg-type]
        cfg = get_config(reload=True)
        cfg.set("colour", "never")
        cfg.set("wake_lock", False)
        set_config(cfg)
        # each test starts with a clean library/history database
        db_path = home() / "aninext.db"
        if db_path.exists():
            db_path.unlink()
        reset_db()

    def test_search_json(self) -> None:
        code, out = run("search", "frieren", "-p", "demo", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data[0]["title"], "Frieren: Beyond Journey's End")
        self.assertEqual(data[0]["provider"], "demo")

    def test_search_limit_and_empty_result(self) -> None:
        code, out = run("search", "zzzz-nothing", "-p", "demo", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), [])

    def test_search_numbered_then_reference_by_number(self) -> None:
        code, out = run("search", "frieren", "-p", "demo")
        self.assertEqual(code, 0)
        self.assertIn("Frieren", out)
        code, out = run("info", "1", "-p", "demo")
        self.assertEqual(code, 0)
        self.assertIn("Frieren", out)

    def test_info_json(self) -> None:
        code, out = run("info", "demo:frieren", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["kind"], "anime")
        self.assertEqual(data["year"], "2023")
        self.assertEqual(data["total_episodes"], 8)
        self.assertIn("Fantasy", data["genres"])
        self.assertEqual(len(data["episodes"]), 8)

    def test_info_by_url(self) -> None:
        code, out = run("info", "https://demo.aninext.local/anime/frieren", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["title"], "Frieren: Beyond Journey's End")

    def test_info_unknown_provider(self) -> None:
        code, _ = run("info", "nope:frieren", "-p", "demo")
        self.assertEqual(code, 1)

    def test_episodes_json(self) -> None:
        code, out = run("episodes", "demo:frieren", "--json")
        self.assertEqual(code, 0)
        episodes = json.loads(out)
        self.assertEqual(len(episodes), 8)
        self.assertEqual(episodes[0]["number"], 1.0)

    def test_links_json(self) -> None:
        code, out = run("links", "demo:frieren", "-e", "1", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(any(s["kind"] == "hls" for s in data["streams"]))
        self.assertTrue(any(d["quality"] == "1080p" for d in data["downloads"]))
        self.assertTrue(any(s["lang"] == "en" for s in data["subtitles"]))

    def test_links_unknown_episode(self) -> None:
        code, _ = run("links", "demo:frieren", "-e", "99")
        self.assertEqual(code, 1)

    def test_library_roundtrip(self) -> None:
        code, _ = run("library", "add", "demo:frieren")
        self.assertEqual(code, 0)
        self.assertIn("added", LAST_ERR)
        code, out = run("library", "list", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Frieren: Beyond Journey's End")
        code, out = run("library", "search", "frieren", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)), 1)
        code, _ = run("library", "remove", "demo:frieren")
        self.assertEqual(code, 0)
        code, out = run("library", "list", "--json")
        self.assertEqual(json.loads(out), [])

    def test_watchlist_status(self) -> None:
        code, _ = run("watchlist", "add", "demo:severance", "--status", "watching")
        self.assertEqual(code, 0)
        code, out = run("watchlist", "list", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(rows[0]["status"], "watching")
        code, out = run("watchlist", "list", "--status", "plan", "--json")
        self.assertEqual(json.loads(out), [])
        code, _ = run("watchlist", "remove", "demo:severance")
        self.assertEqual(code, 0)

    def test_history_flow(self) -> None:
        code, out = run("history", "list")
        self.assertEqual(code, 0)
        self.assertIn("no history", out)
        from aninext.db import get_db

        db = get_db()
        title = Title(title="Frieren: Beyond Journey's End", provider="demo", id="frieren",
                      episodes=[Episode(number=n) for n in range(1, 9)])
        db.library_add(title)
        db.history_add({"title": title.title, "episode": 3, "title_key": title.key})
        code, out = run("history", "list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["episode"], 3.0)
        code, out = run("history", "continue", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["next_episode"], 1.0)  # nothing watched yet
        code, _ = run("history", "clear")
        self.assertEqual(code, 0)
        code, out = run("history", "list")
        self.assertIn("no history", out)

    def test_downloads_empty(self) -> None:
        code, out = run("downloads", "list")
        self.assertEqual(code, 0)
        self.assertIn("no downloads", out.lower())

    def test_providers_lists_demo(self) -> None:
        code, out = run("providers")
        self.assertEqual(code, 0)
        self.assertIn("demo", out)
        self.assertIn("anilist", out)

    def test_config_set_get(self) -> None:
        code, out = run("config", "get", "quality")
        self.assertEqual(code, 0)
        code, out = run("config", "set", "quality", "720p")
        self.assertEqual(code, 0)
        code, out = run("config", "get", "quality")
        self.assertEqual(out.strip(), "720p")
        code, out = run("config", "path")
        self.assertTrue(out.strip().endswith("config.json"))

    def test_config_json_dump(self) -> None:
        code, out = run("config", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("quality", data)

    def test_cache_report(self) -> None:
        code, out = run("cache")
        self.assertEqual(code, 0)
        self.assertIn("entries", out)

    def test_sources_path(self) -> None:
        code, out = run("sources", "path")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("sources"))

    def test_sources_validate_example(self) -> None:
        example = Path("aninext/sources.d/example.json").resolve()
        self.assertTrue(example.is_file())
        code, out = run("sources", "validate", str(example))
        self.assertEqual(code, 0, out)

    def test_subs_convert(self) -> None:
        path = home() / "clip.vtt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.000\nHi\n", "utf-8")
        code, out = run("subs", str(path), "--to", "srt")
        self.assertEqual(code, 0)
        self.assertTrue((home() / "clip.srt").is_file())

    def test_doctor_runs(self) -> None:
        code, out = run("doctor")
        self.assertIn("python", out.lower())
        self.assertIn("aninext", out.lower())
        self.assertEqual(code, 0)

    def test_global_flags_after_subcommand(self) -> None:
        code, out = run("info", "demo:frieren", "--json", "-p", "demo")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["title"], "Frieren: Beyond Journey's End")

    def test_home_screen_without_command(self) -> None:
        code, out = run()
        self.assertEqual(code, 0)
        self.assertIn("what do you want to watch", out.lower())
        self.assertIn("aninext frieren", out)

    def test_help_flag(self) -> None:
        code, out = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("usage", out.lower())
        self.assertIn("quick", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
