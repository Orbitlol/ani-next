"""Android integration: mpv-android intents and app detection.

The intent API is documented at http://mpv-android.github.io/mpv-android/intent.html
Data comes from the app: `am start -a android.intent.action.VIEW -t video/any
-p is.xyz.mpv -d URL --es title T --ei position MS --esa subs U1,U2`.
"""
from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from aninext import android


class TestPackageDetection(unittest.TestCase):
    def test_fake_packages_env(self) -> None:
        with mock.patch.dict(os.environ, {"ANINEXT_FAKE_PACKAGES": "is.xyz.mpv, org.videolan.vlc"}):
            android._package_cache.clear()
            self.assertTrue(android.package_installed("is.xyz.mpv"))
            self.assertTrue(android.package_installed("org.videolan.vlc"))
            self.assertFalse(android.package_installed("com.example.nope"))

    def test_pm_output_is_parsed(self) -> None:
        output = "package:is.xyz.mpv\npackage:org.videolan.vlc\n"
        proc = mock.Mock(returncode=0, stdout=output)
        with mock.patch.object(android, "_tool", return_value="/system/bin/pm"), \
             mock.patch("subprocess.run", return_value=proc) as run:
            android._package_cache.clear()
            os.environ.pop("ANINEXT_FAKE_PACKAGES", None)
            self.assertTrue(android.package_installed("is.xyz.mpv"))
            self.assertIn("list", run.call_args[0][0])

    def test_no_pm_means_nothing_installed(self) -> None:
        with mock.patch.object(android, "_tool", return_value=None):
            self.assertEqual(android.packages(), [])

    def test_availability_respects_the_override(self) -> None:
        with mock.patch.dict(os.environ, {"ANINEXT_MPV_ANDROID": "0"}), \
             mock.patch.object(android, "am_binary", return_value="/usr/bin/am"), \
             mock.patch.object(android, "package_installed", return_value=True):
            self.assertFalse(android.mpv_android_available())
        with mock.patch.dict(os.environ, {"ANINEXT_MPV_ANDROID": "1"}), \
             mock.patch.object(android, "am_binary", return_value="/usr/bin/am"):
            self.assertTrue(android.mpv_android_available())

    def test_am_binary_is_cached(self) -> None:
        android._am_checked = False
        with mock.patch.object(android, "_tool", return_value="/usr/bin/termux-am") as tool:
            self.assertEqual(android.am_binary(), "/usr/bin/termux-am")
            self.assertEqual(android.am_binary(), "/usr/bin/termux-am")
            self.assertEqual(tool.call_count, 1)


class TestIntent(unittest.TestCase):
    def test_intent_argv(self) -> None:
        with mock.patch.object(android, "am_binary", return_value="/usr/bin/am"):
            cmd = android.build_mpv_intent(
                "https://cdn.example/frieren-3.mp4",
                title="Frieren - Episode 3",
                subs=["https://x/en.vtt", "https://x/es.vtt"],
                position_ms=90000,
            )
        self.assertEqual(cmd[:6], ["/usr/bin/am", "start", "-a", "android.intent.action.VIEW",
                                   "-t", "video/any"])
        self.assertIn("-p", cmd)
        self.assertEqual(cmd[cmd.index("-p") + 1], "is.xyz.mpv")
        self.assertEqual(cmd[cmd.index("-d") + 1], "https://cdn.example/frieren-3.mp4")
        self.assertEqual(cmd[cmd.index("--es") + 1], "title")
        self.assertEqual(cmd[cmd.index("--es") + 2], "Frieren - Episode 3")
        self.assertEqual(cmd[cmd.index("--ei") + 1], "position")
        self.assertEqual(cmd[cmd.index("--ei") + 2], "90000")
        self.assertEqual(cmd[cmd.index("--esa") + 1], "subs")
        self.assertEqual(cmd[cmd.index("--esa") + 2], "https://x/en.vtt,https://x/es.vtt")

    def test_optional_bits_are_left_out(self) -> None:
        with mock.patch.object(android, "am_binary", return_value="am"):
            cmd = android.build_mpv_intent("https://x/a.mp4")
        self.assertNotIn("--es", cmd)
        self.assertNotIn("--ei", cmd)
        self.assertNotIn("--esa", cmd)

    def test_zero_position_is_not_sent(self) -> None:
        with mock.patch.object(android, "am_binary", return_value="am"):
            cmd = android.build_mpv_intent("https://x/a.mp4", position_ms=0)
        self.assertNotIn("position", cmd)

    def test_subtitle_urls_with_commas_are_dropped(self) -> None:
        with mock.patch.object(android, "am_binary", return_value="am"):
            cmd = android.build_mpv_intent("https://x/a.mp4", subs=["https://x/a,b.vtt", "", "https://x/c.vtt"])
        self.assertEqual(cmd[cmd.index("--esa") + 2], "https://x/c.vtt")


class TestLaunching(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = self.dir / "am.log"
        script = self.dir / "am"
        script.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$AM_LOG\"\nexit 0\n", "utf-8"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {
            "PATH": f"{self.dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "AM_LOG": str(self.log),
            "ANINEXT_FAKE_PACKAGES": "is.xyz.mpv",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        android._am_checked = False
        android._package_cache.clear()
        self.addCleanup(setattr, android, "_am_checked", False)

    def test_play_in_mpv_android_runs_am(self) -> None:
        ok = android.play_in_mpv_android("https://cdn.example/x.mp4", title="Show - Episode 1")
        self.assertTrue(ok)
        for _ in range(50):                       # the process is detached
            if self.log.exists() and self.log.stat().st_size:
                break
            import time

            time.sleep(0.05)
        text = self.log.read_text()
        self.assertIn("android.intent.action.VIEW", text)
        self.assertIn("is.xyz.mpv", text)
        self.assertIn("https://cdn.example/x.mp4", text)
        self.assertIn("Show - Episode 1", text)

    def test_without_the_app_nothing_is_launched(self) -> None:
        os.environ["ANINEXT_FAKE_PACKAGES"] = "org.videolan.vlc"
        android._package_cache.clear()
        self.assertFalse(android.mpv_android_available())
        self.assertFalse(android.play_in_mpv_android("https://cdn.example/x.mp4"))
        self.assertFalse(self.log.exists())

    def test_open_in_another_package(self) -> None:
        self.assertTrue(android.open_in("org.videolan.vlc", "https://cdn.example/x.mp4"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
