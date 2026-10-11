"""Turn embed/player pages into direct media URLs.

Scraped sites rarely expose a video file directly: they show an iframe from a
streaming host, which in turn hides an .m3u8 or .mp4 behind a little JavaScript.
This module does the boring work generically:

  1. fetch the embed page
  2. run rule-supplied regexes, then a set of generic patterns
  3. unpack Dean Edwards "p.a.c.k.e.r" packed JS, which most of those hosts use
  4. follow nested iframes a couple of levels deep
  5. fall back to yt-dlp when it is installed

It is intentionally *not* a DRM circumvention tool: encrypted/Widevine sources
are detected and reported as such instead of being touched.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Iterable, Sequence

from . import log
from .models import Stream, guess_kind
from .net import Client, HttpError, absolute, client as default_client, host_of, parallel

# --------------------------------------------------------------------------
# generic patterns
# --------------------------------------------------------------------------

GENERIC_PATTERNS: list[tuple[str, str]] = [
    # (regex, forced kind or "")
    (r"""["'](https?://[^"'\s\\<>]+?\.m3u8[^"'\s\\<>]*)["']""", "hls"),
    (r"""(https?://[^"'\s\\<>]+?\.m3u8[^"'\s\\<>]*)""", "hls"),
    (r"""(https?://[^"'\s\\<>]+?\.mpd[^"'\s\\<>]*)""", "dash"),
    (r"""["'](https?://[^"'\s\\<>]+?\.(?:mp4|m4v|webm|mkv)[^"'\s\\<>]*)["']""", ""),
    (r"""(https?://[^"'\s\\<>]+?\.(?:mp4|m4v|webm|mkv)[^"'\s\\<>]*)""", ""),
    (r"""file\s*[:=]\s*["']([^"']+\.(?:m3u8|mp4|mpd)[^"']*)["']""", ""),
    (r"""["']?(?:source|src|file|url|link|stream)["']?\s*[:=]\s*["']([^"']{12,}?)["']""", ""),
    # root-relative paths, e.g. {"file": "/media/movie-1080p.mp4"}
    (r"""["'](/(?:[^"'\s\\<>]{0,240}?)\.(?:m3u8|mp4|mpd|webm|m4v|mkv)(?:\?[^"'\s\\<>]{0,120})?)["']""", ""),
    (r"""["']hls(?:url|_url|Url)?["']\s*:\s*["']([^"']+)["']""", "hls"),
    (r"""atob\s*\(\s*["']([A-Za-z0-9+/=]{24,})["']\s*\)""", ""),
    (r"""["']([^"']*?\.(?:m3u8|mp4)\?[^"']{0,200})["']""", ""),
]

_DRM_HINTS = ("widevine", "playready", "clearkey", "drm", "license", "encrypted-media", "fairplay")

_IFRAME_RE = re.compile(r"""<iframe[^>]+src\s*=\s*["']([^"']+)["']""", re.I)

_SPLIT_RE = re.compile(r"""\.split\s*\(\s*['"]\|['"]\s*\)""")
_ARGS_TAIL_RE = re.compile(r",\s*(\d+)\s*,\s*(\d+)\s*,\s*$")
_OBFUSCATED_RE = re.compile(r"""["']([A-Za-z0-9+/=]{40,})["']""")


def _pack_digit(value: int) -> str:
    """Canonical packer digit: 0-9a-z, then chars from chr(29) for >35."""
    if value > 35:
        return chr(value + 29)
    return "0123456789abcdefghijklmnopqrstuvwxyz"[value]


