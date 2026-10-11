"""Rule-driven scrapers: add a site by dropping a JSON file in ~/.aninext/sources/.

A rule file describes *where* the data lives on a page (CSS selectors) and
*how* to clean it up.  No code required, which matters a lot on Termux where
you cannot pip-install a scraper stack.

Minimal example
---------------
{
  "id": "mysite",
  "name": "My Site",
  "base": "https://mysite.example",
  "kinds": ["anime"],
  "search": {
    "url": "/search?q={query}&page={page}",
    "items": "div.card",
    "fields": {
      "title":  "h3 a ::text",
      "url":    "h3 a @href",
      "poster": "img @data-src | img @src",
      "year":   "span.year ::text"
    }
  },
  "details": {
    "fields": {
      "title":    "h1.entry-title ::text",
      "synopsis": "div.synopsis ::text",
      "poster":   "div.poster img @src",
      "genres":   "div.genres a ::text",
      "score":    "span.score ::text",
      "status":   "span.status ::text"
    },
    "episodes": {
      "items": "div.eplister li",
      "fields": {"number": "span.num ::text", "title": "a ::text", "url": "a @href"}
    }
  },
  "episode": {
    "servers": {
      "items": "div.server",
      "fields": {"label": "::text", "embed": "@data-embed | @data-src | @href"}
    },
    "downloads": {
      "items": "div.downloads a",
      "fields": {"url": "@href", "label": "::text", "quality": "::text"}
    }
  }
}

Expression syntax
-----------------
    "selector"                 text of the first match
    "selector ::text"          explicit text content
    "selector @href"           attribute value
    "selector html"            inner HTML
    "a @href | b @href"        fallbacks, first non-empty wins   ('||' also works)
    "@src" / "::text"          attribute / text of the current node

Field names that look like URLs (`url`, `poster`, `image`, `embed`, ...) are
resolved against the page URL automatically.  Extra cleanup steps live in an
optional "post" map, e.g.

    "post": {"url": ["url"], "number": ["regex:episode-(\\d+)", "number"]}
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .. import log
from ..htmldom import Node, extract, extract_all, select
from ..models import DownloadLink, Episode, SearchResult, Stream, Subtitle, Title, guess_kind
from ..paths import bundled_sources_dir, sources_dir
from .base import MediaProvider, ProviderError, SearchQuery, parse_episode_number, parse_size, quality_from_text

KINDS_HINT = ("anime", "movie", "series", "tv", "cartoon", "donghua")

AUDIO_TAGS = ("sub", "subbed", "dub", "dubbed", "raw", "eng", "english", "jpn", "japanese",
              "hindi", "multi", "multi-audio", "dual")

# --------------------------------------------------------------------------
# expression evaluation
# --------------------------------------------------------------------------

_FALLBACK_RE = re.compile(r"\s*\|\|\s*|\s+\|\s+")

_NUMERIC_HINT = re.compile(r"^\d+(?:\.\d+)?$")


def split_fallbacks(expr: str) -> list[str]:
    parts = [p.strip() for p in _FALLBACK_RE.split(expr or "")]
    return [p for p in parts if p]


def apply_transforms(value: str, ops: Iterable[str], base_url: str = "") -> str:
    from ..net import absolute

    for op in ops:
        op = (op or "").strip()
        if not op:
            continue
        name, _, arg = op.partition(":")
        name = name.strip().lower()
        arg = arg.strip()
        if name == "url":
            value = absolute(base_url, value)
        elif name == "text":
            from ..net import strip_html

            value = strip_html(value)
        elif name == "trim":
            value = value.strip()
        elif name == "lower":
            value = value.lower()
        elif name == "upper":
            value = value.upper()
        elif name == "title":
            value = value.title()
        elif name == "number":
            value = str(parse_episode_number(value))
        elif name == "int":
            m = re.search(r"-?\d+", value or "")
            value = m.group(0) if m else "0"
        elif name == "float":
            m = re.search(r"-?\d+(?:\.\d+)?", value or "")
            value = m.group(0) if m else "0"
        elif name == "size":
            value = str(parse_size(value))
        elif name == "quality":
            value = quality_from_text(value)
        elif name == "regex":
            pat, _, grp = arg.rpartition(":")
            if not pat:
                pat, grp = arg, "1"
            try:
                m = re.search(pat, value or "")
                value = (m.group(int(grp or 1)) if m and m.groups() else (m.group(0) if m else ""))
            except (re.error, ValueError, IndexError):
                value = ""
        elif name == "replace":
            a, _, b = arg.partition("=>")
            value = (value or "").replace(a, b)
        elif name == "split":
            value = (value or "").split(arg)[0] if arg else value
        elif name == "prefix":
            value = f"{arg}{value}"
        elif name == "suffix":
            value = f"{value}{arg}"
        elif name == "date":
            value = _normalise_date(value)
        else:
            log.debug("unknown transform %r", op)
    return value


def _normalise_date(value: str) -> str:
    value = (value or "").strip()
    m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", value)
    if m:
        months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
        try:
            return f"{m.group(3)}-{months.index(m.group(1)[:3].lower()) + 1:02d}-{int(m.group(2)):02d}"
        except ValueError:
            return value
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    return m.group(0) if m else value


def evaluate(
    node: Node | None,
    expr: str,
    *,
    base_url: str = "",
    transforms: Iterable[str] = (),
    multi: bool = False,
) -> Any:
    """Evaluate one field expression against a node."""
    if node is None:
        return [] if multi else ""

    exprs = split_fallbacks(expr)
    if multi:
        out: list[str] = []
        for e in exprs:
            out = extract_all(node, e)
            if out:
                break
        cleaned = [apply_transforms(v, transforms, base_url) for v in out]
        return [c for c in cleaned if c]

    for e in exprs:
        value = extract(node, e)
        if value:
            return apply_transforms(value, transforms, base_url)
    return ""


# --------------------------------------------------------------------------
# JSON path support (for sites that expose JSON endpoints)
# --------------------------------------------------------------------------

_PATH_TOKEN = re.compile(r"([^.[\]]+)|\[(\*|\d+)\]")


def json_path(data: Any, path: str) -> Any:
    """A tiny JSONPath: `data.results[*].title.romaji`, `@a.b`, `a[0].b`."""
    if not path:
        return data
    path = path.strip().lstrip("$").lstrip("@").strip()
    if path in ("", "."):
        return data
    if path == "::text" or path == "text":
        return data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)

    current: list[Any] = [data]
    for token in re.finditer(r"[^.[\]]+|\[(?:\*|\d+|'[^']*'|\"[^\"]*\")\]", path):
        part = token.group(0)
        nxt: list[Any] = []
        for item in current:
            if part.startswith("["):
                inner = part[1:-1].strip().strip("'\"")
                if isinstance(item, list):
                    if inner == "*":
                        nxt.extend(item)
                    else:
                        try:
                            nxt.append(item[int(inner)])
                        except (ValueError, IndexError):
                            pass
                elif isinstance(item, dict) and inner in item:
                    nxt.append(item[inner])
            else:
                if isinstance(item, dict):
                    if part in item:
                        nxt.append(item[part])
                elif isinstance(item, list) and part.isdigit():
                    try:
                        nxt.append(item[int(part)])
                    except IndexError:
                        pass
        current = nxt
        if not current:
            return []
    if len(current) == 1:
        return current[0]
    return current


def json_to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(json_to_str(v) for v in value if v is not None)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# --------------------------------------------------------------------------
# the provider
# --------------------------------------------------------------------------

class RuleProvider(MediaProvider):
    """A scraper defined entirely by a JSON rule document."""

    priority = 40

    def __init__(self, rule: dict[str, Any]) -> None:
        super().__init__(rule.get("config", {}) or {})
        self.rule = rule
        self.id = str(rule.get("id") or "rule").strip()
        self.name = str(rule.get("name") or self.id.title())
        self.kinds = tuple(rule.get("kinds") or ("anime", "movie", "series"))
        self.base = str(rule.get("base") or rule.get("base_url") or "").rstrip("/")
        self.language = rule.get("language", "en")
        self.priority = int(rule.get("priority", 40))
        self.note = rule.get("note", "")
        self.home = self.base
        self.type = str(rule.get("type") or "html").lower()  # html | json
        self.config = dict(rule.get("config") or {})
        self.config.setdefault("enabled", bool(rule.get("enabled", True)))
        self.config.setdefault("headers", dict(rule.get("headers") or {}))
        if rule.get("user_agent"):
            self.config["user_agent"] = rule["user_agent"]
        if self.base:
            self.config["headers"].setdefault("Referer", self.base + "/")
        self.meta_only = bool(rule.get("meta_only", False))
        self.source_path = rule.get("__path__", "")
        self._detail_urls: dict[str, str] = {}   # slug -> page found by searching

    # -- url templating ----------------------------------------------------

    def url(self, template: str, **values: Any) -> str:
        from ..net import absolute

        def repl(m: re.Match) -> str:
            key = m.group(1) or m.group(2)
            val = values.get(key, "")
            return "" if val is None else str(val)

        text = re.sub(r"\{(\w+)\}", repl, template or "")
        if text.startswith(("http://", "https://")):
            return text
        return absolute(self.base + "/", text)

    def _section(self, *keys: str) -> dict[str, Any]:
        cur: Any = self.rule
        for k in keys:
            if not isinstance(cur, dict):
                return {}
            cur = cur.get(k)
        return cur if isinstance(cur, dict) else {}

    # -- search ------------------------------------------------------------

    def search(self, query: SearchQuery) -> list[SearchResult]:
        cfg = self._section("search")
        if not cfg or not cfg.get("url"):
            raise ProviderError(f"{self.id}: no search rule defined")

        limit = max(1, query.limit)
        max_pages = max(1, int(cfg.get("max_pages", 1)))
        results: list[SearchResult] = []
        page = max(1, int(query.page or 1))
        pages_done = 0
        next_url: str | None = None

        while pages_done < max_pages and len(results) < limit:
            url = next_url or self.url(
                cfg["url"],
                query=query.text or "",
                query_plus=str(query.text or "").replace(" ", "+"),
                page=page,
                kind=query.kind or "",
                year=query.year or "",
                genre=query.genre or "",
                season=query.extra.get("season", ""),
            )
            items, follow = self._parse_search_page(cfg, url)
            if not items and pages_done:
                break
            results.extend(items)
            pages_done += 1
            if not follow or not items:
                break
            next_url = follow
            page += 1

        return results[:limit]

    def _parse_search_page(self, cfg: dict[str, Any], url: str) -> tuple[list[SearchResult], str | None]:
        """Return (results, next_page_url)."""
        items_expr = cfg.get("items") or ""
        fields = cfg.get("fields") or {}
        post = cfg.get("post") or {}
        kind = str(cfg.get("kind") or (self.kinds[0] if self.kinds else "anime"))

        if self.type == "json":
            data = self.get_json(url)
            rows = json_path(data, items_expr)
            if isinstance(rows, dict):
                rows = [rows]
            out = [self._search_row_from_json(row, fields, post, url, kind) for row in rows or []]
            follow = None
            if cfg.get("next"):
                follow = json_to_str(json_path(data, cfg["next"]))
            return [r for r in out if r and r.title], (follow or None)

        doc, resp = self.get_html(url)
        nodes = select(doc, items_expr) if items_expr else [doc]
        out = []
        for node in nodes:
            result = self._search_row(node, fields, post, resp.url or url, kind)
            if result and result.title:
                out.append(result)
        follow = self._next_url(doc, cfg.get("next") or "", resp.url or url) or None
        return out, follow

    def _search_row(self, node: Node, fields: dict, post: dict, base_url: str, kind: str) -> SearchResult | None:
        def val(name: str, node_: Node | None = None, *, multi: bool = False) -> Any:
            expr = fields.get(name)
            if not expr:
                return [] if multi else ""
            transforms = post.get(name) or self._auto_transforms(name)
            return evaluate(node_ if node_ is not None else node, expr, base_url=base_url,
                            transforms=transforms, multi=multi)

        url = val("url")
        if not url:
            return None
        title = val("title")
        if not title:
            title = url.rstrip("/").split("/")[-1].replace("-", " ").title()
        return SearchResult(
            title=title,
            url=url,
            provider=self.id,
            poster=val("poster"),
            year=val("year"),
            kind=self._kind_from_text(val("kind"), default=kind),
            score=val("score"),
            status=val("status"),
            episodes=int(float(val("episodes") or 0)) if str(val("episodes") or "").strip() else 0,
            synopsis=val("synopsis"),
        )

    def _search_row_from_json(self, row: Any, fields: dict, post: dict, base_url: str, kind: str) -> SearchResult | None:
        def val(name: str) -> str:
            expr = fields.get(name)
            if not expr:
                return ""
            value = ""
            for part in split_fallbacks(expr):
                raw = json_path(row, part)
                value = json_to_str(raw)
                if value:
                    break
            return apply_transforms(value, post.get(name) or self._auto_transforms(name), base_url)

        url = val("url")
        title = val("title")
        if not title and not url:
            return None
        return SearchResult(
            title=title or url,
            url=url or (self.base + "/" + str(json_path(row, fields.get("id", "id")) or "")),
            provider=self.id,
            poster=val("poster"),
            year=val("year"),
            kind=self._kind_from_text(val("kind"), default=kind),
            score=val("score"),
            status=val("status"),
            episodes=int(float(val("episodes") or 0)) if val("episodes").strip() else 0,
            synopsis=val("synopsis"),
        )

    def _kind_map(self) -> dict[str, str]:
        """Optional explicit mapping of the site's wording onto aninext kinds."""
        raw = self.rule.get("kind_map") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k).strip().lower(): str(v).strip().lower() for k, v in raw.items()}

    def _kind_from_text(self, text: str, *, default: str = "anime") -> str:
        """Map a site's wording onto aninext kinds.

        An explicit kind word ("anime", "series", "movie", "tv") wins; only
        site-specific wording ("OVA", "TV Special", ...) is inferred from the
        provider's primary kind.
        """
        low = (text or "").strip().lower()
        if not low:
            return default
        mapped = self._kind_map().get(low)
        if mapped in KINDS_HINT:
            return "series" if mapped == "tv" else mapped
        if low in KINDS_HINT:
            return "series" if low == "tv" else low
        return self._kind_from_type(low, default=default)

    def _kind_from_type(self, text: str, *, default: str = "anime") -> str:
        low = (text or "").strip().lower()
        primary = self.kinds[0] if self.kinds else "anime"
        if not low:
            return default
        mapped = self._kind_map().get(low)
        if mapped in KINDS_HINT:
            return "series" if mapped == "tv" else mapped
        if low in ("movie", "film", "feature"):
            return "movie"
        if low in ("ova", "ona", "special", "tv special", "music video", "music"):
            return primary
        if low in ("tv", "series", "tv series", "tv show", "webseries", "web series"):
            return "series" if primary != "anime" else "anime"
        return default

    # -- details -----------------------------------------------------------

    def details(self, url_or_id: str) -> Title:
        cfg = self._section("details")
        value = self.clean_ref(url_or_id)
        seed = None
        if value.startswith(("http://", "https://")):
            url = value
        elif cfg.get("url"):
            # e.g. "url": "/anime/{id}" turns a bare slug into a real page
            url = self.url(cfg["url"], url=value, id=value, slug=value, path=value)
        else:
            # no template: ask the site's own search for the page, and only
            # fall back to the "/<slug>" guess when nothing comes back
            url = ""
            if value in self._detail_urls:
                url = self._detail_urls[value]
            else:
                try:
                    found = self.search(SearchQuery(text=value, limit=1))
                except (ProviderError, HttpError):
                    found = []
                if found and found[0].url:
                    url = found[0].url
                    seed = found[0]
                    self._detail_urls[value] = url
            if not url:
                url = self.url("/{url}", url=value, id=value, slug=value)
        fields = cfg.get("fields") or {}
        post = cfg.get("post") or {}

        title = Title(title="", url=url, provider=self.id, kind=self.kinds[0] if self.kinds else "anime")
        if seed is not None:
            title.title = seed.title
            title.poster = seed.poster
            title.year = seed.year
            title.score = seed.score
            title.status = seed.status
            title.synopsis = seed.synopsis
            title.kind = seed.kind or title.kind

        if self.type == "json":
            data = self.get_json(url)
            row = json_path(data, cfg.get("root", "")) if cfg.get("root") else data
            collected: list[tuple[str, list[str]]] = []
            for name, expr in fields.items():
                values: list[Any] = []
                for part in split_fallbacks(expr):
                    raw = json_path(row, part)
                    if raw not in (None, "", []):
                        values = raw if isinstance(raw, list) else [raw]
                        break
                collected.append((name, [json_to_str(v) for v in values]))
            self._assign_fields(title, collected, post)
        else:
            doc, resp = self.get_html(url)
            base_url = resp.url or url
            title.url = base_url
            collected = []
            for name, expr in fields.items():
                transforms = post.get(name) or self._auto_transforms(name)
                values = evaluate(doc, expr, base_url=base_url, transforms=transforms, multi=True)
                collected.append((name, list(values or [])))
            self._assign_fields(title, collected, post)

            ep_cfg = cfg.get("episodes") or {}
            if ep_cfg.get("items"):
                title.episodes = self._episodes_with_pages(doc, ep_cfg, base_url)
            rel_cfg = cfg.get("related") or {}
            if rel_cfg.get("items"):
                title.related = self._parse_related(doc, rel_cfg, base_url)
            seasons_cfg = cfg.get("seasons") or {}
            if seasons_cfg.get("items"):
                title.seasons = self._parse_related(doc, seasons_cfg, base_url)

        if not title.title:
            title.title = url.rstrip("/").split("/")[-1].replace("-", " ").title()
        if not title.total_episodes and title.episodes:
            title.total_episodes = len(title.episodes)
        return title

    def _parse_related(self, doc: Node, cfg: dict[str, Any], base_url: str) -> list[dict[str, str]]:
        fields = cfg.get("fields") or {}
        out: list[dict[str, str]] = []
        for node in select(doc, cfg["items"]):
            def val(name: str) -> str:
                expr = fields.get(name)
                if not expr:
                    return ""
                return evaluate(node, expr, base_url=base_url, transforms=self._auto_transforms(name))

            url = val("url")
            name = val("title") or val("name")
            if not (url or name):
                continue
            out.append({"title": name, "url": url, "poster": val("poster"), "type": val("type") or "related"})
        return out

    def _assign_fields(self, title: Title, collected: list[tuple[str, list[str]]],
                       post: dict[str, Any]) -> None:
        """Assign scraped values, applying an explicit `kind` last.

        A rule that states the kind outright ("anime"/"series"/"movie") should
        win over anything inferred from a "type" field like "TV".
        """
        priority = {"kind": 1}
        ordered = sorted(collected, key=lambda item: priority.get(item[0], 0))
        for name, values in ordered:
            self._assign(title, name, values, post.get(name))

    def _assign(self, title: Title, name: str, values: list[str], transforms: Iterable[str] | None) -> None:
        if not values:
            return
        name = name.strip()
        joined = values if isinstance(values, list) else [values]
        joined = [v for v in joined if v]
        if not joined:
            return
        first = joined[0]

        list_fields = {"genres": "genres", "tags": "genres", "studios": "studios",
                       "alt_titles": "alt_titles", "aliases": "alt_titles"}
        if name in list_fields:
            flat: list[str] = []
            for v in joined:
                flat.extend([p.strip() for p in re.split(r"[,\u2022|/]", v) if p.strip()])
            setattr(title, list_fields[name], list(set(flat)))
            return
        if name == "episodes_count" or name == "total_episodes":
            m = re.search(r"\d+", first)
            title.total_episodes = int(m.group(0)) if m else 0
            return
        if name == "type" or name == "format":
            title.type_ = first
            title.kind = self._kind_from_type(first, default=title.kind)
            return
        if name == "kind":
            title.kind = self._kind_from_text(first, default=title.kind)
            return
        if name == "title":
            title.title = first
            return
        if name == "synopsis" or name == "description" or name == "summary":
            title.synopsis = first if len(joined) == 1 else " ".join(joined)
            return
        if name == "rating":
            title.rating = first
            return
        if name == "season":
            m = re.search(r"\d{4}", first)
            if m and not title.year:
                title.year = m.group(0)
            return
        attr = {
            "url": "url", "link": "url", "poster": "poster", "image": "poster",
            "banner": "banner", "cover": "poster", "year": "year", "status": "status",
            "score": "score", "country": "country", "duration": "duration",
        }.get(name)
        if attr:
            current = getattr(title, attr, "")
            # prefer a value that looks like an image/href over a placeholder
            if not current or (attr in ("poster", "banner") and "placeholder" in current.lower()):
                setattr(title, attr, first)
            return
        title.raw.setdefault(name, first)

    # -- episodes ----------------------------------------------------------

    def episodes(self, url_or_id: str) -> list[Episode]:
        title = self.details(url_or_id)
        return title.episodes

    def _next_url(self, doc: Node, expr: str, base_url: str) -> str:
        """Resolve a "next page" selector, preferring the href over link text."""
        from ..htmldom import extract as _extract
        from ..net import absolute as _absolute

        if not expr:
            return ""
        candidates: list[str] = []
        if "@" not in expr:
            candidates.append(_extract(doc, expr + " @href"))
        candidates.append(_extract(doc, expr))
        for value in candidates:
            if not value:
                continue
            url = _absolute(base_url, value)
            if url.startswith(("http://", "https://")):
                return url
        return ""

    def _episodes_with_pages(self, doc: Node, cfg: dict[str, Any], base_url: str) -> list[Episode]:
        """Collect episodes, following a "next page" link when the rule defines one."""
        episodes = self._parse_episodes(doc, cfg, base_url)
        next_expr = cfg.get("next")
        max_pages = int(cfg.get("max_pages", 6))
        page = 1
        while next_expr and page < max_pages:
            url = self._next_url(doc, next_expr, base_url)
            if not url or url == base_url:
                break
            try:
                doc, resp = self.get_html(url)
            except ProviderError:
                break
            base_url = resp.url or url
            more = self._parse_episodes(doc, cfg, base_url, start_index=len(episodes))
            if not more:
                break
            have = {e.number for e in episodes}
            fresh = [e for e in more if e.number not in have]
            if not fresh:
                break
            episodes.extend(fresh)
            page += 1
        return episodes

    def _parse_episodes(self, doc: Node, cfg: dict[str, Any], base_url: str,
                        start_index: int = 0) -> list[Episode]:
        fields = cfg.get("fields") or {}
        post = cfg.get("post") or {}
        episodes: list[Episode] = []
        seen: set[float] = set()
        nodes = select(doc, cfg["items"])
        index = start_index
        for node in nodes:
            index += 1

            def val(name: str, *, multi: bool = False) -> Any:
                expr = fields.get(name)
                if not expr:
                    return [] if multi else ""
                transforms = post.get(name) or self._auto_transforms(name)
                return evaluate(node, expr, base_url=base_url, transforms=transforms, multi=multi)

            url = val("url")
            raw_number = val("number")
            number = parse_episode_number(str(raw_number), default=float(index))
            if not raw_number:
                number = parse_episode_number(url, default=float(index))
            if number in seen:
                number = float(index)
            seen.add(number)

            audio_raw = val("audio", multi=True)
            audio: list[str] = []
            for chunk in (audio_raw if isinstance(audio_raw, list) else [audio_raw]):
                for part in re.split(r"[,/|]+|\s{1,}", str(chunk or "")):
                    part = part.strip().lower()
                    if part in AUDIO_TAGS and part not in audio:
                        audio.append(part)
            episode = Episode(
                number=number,
                title=val("title") or f"Episode {number:g}",
                url=url,
                thumbnail=val("thumbnail"),
                duration=val("duration"),
                synopsis=val("synopsis"),
                aired=val("aired"),
                filler="filler" in (str(val("type")) + str(val("title"))).lower(),
                recap="recap" in str(val("title")).lower(),
                audio=audio,
                provider=self.id,
                id=str(val("id") or ""),
            )
            episodes.append(episode)

        sort = str(cfg.get("sort") or "").lower()
        if sort == "desc" or cfg.get("reverse"):
            episodes.reverse()
        return episodes

    # -- streams -----------------------------------------------------------

    def streams(self, episode: Episode, *, title: Title | None = None) -> list[Stream]:
        if not episode.url:
            return []
        cfg = self._section("episode")
        servers = cfg.get("servers") or {}

        from ..resolve import resolve_embed, upgrade_embeds

        # 1. collect server / embed entries from the episode page (or json)
        embeds: list[tuple[str, str]] = []  # (label, url)
        if servers.get("items"):
            if self.type == "json" or servers.get("json"):
                data = self.get_json(episode.url)
                rows = json_path(data, servers["items"])
                if isinstance(rows, dict):
                    rows = [rows]
                for row in rows or []:
                    label = json_to_str(json_path(row, servers["fields"].get("label", "label")))
                    link = json_to_str(json_path(row, servers["fields"].get("embed", servers["fields"].get("url", "url"))))
                    if link:
                        embeds.append((label, link))
            else:
                doc, resp = self.get_html(episode.url)
                for node in select(doc, servers["items"]):
                    label = evaluate(node, servers["fields"].get("label", "::text"))
                    link = evaluate(
                        node,
                        servers["fields"].get("embed") or servers["fields"].get("url") or "@href",
                        base_url=resp.url or episode.url,
                        transforms=["url"],
                    )
                    if link:
                        embeds.append((label, link))
        elif servers.get("url"):
            embeds.append(("", self.url(servers["url"], url=episode.url, id=episode.id)))

        # 2. direct streams declared by the rule
        streams: list[Stream] = []
        for item in cfg.get("streams", []) or []:
            url = evaluate_static(item.get("url", ""), self, episode)
            if not url:
                continue
            streams.append(
                Stream(
                    url=url,
                    quality=item.get("quality", ""),
                    label=item.get("label", ""),
                    kind=item.get("kind", "") or guess_kind(url),
                    server=item.get("server", self.name),
                    language=item.get("language", ""),
                    referer=item.get("referer", self.base),
                    provider=self.id,
                )
            )

        # 3. resolve embeds to direct media (regex rules, then generic heuristics)
        if embeds:
            if cfg.get("resolve", {}).get("enabled", True):
                found = upgrade_embeds(
                    embeds,
                    client=self.client,
                    patterns=self._resolve_patterns(),
                    referer=episode.url,
                    provider=self.id,
                    server=self.name,
                    workers=int(cfg.get("resolve", {}).get("workers", 4)),
                )
                streams.extend(found)
            else:
                streams.extend(
                    Stream(url=u, kind="embed", label=lbl, server=self.name, provider=self.id,
                           referer=episode.url)
                    for lbl, u in embeds
                )

        # 4. optional site-specific resolver module hook
        resolver = self.rule.get("resolver")
        if resolver:
            try:
                module = __import__(resolver, fromlist=["resolve"])
                extra = module.resolve(episode, self) or []
                streams.extend(extra)
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: custom resolver failed: %s", self.id, exc)

        episode.streams = _dedupe(streams)
        return episode.streams

    def _resolve_patterns(self) -> list[str]:
        cfg = self._section("episode", "resolve")
        pats = cfg.get("patterns") or self.rule.get("resolve_patterns") or []
        if isinstance(pats, str):
            pats = [pats]
        return list(pats)

    def downloads(self, episode: Episode, *, title: Title | None = None) -> list[DownloadLink]:
        cfg = self._section("episode")
        dl_cfg = cfg.get("downloads") or {}
        if episode.downloads:
            return list(episode.downloads)
        if not (dl_cfg.get("items") and episode.url):
            return []
        fields = dl_cfg.get("fields") or {}
        post = dl_cfg.get("post") or {}

        if self.type == "json":
            data = self.get_json(episode.url)
            rows = json_path(data, dl_cfg["items"])
            if isinstance(rows, dict):
                rows = [rows]
            links = []
            for row in rows or []:
                url = json_to_str(json_path(row, fields.get("url", "url")))
                if not url:
                    continue
                label = json_to_str(json_path(row, fields.get("label", "label")))
                links.append(
                    DownloadLink(
                        url=apply_transforms(url, post.get("url") or ["url"], episode.url),
                        quality=quality_from_text(json_to_str(json_path(row, fields.get("quality", "quality"))) or label),
                        size=parse_size(json_to_str(json_path(row, fields.get("size", "size")))),
                        server=self.name,
                        referer=episode.url,
                    )
                )
            episode.downloads = links
            return links

        doc, resp = self.get_html(episode.url)
        links = []
        for node in select(doc, dl_cfg["items"]):
            url = evaluate(node, fields.get("url", "@href"), base_url=resp.url or episode.url,
                           transforms=post.get("url") or ["url"])
            if not url:
                continue
            label = evaluate(node, fields.get("label", "::text"))
            quality = evaluate(node, fields.get("quality", "::text")) or label
            size_text = evaluate(node, fields.get("size", "::text"))
            links.append(
                DownloadLink(
                    url=url,
                    quality=quality_from_text(quality) or quality_from_text(label),
                    size=parse_size(size_text),
                    server=self.name,
                    headers={"Referer": resp.url or episode.url},
                    referer=resp.url or episode.url,
                )
            )
        episode.downloads = links
        return links

    def subtitles(self, episode: Episode, *, title: Title | None = None):
        cfg = self._section("episode", "subtitles")
        if not (cfg.get("items") and episode.url):
            return list(episode.subtitles)
        from ..models import Subtitle

        fields = cfg.get("fields") or {}
        out: list[Subtitle] = []
        if self.type == "json":
            rows = json_path(self.get_json(episode.url), cfg["items"])
            for row in rows or []:
                url = json_to_str(json_path(row, fields.get("url", "url")))
                if url:
                    out.append(Subtitle(
                        label=json_to_str(json_path(row, fields.get("label", "label"))),
                        lang=json_to_str(json_path(row, fields.get("lang", "lang"))),
                        url=url,
                    ))
            episode.subtitles = out
            return out

        doc, resp = self.get_html(episode.url)
        for node in select(doc, cfg["items"]):
            url = evaluate(node, fields.get("url", "@src"), base_url=resp.url or episode.url,
                           transforms=["url"])
            if not url:
                continue
            out.append(Subtitle(
                label=evaluate(node, fields.get("label", "@label")) or "Subtitle",
                lang=evaluate(node, fields.get("lang", "@srclang")),
                url=url,
            ))
        episode.subtitles = out
        return out

    # -- helper ------------------------------------------------------------

    _URL_FIELDS = {"url", "link", "poster", "image", "banner", "cover", "embed", "thumbnail",
                   "href", "src", "stream", "server", "player"}

    def _auto_transforms(self, name: str) -> list[str]:
        """Fields named like URLs get resolved against the page automatically."""
        if name.lower() in self._URL_FIELDS:
            return ["url"]
        return []

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": "rules",
            "kinds": list(self.kinds),
            "base": self.base,
            "enabled": self.is_enabled(),
            "source": self.source_path,
            "note": self.note,
        }


