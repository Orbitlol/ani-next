"""HTTP client built on urllib - no `requests` needed.

Why not requests?  Termux runs Android's Bionic libc, so PyPI's manylinux
wheels are unusable: anything with a C extension has to be compiled locally
(and often fails).  urllib is in the stdlib, so aninext works on a bare
`pkg install python`.

Features: connection reuse via keep-alive-less pool, gzip/deflate/br decoding,
retry with exponential backoff + jitter, per-host rate limiting, cookie jar,
redirect capture, on-disk cache, and a thread-pool map helper.
"""
from __future__ import annotations

import gzip
import hashlib
import http.cookiejar
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from . import log
from .paths import cache_dir

try:  # brotli is optional; Android builds rarely ship it
    import brotli as _brotli  # type: ignore
except Exception:  # pragma: no cover
    _brotli = None


DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"
)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed: float = 0.0
    from_cache: bool = False

    @property
    def text(self) -> str:
        """Decode the body using charset from headers, meta tags or utf-8."""
        ctype = self.headers.get("content-type", "")
        charset = ""
        m = re.search(r"charset=([\w\-]+)", ctype, re.I)
        if m:
            charset = m.group(1)
        if not charset:
            head = self.body[:4096].decode("ascii", "ignore")
            m = re.search(r'<meta[^>]+charset=["\']?([\w\-]+)', head, re.I)
            if m:
                charset = m.group(1)
        for enc in (charset, "utf-8", "latin-1"):
            if not enc:
                continue
            try:
                return self.body.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Response {self.status} {len(self.body)}B {self.url[:70]}>"


def _decompress(body: bytes, encoding: str) -> bytes:
    if not encoding:
        return body
    enc = encoding.lower()
    try:
        if enc == "gzip" or enc == "x-gzip":
            return gzip.decompress(body)
        if enc == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        if enc == "br" and _brotli is not None:
            return _brotli.decompress(body)
    except Exception:
        return body
    return body


# --------------------------------------------------------------------------
# per-host rate limiter
# --------------------------------------------------------------------------

