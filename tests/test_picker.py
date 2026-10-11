"""Picker tests: the fzf path and the numbered-prompt fallback.

No real fzf is needed: a stub script stands in for it so the argument
handling, the tab-delimited index protocol, cancellation and multi-select are
all exercised for real.
"""
from __future__ import annotations

import io
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from aninext import picker
from aninext.picker import Pick

FAKE_FZF = '''#!/usr/bin/env python3
"""Stand-in for fzf: echoes the lines listed in $FZF_ECHO (1-based), or exits
with $FZF_EXIT.  Records its argv so tests can inspect the flags we pass."""
import os, sys
with open(os.environ["FZF_ARGV_LOG"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
lines = [l for l in sys.stdin.read().splitlines() if l]
if os.environ.get("FZF_STDIN_LOG"):
    with open(os.environ["FZF_STDIN_LOG"], "a") as fh:
        fh.write("\\n".join(lines) + "\\n")
sys.exit_code = int(os.environ.get("FZF_EXIT", "0"))
if sys.exit_code:
    sys.exit(sys.exit_code)
want = [int(x) for x in os.environ.get("FZF_ECHO", "1").split(",") if x]
out = [lines[i - 1] for i in want if 0 < i <= len(lines)]
sys.stdout.write("\\n".join(out) + ("\\n" if out else ""))
sys.exit(0 if out else 1)
'''


NAMES = ("Alpha Show", "Beta Show", "Gamma Show", "Delta Show", "Epsilon Show")


def entries(count: int = 3) -> list[Pick]:
    return [
        Pick(key=f"k{i}", label=f"{NAMES[i - 1]}  [demo]", preview=f"synopsis {i}",
             search=f"extra {i}")
        for i in range(1, count + 1)
    ]


class TestPick(unittest.TestCase):
    def test_line_hides_the_search_column(self) -> None:
        line = Pick(key="1", label="Frieren 2023", search="anime demo").line(0)
        self.assertEqual(line, "0\tFrieren 2023 anime demo")
        self.assertEqual(Pick(key="1", label="X").line(4), "4\tX")

    def test_describe_entries(self) -> None:
        self.assertEqual(picker.describe_entries(entries(2)),
                         ["1. Alpha Show  [demo]", "2. Beta Show  [demo]"])


class TestSelectionParsing(unittest.TestCase):
    def test_single_numbers(self) -> None:
        self.assertEqual(picker._parse_selection("2", 5, multi=False), [1])
        self.assertEqual(picker._parse_selection(" 3 ", 5, multi=False), [2])

    def test_lists_and_ranges(self) -> None:
        self.assertEqual(picker._parse_selection("1,3", 5, multi=True), [0, 2])
        self.assertEqual(picker._parse_selection("2-4", 5, multi=True), [1, 2, 3])
        self.assertEqual(picker._parse_selection("4-2", 5, multi=True), [3, 2, 1])
        self.assertEqual(picker._parse_selection("1 , 2-3", 5, multi=True), [0, 1, 2])

    def test_all_and_empty(self) -> None:
        self.assertEqual(picker._parse_selection("all", 3, multi=True), [0, 1, 2])
        self.assertEqual(picker._parse_selection("", 3, multi=True), [])
        self.assertEqual(picker._parse_selection("*", 2, multi=True), [0, 1])

    def test_out_of_range_is_ignored(self) -> None:
        self.assertEqual(picker._parse_selection("9", 3, multi=True), [])
        self.assertEqual(picker._parse_selection("2,9", 3, multi=True), [1])

    def test_non_numeric_is_not_a_selection(self) -> None:
        self.assertIsNone(picker._parse_selection("frieren", 3, multi=True))
        self.assertIsNone(picker._parse_selection("1-frieren", 3, multi=True))

    def test_single_mode_keeps_only_the_first(self) -> None:
        self.assertEqual(picker._parse_selection("2,3", 5, multi=False), [1])


