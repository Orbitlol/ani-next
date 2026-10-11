"""Embed-resolution tests: packed JS, base64, iframes, HLS manifests.

Everything is served by a local HTTP server, so no network is required.  The
packed payloads are produced by the *real* Dean Edwards packer (the encoder is
implemented at the bottom of this file) instead of hard-coded strings, which
keeps the decoder honest.
"""
from __future__ import annotations

import base64
import http.server
import re
import socketserver
import threading
import unittest

from aninext.net import Client, client as default_client
from aninext.resolve import (
    GENERIC_PATTERNS,
    Resolver,
    deobfuscate,
    find_media_urls,
    find_packed,
    parse_manifest,
    quality_from_url,
    resolve_embed,
    unpack_all,
    unpack_packed_js,
)

HLS = "https://cdn.example.net/hls/master.m3u8"
MP4 = "https://cdn.example.net/vod/movie-1080p.mp4"


def _pack(script: str, base: int = 36) -> str:
    """A genuine p.a.c.k.e.r encoder (p,a,c,k,e,d) - generates test fixtures."""
    # real packers tokenise \w+ , so "1080p" is one word (not "1080" + "p")
    words = sorted(set(re.findall(r"[A-Za-z0-9_$]+", script)), key=len, reverse=True)
    if not words:
        return f"eval(function(p,a,c,k,e,d){{return p}}('{script}',{base},0,''.split('|'),0,{{}}))"
    table = {word: index for index, word in enumerate(words)}

    def encode(value: int) -> str:
        if value < base:
            chunk = ""
        else:
            chunk = encode(value // base)
        digit = value % base
        char = chr(digit + 29) if digit > 35 else "0123456789abcdefghijklmnopqrstuvwxyz"[digit]
        return chunk + char

    packed = re.sub(r"[A-Za-z0-9_$]+",
                    lambda m: encode(table[m.group(0)]),
                    script)
    symbols = "|".join(words)
    return (
        "eval(function(p,a,c,k,e,d){e=function(c){return(c<a?'':e(parseInt(c/a)))+"
        "((c=c%a)>35?String.fromCharCode(c+29):c.toString(36))};if(!''.replace(/^/,String))"
        "{while(c--){d[e(c)]=k[c]||e(c)}k=[function(e){return d[e]}];e=function(){return'\\\\w+'};c=1};"
        "while(c--){if(k[c]){p=p.replace(new RegExp('\\\\b'+e(c)+'\\\\b','g'),k[c])}}return p}"
        f"('{packed.replace(chr(39), chr(92) + chr(39))}',{base},{len(words)},'{symbols}'.split('|'),0,{{}}))"
    )


EMBED = "<!doctype html><html><body><video></video>%s</body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, body: str, ctype: str = "text/html; charset=utf-8") -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/embed/packed":
            script = f'var player = {{"file": "{HLS}", "type": "hls"}};'
            self._send(EMBED % f"<script>{_pack(script)}</script>")
        elif path == "/embed/base64":
            blob = base64.b64encode(MP4.encode()).decode()
            self._send(EMBED % f"<script>video.src = atob(\"{blob}\");</script>")
        elif path == "/embed/escaped":
            # jwplayer-style escaped slashes, exactly as scraped pages ship them
            url = HLS.replace("/", "\\/")
            self._send(EMBED % f"<script>jwplayer('v').setup({{file:'{url}'}});</script>")
        elif path == "/embed/iframe":
            self._send(EMBED % '<iframe src="/embed/packed"></iframe>')
        elif path == "/embed/drm":
            self._send(EMBED % f'<script>var x = {{widevine: true, file: "{HLS}"}};</script>')
        elif path == "/embed/nothing":
            self._send(EMBED % "<script>console.log('no media here');</script>")
        elif path == "/embed/ad":
            self._send(EMBED % '<script>var ad = "https://ads.example.net/tracker-300x250.png";</script>')
        elif path == "/embed/subs":
            self._send(EMBED % (
                '<video><track kind="subtitles" label="English" srclang="en" src="/subs/en.vtt">'
                '<track kind="subtitles" label="Spanish" srclang="es" src="/subs/es.vtt"></video>'
            ))
        elif path == "/manifest.m3u8":
            self._send(
                "#EXTM3U\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720,NAME=\"720p\"\n"
                "720/index.m3u8\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1920x1080,NAME=\"1080p\"\n"
                "1080/index.m3u8\n",
                ctype="application/vnd.apple.mpegurl",
            )
        elif path == "/plain.m3u8":
            self._send("#EXTM3U\n#EXT-X-TARGETDURATION:6\nsegment0.ts\n")
        else:
            self._send("<!doctype html><p>not found</p>")


class TestPacker(unittest.TestCase):
    def test_unpack_roundtrip_base36(self) -> None:
        script = 'var player = {"file": "https://cdn.example.net/hls/master.m3u8"};'
        packed = _pack(script, base=36)
        self.assertIn("eval(function(p,a,c,k,e,d)", packed)
        self.assertEqual(unpack_packed_js(*find_packed(packed)[0]).strip(), script.replace("'", "\\'"))

    def test_unpack_roundtrip_base62(self) -> None:
        script = 'source: "https://cdn.example.net/vod/frieren-ep1-1080p.mp4"'
        packed = _pack(script, base=62)
        self.assertEqual(unpack_packed_js(*find_packed(packed)[0]).strip(), script.replace("'", "\\'"))

    def test_find_packed_ignores_lookalikes(self) -> None:
        self.assertEqual(find_packed("eval('not packed at all')"), [])
        self.assertEqual(find_packed("var a = 'x'.split('|');"), [])

    def test_unpack_all(self) -> None:
        packed = _pack('var file = "https://x.example/a.m3u8";')
        self.assertIn("a.m3u8", unpack_all(f"<script>{packed}</script>"))

    def test_deobfuscate_decodes_base64(self) -> None:
        blob = base64.b64encode(MP4.encode()).decode()
        text = f'<script>var u = "{blob}";</script>'
        self.assertIn(MP4, deobfuscate(text))


class TestFindMediaUrls(unittest.TestCase):
    def test_finds_plain_urls(self) -> None:
        urls = dict(find_media_urls(f'<video src="{MP4}"></video>'))
        self.assertIn(MP4, urls)

    def test_finds_escaped_slashes(self) -> None:
        text = 'jwplayer("x").setup({file: "https:\\/\\/cdn.example.net\\/hls\\/master.m3u8"});'
        urls = [u for u, _ in find_media_urls(text)]
        self.assertTrue(any(u.endswith("master.m3u8") for u in urls), urls)

    def test_atob_base64(self) -> None:
        blob = base64.b64encode(MP4.encode()).decode()
        urls = [u for u, _ in find_media_urls(f'x.src = atob("{blob}");')]
        self.assertIn(MP4, urls)

    def test_custom_patterns(self) -> None:
        text = 'data-media="https://weird.example/path/stream-abc"'
        pattern = r'data-media="([^"]+)"'
        urls = [u for u, _ in find_media_urls(text, [pattern])]
        self.assertIn("https://weird.example/path/stream-abc", urls)

    def test_ignores_ads_and_data_uris(self) -> None:
        text = ('<script>a="https://ads.example.net/banner.png";'
                'b="data:video/mp4;base64,AAAA";</script>')
        urls = [u for u, _ in find_media_urls(text)]
        self.assertFalse(any("banner.png" in u or u.startswith("data:") for u in urls))

    def test_query_strings_survive(self) -> None:
        text = '<source src="https://cdn.example.net/vod/movie.mp4?token=abc123&e=1">'
        urls = [u for u, _ in find_media_urls(text)]
        self.assertTrue(any(u.endswith("?token=abc123&e=1") for u in urls), urls)

    def test_quality_from_url(self) -> None:
        self.assertEqual(quality_from_url("https://x/movie-1080p.mp4"), "1080p")
        self.assertEqual(quality_from_url("https://x/movie-4k.mkv"), "4k")
        self.assertEqual(quality_from_url("https://x/movie-2160p.mkv"), "4k")
        self.assertEqual(quality_from_url("https://x/master.m3u8"), "auto")


class TestResolver(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_packed_js_embed(self) -> None:
        streams = resolve_embed(f"{self.base}/embed/packed", http=default_client())
        urls = [s.url for s in streams]
        self.assertIn(HLS, urls)
        stream = next(s for s in streams if s.url == HLS)
        self.assertEqual(stream.kind, "hls")
        self.assertTrue(stream.playable)
        self.assertEqual(stream.provider, "")

    def test_base64_embed(self) -> None:
        streams = resolve_embed(f"{self.base}/embed/base64", http=default_client())
        self.assertIn(MP4, [s.url for s in streams])

    def test_escaped_slashes_embed(self) -> None:
        streams = resolve_embed(f"{self.base}/embed/escaped", http=default_client())
        self.assertTrue(any(s.url.endswith("master.m3u8") for s in streams))

    def test_iframe_is_followed(self) -> None:
        streams = resolve_embed(f"{self.base}/embed/iframe", http=default_client())
        self.assertIn(HLS, [s.url for s in streams])

    def test_depth_zero_does_not_follow_iframes(self) -> None:
        streams = Resolver(default_client(), depth=0).resolve(f"{self.base}/embed/iframe")
        self.assertEqual(streams, [])

    def test_drm_is_flagged_but_returned(self) -> None:
        streams = resolve_embed(f"{self.base}/embed/drm", http=default_client())
        self.assertTrue(streams)
        self.assertTrue(all(s.drm for s in streams))
        self.assertFalse(any(s.playable for s in streams))

    def test_no_media(self) -> None:
        self.assertEqual(resolve_embed(f"{self.base}/embed/nothing", http=default_client()), [])

    def test_ads_are_ignored(self) -> None:
        self.assertEqual(resolve_embed(f"{self.base}/embed/ad", http=default_client()), [])

    def test_unreachable_embed_returns_empty(self) -> None:
        client = Client(timeout=2, retries=0)
        self.assertEqual(resolve_embed("http://127.0.0.1:1/embed", http=client), [])

    def test_direct_media_url_is_returned_without_fetching(self) -> None:
        streams = resolve_embed(MP4, http=default_client())
        self.assertEqual([s.url for s in streams], [MP4])
        self.assertEqual(streams[0].kind, "mp4")

    def test_subtitles_from_embed(self) -> None:
        resolver = Resolver(default_client())
        subs = resolver.subtitles(f"{self.base}/embed/subs", referer=self.base + "/")
        labels = {s.label for s in subs}
        self.assertEqual(len(subs), 2)
        self.assertIn("English", labels)
        self.assertTrue(all(s.url.startswith(self.base) for s in subs), [s.url for s in subs])

    def test_parse_manifest(self) -> None:
        variants = parse_manifest(f"{self.base}/manifest.m3u8", default_client())
        self.assertEqual(len(variants), 2)
        by_name = {v["name"]: v for v in variants}
        self.assertEqual(by_name["1080p"]["resolution"], "1920x1080")
        self.assertEqual(by_name["720p"]["bandwidth"], 800000)
        self.assertTrue(by_name["1080p"]["url"].endswith("/1080/index.m3u8"))

    def test_parse_manifest_not_a_playlist(self) -> None:
        self.assertEqual(parse_manifest(f"{self.base}/embed/packed", default_client()), [])

    def test_generic_patterns_are_usable(self) -> None:
        self.assertTrue(any("m3u8" in pattern for pattern, _ in GENERIC_PATTERNS))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
