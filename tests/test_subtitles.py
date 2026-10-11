"""Subtitle parsing and conversion."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aninext import subtitles

SRT = """1
00:00:01,000 --> 00:00:03,500
Hello <i>there</i>

2
00:00:04,000 --> 00:00:06,000
Second line
with a wrap
"""

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:03.500
Hello there

2
00:00:04.000 --> 00:00:06.000
Second line
"""

ASS = """[Script Info]
Title: test

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,Hello {\\i1}there{\\i0}
Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,Line1\\NLine2
"""


class TestSubtitles(unittest.TestCase):
    def test_detect(self) -> None:
        self.assertEqual(subtitles.detect_format(SRT), "srt")
        self.assertEqual(subtitles.detect_format(VTT), "vtt")
        self.assertEqual(subtitles.detect_format(ASS), "ass")

    def test_parse_srt(self) -> None:
        cues = subtitles.parse(SRT)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start, 1.0)
        self.assertEqual(cues[0].end, 3.5)
        self.assertEqual(cues[0].text, "Hello there")     # inline tags stripped
        self.assertEqual(cues[1].text, "Second line\nwith a wrap")

    def test_parse_vtt(self) -> None:
        cues = subtitles.parse(VTT)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].end, 3.5)

    def test_parse_ass(self) -> None:
        cues = subtitles.parse(ASS)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "Hello there")      # override tags stripped
        self.assertEqual(cues[1].text, "Line1\nLine2")     # \N becomes a newline

    def test_timestamps(self) -> None:
        self.assertAlmostEqual(subtitles.parse_timestamp("00:01:02,500"), 62.5)
        self.assertAlmostEqual(subtitles.parse_timestamp("1:02.5"), 62.5)
        self.assertEqual(subtitles.format_timestamp(62.5), "00:01:02,500")
        self.assertEqual(subtitles.format_timestamp(62.5, comma=False), "00:01:02.500")
        self.assertEqual(subtitles.format_timestamp(3661.25), "01:01:01,250")

    def test_to_srt_and_vtt(self) -> None:
        srt = subtitles.to_srt(VTT, "vtt")
        self.assertIn("00:00:01,000 --> 00:00:03,500", srt)
        self.assertTrue(srt.startswith("1\n"))
        vtt = subtitles.to_vtt(SRT, "srt")
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("00:00:01.000 --> 00:00:03.500", vtt)

    def test_shift(self) -> None:
        shifted = subtitles.to_srt(SRT, "srt", shift=2.0)
        self.assertIn("00:00:03,000 --> 00:00:05,500", shifted)
        negative = subtitles.to_srt(SRT, "srt", shift=-5.0)
        self.assertIn("00:00:00,000 --> 00:00:00,050", negative)  # clamped, keeps ordering

    def test_stats(self) -> None:
        info = subtitles.stats(SRT)
        self.assertEqual(info["cues"], 2)
        self.assertGreater(info["words"], 5)
        self.assertGreater(info["reading_speed"], 0)

    def test_convert_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub.vtt"
            path.write_text(VTT, "utf-8")
            out = subtitles.convert_file(path, to="srt")
            self.assertEqual(out.suffix, ".srt")
            self.assertIn("-->", out.read_text("utf-8"))
            back = subtitles.convert_file(out, to="vtt")
            self.assertTrue(back.read_text("utf-8").startswith("WEBVTT"))

    def test_no_cues(self) -> None:
        self.assertEqual(subtitles.parse("just some text"), [])
        self.assertEqual(subtitles.stats("nothing")["cues"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