class TestNumberedPrompt(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(picker, "interactive", return_value=True)
        self.addCleanup(patcher.stop)
        patcher.start()
        stdout = mock.patch("sys.stdout", new_callable=io.StringIO)
        self.stdout = stdout.start()
        self.addCleanup(stdout.stop)

    def _answers(self, *answers: str):
        queue = list(answers)
        calls: list[str] = []

        def fake_ask(prompt: str) -> str:
            calls.append(prompt)
            if not queue:
                raise EOFError
            return queue.pop(0)

        patcher = mock.patch.object(picker, "_ask", side_effect=fake_ask)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_picks_a_number(self) -> None:
        self._answers("2")
        picked = picker.pick_one(entries(3), prompt="title")
        self.assertIsNotNone(picked)
        self.assertEqual(picked.key, "k2")

    def test_empty_answer_cancels(self) -> None:
        self._answers("")
        self.assertIsNone(picker.pick_one(entries(3), prompt="title"))

    def test_filter_matching_one_entry_selects_it(self) -> None:
        calls = self._answers("beta")
        picked = picker.pick_one(entries(5), prompt="title")
        self.assertIsNotNone(picked)
        self.assertEqual(picked.key, "k2")
        self.assertEqual(len(calls), 1)

    def test_filter_matching_several_shows_a_shortlist(self) -> None:
        calls = self._answers("show", "3")       # filter, then pick from the shortlist
        picked = picker.pick_one(entries(5), prompt="title")
        self.assertIsNotNone(picked)
        self.assertEqual(picked.key, "k3")
        self.assertEqual(len(calls), 2)
        self.assertIn("Gamma Show", self.stdout.getvalue())   # shortlist was printed

    def test_garbage_is_retried_then_gives_up(self) -> None:
        self._answers("nope", "nope")
        self.assertIsNone(picker.pick_one(entries(3), prompt="title"))

    def test_multi_select(self) -> None:
        self._answers("1,3")
        picked = picker.pick_many(entries(3), prompt="episodes")
        self.assertEqual([p.key for p in picked], ["k1", "k3"])

    def test_range_selection(self) -> None:
        self._answers("1-2")
        picked = picker.pick_many(entries(4), prompt="episodes")
        self.assertEqual([p.key for p in picked], ["k1", "k2"])

    def test_eof_cancels(self) -> None:
        self._answers()
        self.assertIsNone(picker.pick_one(entries(3), prompt="title"))


class TestFzf(unittest.TestCase):
    def setUp(self) -> None:
        # pretend there is a real keyboard/terminal so the fzf branch is taken
        tty = mock.patch.object(picker, "_tty_path", return_value="/dev/tty")
        tty.start()
        self.addCleanup(tty.stop)
        prompt = mock.patch.object(picker, "interactive", return_value=False)
        prompt.start()
        self.addCleanup(prompt.stop)
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        script = self.bin / "fzf"
        script.write_text(FAKE_FZF, "utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.args = self.dir / "argv.log"
        self.stdin = self.dir / "stdin.log"
        self.env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FZF_ARGV_LOG": str(self.args),
            "FZF_STDIN_LOG": str(self.stdin),
            "FZF_ECHO": "2",
            "HOME": os.environ.get("HOME", ""),
        }
        self._env = mock.patch.dict(os.environ, self.env, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_fzf_is_used_and_parses_the_index(self) -> None:
        picked = picker.pick_one(entries(3), prompt="title", header="header text")
        self.assertIsNotNone(picked)
        self.assertEqual(picked.key, "k2")
        argv = self.args.read_text()
        self.assertIn("--reverse", argv)
        self.assertIn("--delimiter=\t", argv)      # index is a hidden column
        self.assertIn("--with-nth=2..", argv)
        self.assertIn("--prompt title > ", argv)
        self.assertIn("--header header text", argv)
        self.assertIn("--preview", argv)
        # the list we feed it carries the hidden index first
        self.assertTrue(self.stdin.read_text().startswith("0\tAlpha Show"))

    def test_extra_fzf_options_from_the_environment(self) -> None:
        os.environ["ANINEXT_FZF_OPTIONS"] = "--border --cycle"
        self.addCleanup(os.environ.pop, "ANINEXT_FZF_OPTIONS", None)
        picker.pick_one(entries(2), prompt="title")
        self.assertIn("--border --cycle", self.args.read_text())

    def test_cancelling_fzf_returns_none(self) -> None:
        os.environ["FZF_EXIT"] = "130"
        self.assertIsNone(picker.pick_one(entries(3), prompt="title"))

    def test_no_match_returns_none(self) -> None:
        os.environ["FZF_EXIT"] = "1"
        self.assertIsNone(picker.pick_one(entries(3), prompt="title"))

    def test_multi_select_uses_multi_flag(self) -> None:
        os.environ["FZF_ECHO"] = "1,3"
        picked = picker.pick_many(entries(3), prompt="episodes")
        self.assertEqual([p.key for p in picked], ["k1", "k3"])
        self.assertIn("--multi", self.args.read_text())

    def test_preview_files_are_written(self) -> None:
        picker.pick_one(entries(2), prompt="title")
        self.assertIn("--preview", self.args.read_text())
        self.assertIn("cat", self.args.read_text())

    def test_no_preview_when_no_entry_has_one(self) -> None:
        picker.pick_one([Pick(key="1", label="Alpha")], prompt="title")
        self.assertNotIn("--preview", self.args.read_text())

    def test_fzf_can_be_disabled(self) -> None:
        os.environ["ANINEXT_NO_FZF"] = "1"
        self.addCleanup(os.environ.pop, "ANINEXT_NO_FZF", None)
        self.assertFalse(picker.fzf_available())
        self.assertIsNone(picker.pick_one(entries(3), prompt="title"))  # no prompt either


class TestEnvironment(unittest.TestCase):
    def test_non_interactive_never_blocks(self) -> None:
        with mock.patch.object(picker, "interactive", return_value=False), \
             mock.patch.object(picker, "fzf_available", return_value=False):
            self.assertIsNone(picker.pick_one(entries(3), prompt="title"))
            self.assertEqual(picker.pick_many(entries(3), prompt="title"), [])

    def test_empty_entries(self) -> None:
        self.assertIsNone(picker.pick_one([], prompt="title"))

    def test_no_prompt_env(self) -> None:
        os.environ["ANINEXT_NO_PROMPT"] = "1"
        self.addCleanup(os.environ.pop, "ANINEXT_NO_PROMPT", None)
        self.assertFalse(picker.interactive())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
