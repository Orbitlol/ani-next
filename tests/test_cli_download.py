"""End-to-end `aninext download` test: a local site, a JSON scraper rule and
a real HTTP download - no network, no demo provider."""
from __future__ import annotations

import contextlib
import io
import http.server
import json
import socketserver
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aninext import cli
from aninext.config import get_config, set_config
from aninext.db import reset_db
from aninext.paths import home

VIDEO = bytes((i * 7) % 256 for i in range(120_000))

SEARCH_HTML = """<!doctype html><html><body>
<div class="grid">
  <article class="card" data-kind="anime">
    <h3 class="title"><a href="/item/frieren">Frieren: Beyond Journey's End</a></h3>
    <span class="year">2023</span>
    <img class="poster" data-src="/img/frieren.jpg">
  </article>
</div>
</body></html>"""

DETAIL_HTML = """<!doctype html><html><body>
<div class="entry" data-kind="anime">
  <h1 class="entry-title">Frieren: Beyond Journey's End</h1>
  <div class="info"><span class="year">2023</span><span class="score">9.0</span></div>
  <div class="genres"><a href="/genre/fantasy">Fantasy</a></div>
  <div class="description"><p>An elf mage outlives her party.</p></div>
  <ul class="episodes">
    <li class="episode" data-number="1"><a href="/watch/frieren-1">
      <span class="ep-num">1</span><span class="ep-title">The Journey's End</span></a></li>
    <li class="episode" data-number="2"><a href="/watch/frieren-2">
      <span class="ep-num">2</span><span class="ep-title">Magic</span></a></li>
  </ul>
</div>
</body></html>"""

WATCH_HTML = """<!doctype html><html><body>
<div class="watch">
  <div class="server" data-label="Server 1" data-embed="/embed/one"></div>
  <a class="dl" href="/media/frieren-1.mp4" data-quality="720p">720p</a>
</div>
</body></html>"""

EMBED_HTML = """<!doctype html><html><body>
<script>var player = {"file": "/media/frieren-1.mp4", "type": "mp4"};</script>
</body></html>"""

RULE = {
    "id": "testsite",
    "name": "Test Site",
    "base": "http://127.0.0.1:{port}",
    "kinds": ["anime", "movie", "series"],
    "priority": 55,
    "search": {
        "url": "/search?q={query}&page={page}",
        "items": "div.grid article.card",
        "fields": {
            "title": "h3.title a ::text",
            "url": "h3.title a @href",
            "poster": "img.poster @data-src",
            "year": "span.year ::text",
            "kind": "@data-kind",
        },
    },
    "details": {
        "fields": {
            "title": "h1.entry-title ::text",
            "year": "span.year ::text",
            "score": "span.score ::text",
            "synopsis": "div.description ::text",
            "kind": "@data-kind",
            "genres": "div.genres a ::text",
        },
        "episodes": {
            "items": "ul.episodes li.episode",
            "fields": {
                "number": "span.ep-num ::text",
                "title": "span.ep-title ::text",
                "url": "a @href",
            },
        },
    },
    "episode": {
        "servers": {
            "items": "div.server",
            "fields": {"label": "@data-label", "embed": "@data-embed"},
        },
        "downloads": {
            "items": "a.dl",
            "fields": {"url": "@href", "quality": "@data-quality"},
        },
    },
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/search"):
            self._send(SEARCH_HTML.encode())
        elif path == "/item/frieren":
            self._send(DETAIL_HTML.encode())
        elif path.startswith("/watch/"):
            self._send(WATCH_HTML.encode())
        elif path == "/embed/one":
            self._send(EMBED_HTML.encode())
        elif path == "/media/frieren-1.mp4":
            self._send(VIDEO, ctype="video/mp4")
        else:
            self._send(b"<p>not found</p>")


class TestCliDownload(unittest.TestCase):
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
        reset_db()
        set_config(None)  # type: ignore[arg-type]
        cfg = get_config(reload=True)
        cfg.set("colour", "never")
        cfg.set("wake_lock", False)
        cfg.set("notifications", False)
        set_config(cfg)
        sources = home() / "sources"
        sources.mkdir(parents=True, exist_ok=True)
        rule = json.loads(json.dumps(RULE).replace("{port}", str(self.server.server_address[1])))
        (sources / "testsite.json").write_text(json.dumps(rule), "utf-8")
        self.tmp = TemporaryDirectory()
        self.dest = Path(self.tmp.name)

    def tearDown(self) -> None:
        reset_db()
        (home() / "sources" / "testsite.json").unlink(missing_ok=True)
        self.tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str]:
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        self.err = err.getvalue()
        return code, buf.getvalue()

    def test_search_and_info_through_a_rule_file(self) -> None:
        code, out = self.run_cli("search", "frieren", "-p", "testsite", "--json")
        self.assertEqual(code, 0, self.err)
        results = json.loads(out)
        self.assertEqual(results[0]["title"], "Frieren: Beyond Journey's End")
        self.assertEqual(results[0]["provider"], "testsite")
        self.assertEqual(results[0]["kind"], "anime")

        code, out = self.run_cli("info", "testsite:frieren", "--json")
        self.assertEqual(code, 0, self.err)
        title = json.loads(out)
        self.assertEqual(title["year"], "2023")
        self.assertEqual(title["score"], "9.0")
        self.assertEqual(len(title["episodes"]), 2)
        self.assertIn("Fantasy", title["genres"])

    def test_links_resolves_embed_to_stream(self) -> None:
        code, out = self.run_cli("links", "testsite:frieren", "-e", "1", "--json")
        self.assertEqual(code, 0, self.err)
        data = json.loads(out)
        urls = [s["url"] for s in data["streams"]]
        self.assertTrue(any(url.endswith("/media/frieren-1.mp4") for url in urls), urls)
        self.assertTrue(any(d["quality"] == "720p" for d in data["downloads"]))

    def test_download_writes_the_file(self) -> None:
        code, out = self.run_cli(
            "download", "testsite:frieren", "-E", "1", "-d", str(self.dest),
            "-Q", "720p", "-j", "1",
        )
        self.assertEqual(code, 0, self.err)
        written = list(self.dest.rglob("*.mp4"))
        self.assertTrue(written, list(self.dest.rglob("*")))
        self.assertEqual(written[0].read_bytes(), VIDEO)
        self.assertIn("frieren", written[0].name.lower())
        # the download is recorded in the library database
        code, out = self.run_cli("downloads", "list", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["status"], "done")

    def test_download_skips_existing(self) -> None:
        self.run_cli("download", "testsite:frieren", "-E", "1", "-d", str(self.dest),
                     "-Q", "720p", "-j", "1")
        code, out = self.run_cli("download", "testsite:frieren", "-E", "1", "-d", str(self.dest),
                                 "-Q", "720p", "-j", "1")
        self.assertEqual(code, 0, self.err)
        self.assertIn("already downloaded", (out + self.err).lower())
        self.assertEqual(len(list(self.dest.rglob("*.mp4"))), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