class _RateLimiter:
    """Keeps at least `delay` seconds between requests to the same host."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._guard = threading.Lock()

    def wait(self, host: str, delay: float) -> None:
        if delay <= 0:
            return
        with self._guard:
            lock = self._locks.setdefault(host, threading.Lock())
        with lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            sleep = delay - (now - last)
            if sleep > 0:
                time.sleep(sleep)
            self._last[host] = time.monotonic()


# --------------------------------------------------------------------------
# disk cache
# --------------------------------------------------------------------------

class DiskCache:
    def __init__(self, path: Path | None = None, ttl: int = 3600, max_entries: int = 4000) -> None:
        self.path = path or (cache_dir() / "http")
        self.ttl = ttl
        self.max_entries = max_entries
        try:
            self.path.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.path = Path("/tmp") / "aninext-cache"
            self.path.mkdir(parents=True, exist_ok=True)

    def _entry(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.path / digest[:2] / f"{digest}.json"

    def get(self, key: str) -> Response | None:
        p = self._entry(key)
        try:
            raw = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if self.ttl >= 0 and time.time() - raw.get("stored", 0) > self.ttl:
            try:
                p.unlink()
            except OSError:
                pass
            return None
        try:
            body = bytes.fromhex(raw["body"])
        except (KeyError, ValueError):
            return None
        return Response(
            url=raw.get("url", key),
            status=raw.get("status", 200),
            headers=raw.get("headers", {}),
            body=body,
            from_cache=True,
        )

    def put(self, key: str, resp: Response) -> None:
        # don't cache big binaries - the http cache is for html/json/api calls
        if len(resp.body) > 4 * 1024 * 1024:
            return
        p = self._entry(key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(
                    {
                        "stored": time.time(),
                        "url": resp.url,
                        "status": resp.status,
                        "headers": resp.headers,
                        "body": resp.body.hex(),
                    }
                ),
                "utf-8",
            )
        except OSError:
            return

    def clear(self) -> int:
        n = 0
        for f in self.path.rglob("*.json"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
        return n

    def size(self) -> int:
        total = 0
        for f in self.path.rglob("*.json"):
            try:
                total += f.stat().st_size
            except OSError:
                pass
        return total


def _prune_cache(directory: Path, max_entries: int) -> None:
    try:
        files = sorted(directory.rglob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return
    for f in files[max_entries:]:
        try:
            f.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------

@dataclass
class Client:
    """A small, polite HTTP client."""

    user_agent: str = DEFAULT_UA
    timeout: float = 25.0
    retries: int = 3
    backoff: float = 0.8
    delay: float = 0.0  # per-host seconds between requests
    use_cache: bool = True
    cache_ttl: int = 3600
    headers: dict[str, str] = field(default_factory=dict)

    _cache: DiskCache | None = field(default=None, init=False, repr=False)
    _limiter: _RateLimiter = field(default_factory=_RateLimiter, init=False, repr=False)
    _cookies: http.cookiejar.CookieJar = field(default_factory=http.cookiejar.CookieJar, init=False, repr=False)
    _stats: dict[str, int] = field(
        default_factory=lambda: {"requests": 0, "cached": 0, "errors": 0, "retries": 0},
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.use_cache:
            self._cache = DiskCache(ttl=self.cache_ttl)

    # -- public API --------------------------------------------------------

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cache: bool | None = None,
        cache_ttl: int | None = None,
        referer: str | None = None,
        allow_redirects: bool = True,
    ) -> Response:
        if params:
            url = add_query(url, params)
        key = url if cache is None or cache else None
        want_cache = self.use_cache if cache is None else cache

        if want_cache and self._cache is not None:
            hit = self._cache.get(url)
            if hit is not None:
                self._stats["cached"] += 1
                return hit

        resp = self.request(
            url,
            headers=headers,
            referer=referer,
            allow_redirects=allow_redirects,
            method="GET",
        )
        if want_cache and self._cache is not None and 200 <= resp.status < 300:
            self._cache.put(url, resp)
            if self._cache.max_entries:
                _prune_cache(self._cache.path, self._cache.max_entries)
        return resp

    def post(
        self,
        url: str,
        data: dict[str, Any] | str | bytes | None = None,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        referer: str | None = None,
        method: str = "POST",
    ) -> Response:
        hdrs = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data or b""
        return self.request(url, headers=hdrs, referer=referer, method=method, body=body)

    def head(self, url: str, **kw) -> Response:
        return self.request(url, method="HEAD", **kw)

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        referer: str | None = None,
        allow_redirects: bool = True,
    ) -> Response:
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
            url = "https://" + url

        host = urllib.parse.urlsplit(url).netloc
        self._limiter.wait(host, self.delay)

        last_exc: Exception | None = None
        for attempt in range(max(1, self.retries)):
            hdrs = {
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate" + (", br" if _brotli else ""),
                "Connection": "close",
            }
            hdrs.update(self.headers)
            if referer:
                hdrs["Referer"] = referer
            if headers:
                hdrs.update({k: v for k, v in headers.items() if v is not None})

            req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
            opener = self._opener(allow_redirects)
            started = time.monotonic()
            try:
                with opener.open(req, timeout=self.timeout) as raw:
                    payload = raw.read()
                    payload = _decompress(payload, raw.headers.get("Content-Encoding", ""))
                    resp_headers = {k.lower(): v for k, v in raw.headers.items()}
                    self._stats["requests"] += 1
                    return Response(
                        url=raw.geturl(),
                        status=getattr(raw, "status", None) or raw.getcode(),
                        headers=resp_headers,
                        body=payload,
                        elapsed=time.monotonic() - started,
                    )
            except urllib.error.HTTPError as exc:
                status = exc.code
                payload = b""
                try:
                    payload = _decompress(exc.read(), exc.headers.get("Content-Encoding", "") if exc.headers else "")
                except Exception:
                    pass
                resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                last_exc = HttpError(f"HTTP {status} for {url}", status, url)
                # 4xx (except 429) will not fix itself - fail fast
                if 400 <= status < 500 and status not in (408, 425, 429):
                    self._stats["errors"] += 1
                    err = last_exc
                    err.body = payload  # type: ignore[attr-defined]
                    err.headers = resp_headers  # type: ignore[attr-defined]
                    raise err from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_exc = HttpError(f"{type(exc).__name__}: {exc}", None, url)

            if attempt < self.retries - 1:
                self._stats["retries"] += 1
                sleep = self.backoff * (2**attempt) + random.random() * 0.3
                log.debug("retry %d/%d in %.1fs: %s", attempt + 1, self.retries, sleep, url[:90])
                time.sleep(sleep)

        self._stats["errors"] += 1
        raise last_exc or HttpError(f"request failed: {url}", None, url)

    def _opener(self, allow_redirects: bool):
        handlers: list[Any] = []
        if not allow_redirects:
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
                    return None
            handlers.append(_NoRedirect())
        handlers.append(urllib.request.HTTPCookieProcessor(self._cookies))
        # Some CDNs reject the default Python opener signature
        handlers.append(urllib.request.HTTPSHandler())
        return urllib.request.build_opener(*handlers)

    # -- convenience -------------------------------------------------------

    def text(self, url: str, **kw) -> str:
        return self.get(url, **kw).text

    def json(self, url: str, **kw) -> Any:
        return self.get(url, **kw).json()

    def download(self, url: str, dest: Path, *, referer: str | None = None,
                 progress: Callable[[int, int], None] | None = None,
                 resume: bool = True, chunk: int = 1 << 16) -> Path:
        """Stream a URL to disk with optional resume support."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        offset = part.stat().st_size if resume and part.exists() else 0

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if referer:
            headers["Referer"] = referer
        if offset:
            headers["Range"] = f"bytes={offset}-"

        req = urllib.request.Request(url, headers=headers)
        opener = self._opener(True)
        try:
            raw = opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and offset:  # already complete
                part.replace(dest)
                return dest
            raise HttpError(f"HTTP {exc.code} for {url}", exc.code, url) from None
        except (urllib.error.URLError, OSError) as exc:
            raise HttpError(str(exc), None, url) from exc

        with raw:
            if offset and raw.status != 206:
                offset = 0  # server ignored Range; start over
            total = 0
            clen = raw.headers.get("Content-Length")
            try:
                total = int(clen) + offset if clen else 0
            except ValueError:
                total = 0

            mode = "r+b" if offset else "wb"
            if offset:
                fh = open(part, mode)
                fh.seek(offset)
            else:
                fh = open(part, "wb")
            with fh:
                written = offset
                while True:
                    block = raw.read(chunk)
                    if not block:
                        break
                    fh.write(block)
                    written += len(block)
                    if progress:
                        progress(written, total)

        part.replace(dest)
        return dest

    def iter_lines(self, url: str, **kw) -> Iterator[str]:
        resp = self.get(url, **kw)
        for line in resp.text.splitlines():
            yield line

    def close(self) -> None:
        self._cookies.clear()


