"""HTTP client tests against a throwaway local server (no network needed)."""
from __future__ import annotations

import gzip
import http.server
import json
import socketserver
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aninext.net import Client, DiskCache, HttpError, absolute, add_query, host_of, parallel, strip_html


class Handler(http.server.BaseHTTPRequestHandler):
    hits: dict[str, int] = {}

    def log_message(self, *args) -> None:  # silence the server
        pass

    def _send(self, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8",
              headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        Handler.hits[path] = Handler.hits.get(path, 0) + 1

        if path == "/simple":
            self._send(b"<h1>hello</h1>")
        elif path == "/json":
            self._send(json.dumps({"ok": True, "items": [1, 2, 3]}).encode(), ctype="application/json")
        elif path == "/gzip":
            raw = gzip.compress(b"<p>compressed content</p>")
            self._send(raw, headers={"Content-Encoding": "gzip"})
        elif path == "/latin":
            self._send("café".encode("latin-1"), ctype="text/html; charset=latin-1")
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/simple")
            self.end_headers()
        elif path == "/flaky":
            if Handler.hits[path] < 3:
                self._send(b"nope", status=500)
            else:
                self._send(b"<p>eventually fine</p>")
        elif path == "/missing":
            self._send(b"not found", status=404)
        elif path == "/binary":
            body = bytes(range(256)) * 64  # 16 KiB
            self._send(body, ctype="application/octet-stream")
        elif path == "/ranged":
            total = 64 * 1024
            range_header = self.headers.get("Range")
            start = 0
            if range_header and range_header.startswith("bytes="):
                start = int(range_header.split("=")[1].split("-")[0])
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{total-1}/{total}")
            else:
                self.send_response(200)
            payload = bytes((i % 251) for i in range(start, total))
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self._send(b"?", status=404)


class TestClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.client = Client(retries=4, backoff=0.01, use_cache=False, timeout=5)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_get_text(self) -> None:
        resp = self.client.get(f"{self.base}/simple")
        self.assertEqual(resp.status, 200)
        self.assertIn("hello", resp.text)

    def test_json(self) -> None:
        data = self.client.json(f"{self.base}/json")
        self.assertTrue(data["ok"])
        self.assertEqual(data["items"], [1, 2, 3])

    def test_gzip_decoding(self) -> None:
        self.assertIn("compressed content", self.client.get(f"{self.base}/gzip").text)

    def test_charset_detection(self) -> None:
        self.assertIn("café", self.client.get(f"{self.base}/latin").text)

    def test_redirects(self) -> None:
        self.assertIn("hello", self.client.get(f"{self.base}/redirect").text)

    def test_retry_on_500(self) -> None:
        Handler.hits["/flaky"] = 0
        self.assertIn("eventually fine", self.client.get(f"{self.base}/flaky").text)
        self.assertGreaterEqual(Handler.hits["/flaky"], 3)

    def test_404_raises(self) -> None:
        with self.assertRaises(HttpError) as ctx:
            self.client.get(f"{self.base}/missing")
        self.assertEqual(ctx.exception.status, 404)

    def test_query_params(self) -> None:
        self.assertEqual(add_query("http://x/y?a=1", {"b": "2"}), "http://x/y?a=1&b=2")

    def test_download_binary(self) -> None:
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "file.bin"
            self.client.download(f"{self.base}/binary", dest)
            self.assertEqual(dest.stat().st_size, 16 * 1024)

    def test_download_resume(self) -> None:
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "big.bin"
            part = dest.with_suffix(dest.suffix + ".part")
            part.write_bytes(bytes((i % 251) for i in range(1000)))  # pretend half done
            self.client.download(f"{self.base}/ranged", dest)
            self.assertEqual(dest.stat().st_size, 64 * 1024)

    def test_parallel_preserves_order(self) -> None:
        out = parallel(lambda n: n * 2, [1, 2, 3, 4], workers=3)
        self.assertEqual(out, [2, 4, 6, 8])

    def test_parallel_skips_errors(self) -> None:
        def flaky(n: int) -> int:
            if n == 2:
                raise ValueError("boom")
            return n

        self.assertEqual(sorted(parallel(flaky, [1, 2, 3], workers=2)), [1, 3])

    def test_helpers(self) -> None:
        self.assertEqual(absolute("http://a/b/c", "/d"), "http://a/d")
        self.assertEqual(absolute("http://a/b/c", "d"), "http://a/b/d")
        self.assertEqual(absolute("http://a/b/c", "//x/y"), "http://x/y")
        self.assertEqual(absolute("https://a/b/c", "//x/y"), "https://x/y")
        self.assertEqual(host_of("https://www.example.com/x"), "example.com")
        self.assertEqual(strip_html("<p>hi <b>there</b></p>"), "hi there")


class TestDiskCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_roundtrip_and_ttl(self) -> None:
        from aninext.net import Response

        with TemporaryDirectory() as tmp:
            cache = DiskCache(Path(tmp), ttl=60)
            resp = Response(url="http://x/1", status=200, headers={"a": "b"}, body=b"hello")
            cache.put("http://x/1", resp)
            hit = cache.get("http://x/1")
            self.assertIsNotNone(hit)
            self.assertEqual(hit.body, b"hello")
            self.assertTrue(hit.from_cache)

            forever = DiskCache(Path(tmp), ttl=-1)  # negative ttl = never expires
            forever.put("http://x/2", Response(url="http://x/2", status=200, headers={}, body=b"keep"))
            self.assertIsNotNone(forever.get("http://x/2"))

            expired = DiskCache(Path(tmp), ttl=0)
            self.assertIsNone(expired.get("http://x/2"))

    def test_client_cache(self) -> None:
        Handler.hits["/simple"] = 0
        with TemporaryDirectory() as tmp:
            client = Client(use_cache=True, cache_ttl=60)
            client._cache = DiskCache(Path(tmp), ttl=60)
            url = f"{self.base}/simple"
            client.get(url)
            client.get(url)
            self.assertEqual(Handler.hits["/simple"], 1)
            self.assertEqual(client.stats["cached"], 1)

    def test_client_cache_disabled(self) -> None:
        Handler.hits["/simple"] = 0
        with TemporaryDirectory() as tmp:
            client = Client(use_cache=False)
            client._cache = DiskCache(Path(tmp), ttl=60)
            url = f"{self.base}/simple?nocache=1"
            client.get(url)
            client.get(url, cache=False)
            self.assertEqual(Handler.hits["/simple"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