def _pack_base(num: int, base: int) -> str:
    if num < base:
        return _pack_digit(num)
    return _pack_base(num // base, base) + _pack_digit(num % base)


def unpack_packed_js(payload: str, radix: int, count: int, symbols: str) -> str:
    """Decode a Dean Edwards / p.a.c.k.e.r packed body.

    Tokens in `payload` are indices into the `symbols` dictionary written in
    base `radix`; every word-like token is swapped for the entry it encodes.
    """
    base = radix if 2 <= radix <= 62 else 36
    words = symbols.split("|")
    table: dict[str, str] = {}
    for i in range(max(count, len(words))):
        word = words[i] if i < len(words) else ""
        if word and word not in ("", "undefined"):
            table[_pack_base(i, base)] = word

    def repl(match: re.Match) -> str:
        token = match.group(0)
        return table.get(token, token)

    try:
        return re.sub(r"[A-Za-z0-9_$]+", repl, payload)
    except Exception:  # noqa: BLE001
        return payload


def _find_open_quote(text: str, close: int, quote: str) -> int | None:
    """Index of the quote that opens the literal closing at `close`."""
    i = close - 1
    while i >= 0:
        if text[i] == quote:
            backslashes = 0
            j = i - 1
            while j >= 0 and text[j] == "\\":
                backslashes += 1
                j -= 1
            if backslashes % 2 == 0:
                return i
        i -= 1
    return None


def _string_before(text: str, end: int) -> tuple[int, str, str] | None:
    """Parse the JS string literal that ends at index `end`.

    Returns (open_index, quote, value) or None.
    """
    i = end
    while i > 0 and text[i - 1].isspace():
        i -= 1
    if i == 0:
        return None
    quote = text[i - 1]
    if quote not in ("'", '"'):
        return None
    close = i - 1
    open_ = _find_open_quote(text, close, quote)
    if open_ is None:
        return None
    return open_, quote, text[open_ + 1: close]


def find_packed(text: str) -> list[tuple[str, int, int, str]]:
    """Locate packed payloads: returns [(payload, radix, count, symbols)].

    Anchored on `...,'a|b|c'.split('|')` and scanned *backwards*, because the
    packed payload itself almost always contains quotes.
    """
    found: list[tuple[str, int, int, str]] = []
    for m in _SPLIT_RE.finditer(text):
        head = text[: m.start()]
        sym = _string_before(head, len(head))
        if sym is None:
            continue
        sym_open, _q, symbols = sym
        args = _ARGS_TAIL_RE.search(head[: sym_open])
        if not args:
            continue
        radix, count = int(args.group(1)), int(args.group(2))
        if not (2 <= radix <= 62) or count <= 0:
            continue
        payload_lit = _string_before(head, args.start())
        if payload_lit is None:
            continue
        p_open, _pq, payload = payload_lit
        if not payload:
            continue
        window = head[max(0, p_open - 400): p_open]
        if not re.search(r"(eval|function|\bk\b|\.replace)\s*[\[(]", window):
            continue
        found.append((payload, radix, count, symbols))
    return found


def unpack_all(text: str) -> str:
    """Unpack every packed JS blob we can find in `text`."""
    extra: list[str] = []
    for payload, radix, count, symbols in find_packed(text):
        try:
            decoded = unpack_packed_js(payload, radix, count, symbols)
        except (ValueError, IndexError):
            continue
        if decoded and decoded != payload:
            extra.append(decoded.replace("\\/", "/"))
    return "\n".join(extra)


def deobfuscate(text: str) -> str:
    """Apply the common light obfuscations found on player pages."""
    out = text + "\n" + unpack_all(text)
    # base64 payloads that decode to something script-like
    for m in _OBFUSCATED_RE.finditer(text):
        raw = m.group(1)
        try:
            decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
        except (binascii.Error, ValueError):
            continue
        if any(tok in decoded for tok in (".m3u8", ".mp4", "http", "source", "file")):
            out += "\n" + decoded
    # \u002F \/ html-escaped slashes
    out = out.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    out = out.replace("&amp;", "&").replace("&#038;", "&")
    return out


def find_media_urls(text: str, patterns: Sequence[str] = (), base_url: str = "") -> list[tuple[str, str]]:
    """Return [(url, kind)] discovered in `text`.

    Root-relative candidates ("/media/x.mp4") are made absolute against
    `base_url` when one is given.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    body = deobfuscate(text)

    def _finalise(url: str) -> str:
        if base_url and url.startswith("/"):
            return absolute(base_url, url)
        return url

    for pattern in patterns:
        try:
            for m in re.finditer(pattern, body, re.I | re.S):
                url = m.group(1) if m.groups() else m.group(0)
                url = _clean_url(url)
                if url and url not in seen:
                    seen.add(url)
                    found.append((url, guess_kind(url)))
        except re.error as exc:
            log.debug("bad rule pattern %r: %s", pattern, exc)

    for pattern, forced in GENERIC_PATTERNS:
        for m in re.finditer(pattern, body, re.I | re.S):
            url = _finalise(_clean_url(m.group(1) if m.groups() else m.group(0)))
            if not url or url in seen:
                continue
            if not _plausible_media(url):
                continue
            seen.add(url)
            found.append((url, forced or guess_kind(url)))

    # json blobs with a "sources" array
    for blob in re.findall(r"\{[^{}]*\"(?:sources|tracks|playlist)\"\s*:\s*\[.*?\]\s*\}", body, re.S | re.I):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        for src in data.get("sources", []) or []:
            if isinstance(src, dict):
                url = _finalise(_clean_url(str(src.get("file") or src.get("src") or src.get("url") or "")))
                if url and url not in seen and _plausible_media(url):
                    seen.add(url)
                    found.append((url, guess_kind(url)))

    return found


def _clean_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip().strip("\"'").replace("\\/", "/")
    url = url.replace("\\u0026", "&").replace("\\u003d", "=").replace("\\", "")
    url = re.sub(r"[\s]+$", "", url)
    if url.startswith("//"):
        url = "https:" + url
    return url


_NOT_URL_CHARS = set('{}[]"\'<>() \t\n\r|\\')


def _plausible_media(url: str) -> bool:
    if len(url) < 12:
        return False
    if url.startswith(("data:", "blob:", "javascript:")):
        return False
    if any(ch in _NOT_URL_CHARS for ch in url):
        return False
    low = url.lower()
    if any(bad in low for bad in ("googlesyndication", "doubleclick", "adsystem", "/ads/",
                                  "google-analytics", "facebook.com", "analytics")):
        return False
    if not re.search(r"\.(m3u8|mp4|mpd|webm|m4v|mkv|ts)(\?|$|#)", low) and "m3u8" not in low:
        return False
    return True


def quality_from_url(url: str) -> str:
    from .providers.base import quality_from_text

    low = url.lower()
    q = quality_from_text(low)
    if q:
        return q
    for tag, label in (("1080", "1080p"), ("720", "720p"), ("480", "480p"), ("360", "360p"), ("2160", "2160p")):
        if tag in low:
            return label
    if "/hls/" in low or "master" in low:
        return "auto"
    return ""


def has_drm(text: str) -> bool:
    low = text.lower()
    return any(hint in low for hint in _DRM_HINTS)


def is_likely_embed(url: str) -> bool:
    low = url.lower()
    if guess_kind(url) in ("mp4", "hls", "dash", "webm", "mkv", "torrent"):
        return False
    return any(k in low for k in ("embed", "player", "iframe", "/e/", "/v/", "/stream", "watch"))


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

class Resolver:
    def __init__(self, http: Client | None = None, *, patterns: Sequence[str] = (),
                 depth: int = 2, timeout: float | None = None) -> None:
        self.http = http or default_client()
        self.patterns = list(patterns)
        self.depth = max(0, depth)
        self.timeout = timeout

    def resolve(self, url: str, *, referer: str = "") -> list[Stream]:
        """Resolve a single embed URL into candidate streams."""
        seen_urls: set[str] = set()
        streams: list[Stream] = []
        queue: list[tuple[str, int, str]] = [(url, 0, referer)]
        visited: set[str] = set()

        while queue:
            current, level, ref = queue.pop(0)
            if current in visited or level > self.depth:
                continue
            visited.add(current)

            if guess_kind(current) in ("mp4", "hls", "dash", "webm", "mkv"):
                if current not in seen_urls:
                    seen_urls.add(current)
                    streams.append(_stream_from(current, server=host_of(current), referer=ref))
                continue

            try:
                resp = self.http.get(current, referer=ref or None, cache=False)
                text = resp.text
            except HttpError as exc:
                log.debug("resolve fetch failed %s: %s", current, exc)
                continue

            headers = {"Referer": current, "User-Agent": self.http.user_agent}
            drm = has_drm(text)

            for media_url, kind in find_media_urls(text, self.patterns, base_url=current):
                if media_url in seen_urls:
                    continue
                seen_urls.add(media_url)
                streams.append(
                    Stream(
                        url=media_url,
                        quality=quality_from_url(media_url),
                        kind=kind or guess_kind(media_url),
                        server=host_of(media_url),
                        referer=current,
                        headers=dict(headers),
                        drm=drm,
                    )
                )

            if level < self.depth:
                for m in _IFRAME_RE.finditer(text):
                    child = absolute(current, m.group(1))
                    if child and child not in visited:
                        queue.append((child, level + 1, current))

            if not streams and "eval(" in text and level < self.depth:
                # one more pass over fully unpacked source
                for media_url, kind in find_media_urls(deobfuscate(text), self.patterns, base_url=current):
                    if media_url in seen_urls:
                        continue
                    seen_urls.add(media_url)
                    streams.append(_stream_from(media_url, referer=current, headers=headers))

        return [s for s in streams if _plausible_media(s.url)]

    def subtitles(self, url: str, *, referer: str = "") -> list:
        from .models import Subtitle

        try:
            resp = self.http.get(url, referer=referer or None, cache=False)
        except HttpError:
            return []
        out: list[Subtitle] = []
        for m in re.finditer(r"<track\b[^>]*>", resp.text, re.I):
            tag = m.group(0)
            src = re.search(r"""src\s*=\s*["']([^"']+)["']""", tag, re.I)
            if not src:
                continue
            label = re.search(r"""label\s*=\s*["']([^"']*)["']""", tag, re.I)
            lang = re.search(r"""srclang\s*=\s*["']([^"']*)["']""", tag, re.I)
            out.append(
                Subtitle(
                    label=(label.group(1) if label else "") or "subtitle",
                    lang=(lang.group(1) if lang else "")[:2].lower(),
                    url=absolute(resp.url, src.group(1)),
                )
            )
        return out


def _stream_from(url: str, *, server: str = "", referer: str = "", headers: dict | None = None) -> Stream:
    return Stream(
        url=url,
        quality=quality_from_url(url),
        kind=guess_kind(url),
        server=server or host_of(url),
        referer=referer,
        headers=dict(headers or ({"Referer": referer} if referer else {})),
    )


def resolve_embed(url: str, *, http: Client | None = None, patterns: Sequence[str] = (),
                  referer: str = "", depth: int = 2) -> list[Stream]:
    """Module-level convenience wrapper."""
    return Resolver(http, patterns=patterns, depth=depth).resolve(url, referer=referer)


def upgrade_embeds(
    embeds: Iterable[tuple[str, str]],
    *,
    client: Client | None = None,
    patterns: Sequence[str] = (),
    referer: str = "",
    provider: str = "",
    server: str = "",
    workers: int = 4,
    depth: int = 2,
) -> list[Stream]:
    """Resolve many embeds in parallel; keep the embeds that stay unresolved."""
    pairs = [(lbl, url) for lbl, url in embeds if url]
    if not pairs:
        return []
    http = client or default_client()

    def job(pair: tuple[str, str]) -> list[Stream]:
        label, url = pair
        streams = Resolver(http, patterns=patterns, depth=depth).resolve(url, referer=referer)
        for s in streams:
            s.label = s.label or label
            s.provider = provider
            s.server = s.server or server
            if not s.referer:
                s.referer = url
        if not streams:
            streams = [
                Stream(url=url, kind="embed", label=label, server=server or host_of(url),
                       provider=provider, referer=referer)
            ]
        return streams

    results = parallel(job, pairs, workers=max(1, workers))
    flat: list[Stream] = []
    for group in results:
        flat.extend(group or [])
    return flat


# --------------------------------------------------------------------------
# yt-dlp bridge
# --------------------------------------------------------------------------

def yt_dlp_available() -> bool:
    from .paths import which

    return which("yt-dlp", "youtube-dl") is not None


def resolve_with_ytdlp(url: str, *, referer: str = "", timeout: float = 60.0) -> list[Stream]:
    """Use yt-dlp to list formats for a URL (optional dependency)."""
    import subprocess

    from .paths import which

    exe = which("yt-dlp", "youtube-dl")
    if not exe:
        return []
    cmd = [exe, "-J", "--no-warnings", "--no-playlist"]
    if referer:
        cmd += ["--referer", referer]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("yt-dlp failed: %s", exc)
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        log.debug("yt-dlp returned %s: %s", proc.returncode, proc.stderr.strip()[:200])
        return []
    try:
        info = json.loads(proc.stdout)
    except ValueError:
        return []
    if "entries" in info:
        info = next((e for e in info["entries"] if e), info)
    streams: list[Stream] = []
    for fmt in info.get("formats", []) or []:
        if fmt.get("vcodec") in (None, "none"):
            continue
        url_ = fmt.get("url") or ""
        if not url_:
            continue
        streams.append(
            Stream(
                url=url_,
                quality=fmt.get("format_note") or fmt.get("height") and f"{fmt['height']}p" or "",
                kind=guess_kind(url_) or "hls" if url_.startswith("http") else "other",
                server=fmt.get("extractor") or host_of(url),
                label=fmt.get("format_id") or "",
                headers=dict(fmt.get("http_headers") or {}),
                size=int(fmt.get("filesize") or fmt.get("filesize_approx") or 0),
                provider="yt-dlp",
                extra={"ext": fmt.get("ext"), "extractor": fmt.get("extractor")},
            )
        )
    if not streams and info.get("url"):
        streams.append(_stream_from(info["url"], server=info.get("extractor", "yt-dlp")))
    for s in streams:
        s.quality = s.quality or quality_from_url(s.url)
    return streams


def yt_dlp_download(url: str, dest_dir: str, *, referer: str = "", extra_args: Sequence[str] = ()) -> bool:
    """Download with yt-dlp straight to a directory (optional path)."""
    import subprocess

    from .paths import which

    exe = which("yt-dlp", "youtube-dl")
    if not exe:
        return False
    cmd = [exe, "-o", f"{dest_dir}/%(title)s.%(ext)s", "--no-warnings", "--newline"]
    if referer:
        cmd += ["--referer", referer]
    cmd += list(extra_args)
    cmd.append(url)
    try:
        return subprocess.run(cmd, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def probe_url(url: str, http: Client | None = None) -> dict:
    """HEAD/GET the start of a URL to learn its type and size."""
    http = http or default_client()
    info: dict[str, object] = {"url": url, "size": 0, "type": guess_kind(url), "ok": False}
    try:
        resp = http.head(url, allow_redirects=True)
        info["ok"] = 200 <= resp.status < 400
        info["type"] = resp.headers.get("content-type", info["type"])
        length = resp.headers.get("content-length")
        if length and length.isdigit():
            info["size"] = int(length)
        final = resp.headers.get("location")
        if final:
            info["url"] = absolute(url, final)
    except HttpError as exc:
        log.debug("probe failed for %s: %s", url, exc)
    return info


def parse_manifest(manifest_url: str, http: Client | None = None) -> list[dict]:
    """Parse an HLS master playlist into its variant streams."""
    http = http or default_client()
    try:
        text = http.get(manifest_url, cache=False).text
    except HttpError:
        return []
    if not text.lstrip().startswith("#EXTM3U"):
        return []
    base = manifest_url.rsplit("/", 1)[0] + "/"
    out: list[dict] = []
    current: dict[str, object] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            attrs = dict(re.findall(r'([A-Z\-]+)=("[^"]*"|[^,]*)', line))
            current = {
                "bandwidth": int(attrs.get("BANDWIDTH", "0").strip('"') or 0),
                "resolution": attrs.get("RESOLUTION", "").strip('"'),
                "name": attrs.get("NAME", "").strip('"'),
            }
        elif line and not line.startswith("#"):
            if current:
                current["url"] = absolute(manifest_url, line) if not line.startswith("http") else line
                if not current["url"].startswith("http"):
                    current["url"] = base + line
                out.append(current)
                current = {}
    return out