# --------------------------------------------------------------------------
# module level default client + helpers
# --------------------------------------------------------------------------

_default: Client | None = None
_default_lock = threading.Lock()


def client() -> Client:
    global _default
    with _default_lock:
        if _default is None:
            _default = Client()
        return _default


def configure(**kw) -> Client:
    """Replace the module-level default client (used by the CLI for --proxy etc.)."""
    global _default
    with _default_lock:
        _default = Client(**kw)
        return _default


def get(url: str, **kw) -> Response:
    return client().get(url, **kw)


def get_json(url: str, **kw) -> Any:
    return client().json(url, **kw)


def add_query(url: str, params: dict[str, Any]) -> str:
    parts = urllib.parse.urlsplit(url)
    existing = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    extra = [(k, str(v)) for k, v in params.items() if v is not None]
    query = urllib.parse.urlencode(existing + extra)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def absolute(base: str, link: str) -> str:
    """Resolve `link` against `base`.

    Handles protocol-relative URLs, and the `//host/path` and `?q=` forms that
    scraped pages love to emit.
    """
    if not link:
        return ""
    link = link.strip().replace("\n", "").replace("\t", "")
    if link.startswith(("data:", "javascript:", "mailto:", "tel:")):
        return ""
    if link.startswith("//"):
        scheme = urllib.parse.urlsplit(base).scheme or "https"
        return f"{scheme}:{link}"
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", link):
        return link
    return urllib.parse.urljoin(base, link)


def same_host(a: str, b: str) -> bool:
    def norm(h: str) -> str:
        return h.lower().removeprefix("www.")
    return norm(urllib.parse.urlsplit(a).netloc) == norm(urllib.parse.urlsplit(b).netloc)


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")


def parallel(
    fn: Callable[[Any], Any],
    items: Sequence[Any],
    workers: int = 6,
    on_error: str = "skip",
) -> list[Any]:
    """Map `fn` over `items` concurrently, preserving input order.

    on_error: "skip" (drop failures), "raise" (propagate), "none" (keep None).
    """
    items = list(items)
    if not items:
        return []
    if workers <= 1 or len(items) == 1:
        out = []
        for it in items:
            try:
                out.append(fn(it))
            except Exception as exc:  # noqa: BLE001
                if on_error == "raise":
                    raise
                log.debug("parallel task failed: %s", exc)
                if on_error == "none":
                    out.append(None)
        return out

    results: list[Any] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): idx for idx, item in enumerate(items)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:  # noqa: BLE001
                if on_error == "raise":
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
                log.debug("parallel task failed: %s", exc)
    if on_error == "skip":
        return [r for r in results if r is not None]
    return results


def strip_html(text: str) -> str:
    """Remove tags & collapse whitespace (used for synopses)."""
    from .htmldom import parse

    if "<" in text and ">" in text:
        doc = parse(text)
        text = doc.text_content()
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