def evaluate_static(template: str, provider: RuleProvider, episode: Episode) -> str:
    if not template:
        return ""
    return provider.url(
        template,
        url=episode.url,
        id=episode.id or "",
        episode=str(episode.number),
        number=f"{episode.number:g}",
        season=str(episode.season or 1),
        title=episode.title or "",
    )


def _dedupe(streams: list[Stream]) -> list[Stream]:
    seen: set[str] = set()
    out: list[Stream] = []
    for s in streams:
        key = re.sub(r"[?&](t|start|apikey|token)=[^&]*", "", s.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# --------------------------------------------------------------------------
# loading rules from disk
# --------------------------------------------------------------------------

def rule_files(extra_dir: str | os.PathLike | None = None) -> list[Path]:
    files: list[Path] = []
    for directory in (bundled_sources_dir(), sources_dir()):
        try:
            if directory.is_dir():
                files.extend(sorted(directory.glob("*.json")))
        except OSError:
            continue
    if extra_dir:
        p = Path(extra_dir)
        if p.is_dir():
            files.extend(sorted(p.glob("*.json")))
        elif p.is_file() and p.suffix == ".json":
            files.append(p)
    return files


def parse_rule(data: dict[str, Any], path: str = "") -> RuleProvider:
    rule = dict(data)
    rule["__path__"] = path
    return RuleProvider(rule)


def load_all(extra_dir: str | os.PathLike | None = None) -> list[RuleProvider]:
    providers: list[RuleProvider] = []
    seen: set[str] = set()
    for path in rule_files(extra_dir):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            log.warn("skipping rule file %s: %s", path.name, exc)
            continue
        entries = data.get("providers") if isinstance(data, dict) and "providers" in data else [data]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            pid = str(entry.get("id") or path.stem)
            if pid in seen:
                log.debug("rule provider %s already loaded (bundled?)", pid)
                continue
            entry.setdefault("id", pid)
            try:
                providers.append(parse_rule(entry, str(path)))
                seen.add(pid)
            except Exception as exc:  # noqa: BLE001
                log.warn("bad rule in %s: %s", path.name, exc)
    return providers


def save_rule(rule: dict[str, Any], *, name: str | None = None) -> Path:
    """Write a user rule to ~/.aninext/sources/<id>.json."""
    sources_dir().mkdir(parents=True, exist_ok=True)
    pid = name or str(rule.get("id") or "custom")
    path = sources_dir() / f"{pid}.json"
    path.write_text(json.dumps(rule, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return path


def validate_rule(rule: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not rule.get("id"):
        problems.append("missing 'id'")
    if not rule.get("base") and not rule.get("base_url"):
        problems.append("missing 'base' (site root URL)")
    search = rule.get("search") or {}
    if not search.get("url"):
        problems.append("search.url is required")
    if not search.get("items"):
        problems.append("search.items (row selector) is required")
    if not (search.get("fields") or {}).get("url"):
        problems.append("search.fields.url is required to link to detail pages")
    for key in ("details", "episode", "search"):
        section = rule.get(key)
        if section is not None and not isinstance(section, dict):
            problems.append(f"'{key}' must be an object")
    return problems
