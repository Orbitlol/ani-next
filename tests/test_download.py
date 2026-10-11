"""Download engine tests, served from a local HTTP server (no network)."""
from __future__ import annotations

import http.server
import socketserver
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aninext import download as dl
from aninext.models import Episode, Stream, Subtitle, Title

PAYLOAD = bytes((i % 251) for i in range(200_000))   # ~195 KB, deterministic
SUBTITLE = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.000\nHello there\n"
SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello there\n"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/video.mp4":
            range_header = self.headers.get("Range")
            start = 0
            if range_header and range_header.startswith("bytes="):
                start = int(range_header.split("=")[1].split("-")[0])
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD)-1}/{len(PAYLOAD)}")
            else:
                self.send_response(200)
            chunk = PAYLOAD[start:]
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
        elif path == "/sub.vtt":
            body = SUBTITLE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/vtt")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/sub.srt":
            body = SRT.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


class TestDownload(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.title = Title(title="Frieren: Beyond Journey's End", provider="demo",
                           episodes=[Episode(number=1, title="The Journey's End"),
                                     Episode(number=2, title="It Didn't Have to Be Magic")])
        self.episode = self.title.episodes[0]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_urllib_download(self) -> None:
        stream = Stream(url=f"{self.base}/video.mp4", kind="mp4", quality="1080p")
        dest = self.dir / "video.mp4"
        result = dl.download_stream(stream, dest)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.backend, "urllib")
        self.assertEqual(dest.stat().st_size, len(PAYLOAD))
        self.assertEqual(dest.read_bytes(), PAYLOAD)

    def test_resume_from_partial(self) -> None:
        stream = Stream(url=f"{self.base}/video.mp4", kind="mp4")
        dest = self.dir / "resume.mp4"
        part = dest.with_suffix(dest.suffix + ".part")
        part.write_bytes(PAYLOAD[:50_000])
        result = dl.download_stream(stream, dest)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(dest.read_bytes(), PAYLOAD)

    def test_skips_existing_file(self) -> None:
        stream = Stream(url=f"{self.base}/video.mp4", kind="mp4")
        dest = self.dir / "cached.mp4"
        dest.write_bytes(b"already here")
        result = dl.download_stream(stream, dest)
        self.assertEqual(result.backend, "cached")
        self.assertEqual(dest.read_bytes(), b"already here")

    def test_progress_callback(self) -> None:
        seen: list[dict] = []
        stream = Stream(url=f"{self.base}/video.mp4", kind="mp4")
        dl.download_stream(stream, self.dir / "p.mp4", progress=seen.append)
        self.assertTrue(seen)
        self.assertIn("downloaded", seen[-1])

    def test_failure_is_reported(self) -> None:
        stream = Stream(url=f"{self.base}/missing.mp4", kind="mp4")
        result = dl.download_stream(stream, self.dir / "x.mp4")
        self.assertFalse(result.ok)
        self.assertTrue(result.error)

    def test_subtitle_sidecars(self) -> None:
        subs = [
            Subtitle(label="English", lang="en", url=f"{self.base}/sub.vtt"),
            Subtitle(label="Spanish", lang="es", url=f"{self.base}/sub.srt"),
        ]
        base = self.dir / "show.mp4"
        written = dl.download_subtitles(subs, base, languages=["en", "es"])
        self.assertEqual(len(written), 2)
        names = sorted(p.name for p in written)
        self.assertIn("show.mp4.en.vtt", names)
        self.assertIn("show.mp4.es.srt", names)
        self.assertIn("Hello there", (self.dir / "show.mp4.en.vtt").read_text())

    def test_subtitle_language_filter(self) -> None:
        subs = [
            Subtitle(label="English", lang="en", url=f"{self.base}/sub.vtt"),
            Subtitle(label="Spanish", lang="es", url=f"{self.base}/sub.srt"),
        ]
        written = dl.download_subtitles(subs, self.dir / "f.mp4", languages=["en"])
        self.assertEqual(len(written), 1)

    def test_filename_naming(self) -> None:
        name = dl.episode_filename(self.title, self.episode, "1080p")
        self.assertEqual(name, "Frieren: Beyond Journey's End".replace(":", "") + " - S01E01 - The Journey's End [1080p].mp4")
        movie = Title(title="Blade Runner 2049", kind="movie",
                      episodes=[Episode(number=1, title="Full Movie")])
        self.assertEqual(dl.episode_filename(movie, movie.episodes[0], "1080p"),
                         "Blade Runner 2049 [1080p].mp4")

    def test_target_path_and_find_existing(self) -> None:
        path = dl.target_path(self.title, self.episode, quality="720p", directory=str(self.dir))
        self.assertEqual(path.parent.name, "Frieren Beyond Journey's End")
        self.assertTrue(path.name.startswith("Frieren"))
        # pretend it finished downloading
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        found = dl.find_existing(self.title, self.episode, directory=str(self.dir))
        self.assertIsNotNone(found)
        self.assertEqual(found.name, path.name)

    def test_queue_records_to_db(self) -> None:
        import tempfile

        from aninext.db import Database, set_db

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "q.db")
            set_db(db)
            try:
                stream = Stream(url=f"{self.base}/video.mp4", kind="mp4", quality="1080p")
                item = dl.prepare_item(self.title, self.episode, stream, directory=str(self.dir))
                self.assertTrue(item.id)
                results = dl.run_queue([item], workers=1, notify=False)
                self.assertTrue(results[0].ok)
                row = db.download_get(item.id)
                self.assertEqual(row["status"], "done")
                self.assertGreater(row["size"], 0)
            finally:
                set_db(None)  # type: ignore[arg-type]

    def test_human_helpers(self) -> None:
        self.assertEqual(dl.human_size(2048), "2.0 KB")
        self.assertEqual(dl.human_time(75), "01:15")
        self.assertEqual(dl.human_time(3725), "1:02:05")
        self.assertEqual(dl.safe_name("a/b:c*?"), "a b c")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
