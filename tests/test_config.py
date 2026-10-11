"""Config loading, coercion, environment overrides and persistence."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aninext.config import DEFAULTS, Config, _coerce, download_dir, get_config, set_config


class TestConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self._saved_env = dict(os.environ)
        for key in list(os.environ):
            if key.startswith("ANINEXT_"):
                os.environ.pop(key, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)
        set_config(None)  # type: ignore[arg-type]
        self.tmp.cleanup()

    # -- loading -----------------------------------------------------------
    def test_defaults_when_missing(self) -> None:
        cfg = Config.load(self.path)
        self.assertEqual(cfg["quality"], DEFAULTS["quality"])
        self.assertEqual(cfg["provider"], "auto")
        self.assertEqual(cfg.get_int("search_limit", 0), 24)

    def test_reads_file_and_keeps_unknown_keys(self) -> None:
        self.path.write_text(json.dumps({"quality": "720p", "custom": 7}), "utf-8")
        cfg = Config.load(self.path)
        self.assertEqual(cfg["quality"], "720p")
        self.assertEqual(cfg["custom"], 7)

    def test_broken_json_falls_back_to_defaults(self) -> None:
        self.path.write_text("{ not json", "utf-8")
        cfg = Config.load(self.path)
        self.assertEqual(cfg["quality"], DEFAULTS["quality"])

    def test_empty_file(self) -> None:
        self.path.write_text("   \n", "utf-8")
        cfg = Config.load(self.path)
        self.assertEqual(cfg["provider"], "auto")

    # -- persistence -------------------------------------------------------
    def test_save_roundtrip(self) -> None:
        cfg = Config.load(self.path)
        cfg.set("quality", "480p")
        cfg.set("search_limit", "12")
        cfg.save()
        raw = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(raw["quality"], "480p")
        self.assertEqual(raw["search_limit"], 12)
        again = Config.load(self.path)
        self.assertEqual(again["quality"], "480p")

    # -- environment -------------------------------------------------------
    def test_env_overrides_file(self) -> None:
        self.path.write_text(json.dumps({"quality": "1080p", "workers": 2}), "utf-8")
        os.environ["ANINEXT_QUALITY"] = "720p"
        os.environ["ANINEXT_WORKERS"] = "6"
        cfg = Config.load(self.path)
        self.assertEqual(cfg["quality"], "720p")
        self.assertEqual(cfg["workers"], 6)

    def test_env_ignores_empty_values(self) -> None:
        os.environ["ANINEXT_QUALITY"] = ""
        cfg = Config.load(self.path)
        self.assertEqual(cfg["quality"], DEFAULTS["quality"])

    # -- accessors ---------------------------------------------------------
    def test_get_helpers(self) -> None:
        cfg = Config({"workers": "8", "rate_limit": "1.5", "wake_lock": "yes",
                      "subtitles": "en, es , ja"}, self.path)
        self.assertEqual(cfg.get_int("workers"), 8)
        self.assertEqual(cfg.get_int("nope", 3), 3)
        self.assertEqual(cfg.get_float("rate_limit"), 1.5)
        self.assertTrue(cfg.get_bool("wake_lock"))
        self.assertFalse(cfg.get_bool("nope"))
        self.assertEqual(cfg.get_list("subtitles"), ["en", "es", "ja"])
        self.assertEqual(cfg.get_list("nope", ["en"]), ["en"])

    def test_get_int_bad_value(self) -> None:
        cfg = Config({"workers": "many"}, self.path)
        self.assertEqual(cfg.get_int("workers", 4), 4)

    def test_set_coerces_types(self) -> None:
        cfg = Config({}, self.path)
        cfg.set("wake_lock", "off")
        cfg.set("workers", "3")
        cfg.set("rate_limit", "0.5")
        cfg.set("subtitles", "en,ja")
        self.assertIs(cfg["wake_lock"], False)
        self.assertEqual(cfg["workers"], 3)
        self.assertEqual(cfg["rate_limit"], 0.5)
        self.assertEqual(cfg["subtitles"], ["en", "ja"])
        self.assertTrue(cfg._dirty)

    def test_set_unknown_key_accepted(self) -> None:
        cfg = Config({}, self.path)
        cfg.set("something_new", "x")
        self.assertEqual(cfg["something_new"], "x")

    def test_provider_config_merges_globals(self) -> None:
        cfg = Config({"workers": 5, "timeout": 10, "retries": 1,
                      "providers": {"demo": {"enabled": False, "timeout": 99}}}, self.path)
        merged = cfg.provider_config("demo")
        self.assertFalse(merged["enabled"])
        self.assertEqual(merged["timeout"], 99)
        self.assertEqual(merged["worker_count"], 5)
        self.assertEqual(merged["retries"], 1)
        other = cfg.provider_config("anilist")
        self.assertEqual(other["timeout"], 10)
        self.assertNotIn("enabled", other)

    def test_reset(self) -> None:
        cfg = Config({"quality": "480p"}, self.path)
        cfg.reset()
        self.assertEqual(cfg["quality"], DEFAULTS["quality"])

    def test_describe(self) -> None:
        text = Config({}, self.path).describe()
        self.assertIn("config file", text)
        self.assertIn("quality", text)

    # -- process-wide singleton -------------------------------------------
    def test_get_config_caches_and_reloads(self) -> None:
        os.environ["ANINEXT_HOME"] = self.tmp.name
        first = get_config(reload=True)
        self.assertIs(first, get_config())
        self.path.write_text(json.dumps({"quality": "360p"}), "utf-8")
        self.assertEqual(get_config(reload=True)["quality"], "360p")

    def test_download_dir_prefers_config(self) -> None:
        os.environ["ANINEXT_HOME"] = self.tmp.name
        cfg = get_config(reload=True)
        target = Path(self.tmp.name) / "videos"
        cfg.set("download_dir", str(target))
        set_config(cfg)
        self.assertEqual(download_dir(), str(target))

    def test_download_dir_falls_back_to_default(self) -> None:
        os.environ["ANINEXT_HOME"] = self.tmp.name
        cfg = get_config(reload=True)
        cfg.set("download_dir", "")
        set_config(cfg)
        self.assertTrue(download_dir())

    def test_coerce(self) -> None:
        self.assertIs(_coerce("true"), True)
        self.assertIs(_coerce("FALSE"), False)
        self.assertEqual(_coerce("42"), 42)
        self.assertEqual(_coerce("1.25"), 1.25)
        self.assertEqual(_coerce(" mpv "), " mpv ")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
