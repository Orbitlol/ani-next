"""Offline demo provider.

Serves a small fake streaming site from memory so `aninext` can be tried,
tested and benchmarked with no network at all.  It goes through the very same
rule engine that real sites use (search -> details -> episodes -> server
resolution -> downloads) and one of its embeds is packed with the classic
Dean Edwards packer, so it doubles as an end-to-end integration test.

    aninext --provider demo search "frieren"
    aninext --provider demo play frieren 1
    aninext --provider demo download frieren 1-3
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from ..net import Client, HttpError, Response
from .rules import RuleProvider

BASE = "https://demo.aninext.local"
PER_PAGE = 2          # search results per page (to exercise pagination)
EPS_PER_PAGE = 4      # episodes per page


# --------------------------------------------------------------------------
# the fake catalogue
# --------------------------------------------------------------------------

CATALOG: list[dict[str, Any]] = [
    {
        "slug": "frieren",
        "title": "Frieren: Beyond Journey's End",
        "kind": "anime",
        "year": "2023",
        "score": "9.0",
        "status": "Completed",
        "type": "TV",
        "genres": ["Adventure", "Drama", "Fantasy"],
        "studio": "Madhouse",
        "poster": "/img/frieren.jpg",
        "banner": "/img/frieren-banner.jpg",
        "synopsis": (
            "After the party of heroes defeated the Demon King, the elven mage Frieren "
            "sets out to understand the fleeting lives of the humans she once travelled with."
        ),
        "episodes": [
            ("The Journey's End", "24m", "2023-09-29"),
            ("It Didn't Have to Be Magic", "24m", "2023-09-29"),
            ("Mage Killer", "24m", "2023-09-29"),
            ("The Land Where Souls Rest", "24m", "2023-09-29"),
            ("Phantoms of the Dead", "24m", "2023-10-06"),
            ("The Hero of the Village", "24m", "2023-10-13"),
            ("Like a Fairy Tale", "24m", "2023-10-20"),
            ("Frieren the Slayer", "24m", "2023-10-27"),
        ],
    },
    {
        "slug": "severance",
        "title": "Severance",
        "kind": "series",
        "year": "2022",
        "score": "8.7",
        "status": "Ongoing",
        "type": "TV",
        "genres": ["Drama", "Mystery", "Sci-Fi"],
        "studio": "Red Hour",
        "poster": "/img/severance.jpg",
        "banner": "",
        "synopsis": "Office workers whose memories are surgically split between work and home life.",
        # 4-tuples are (season, title, duration, aired) - two seasons here so the
        # season picker in `aninext severance` has something to show offline
        "episodes": [
            (1, "Good News About Hell", "57m", "2022-02-18"),
            (1, "Half Loop", "53m", "2022-02-18"),
            (1, "In Perpetuity", "56m", "2022-02-25"),
            (1, "The You You Are", "48m", "2022-03-04"),
            (1, "The Grim Barbarity of Optics and Design", "46m", "2022-03-11"),
            (2, "Hello, Ms. Cobel", "56m", "2025-01-17"),
            (2, "Goodbye, Mrs. Selvig", "50m", "2025-01-17"),
            (2, "Who Is Alive?", "52m", "2025-01-24"),
            (2, "Woe's Hollow", "49m", "2025-01-31"),
        ],
    },
    {
        "slug": "blade-runner-2049",
        "title": "Blade Runner 2049",
        "kind": "movie",
        "year": "2017",
        "score": "8.0",
        "status": "Completed",
        "type": "Movie",
        "genres": ["Sci-Fi", "Thriller"],
        "studio": "Alcon",
        "poster": "/img/br2049.jpg",
        "banner": "/img/br2049-banner.jpg",
        "synopsis": "A young blade runner unearths a secret that could plunge what is left of society into chaos.",
        "episodes": [("Full Movie", "2h 44m", "2017-10-06")],
    },
]

_STREAMS = {
    "hls": "https://cdn.demo-aninext.test/hls/{slug}/master.m3u8",
    "backup": "https://backup.demo-aninext.test/vod/{slug}-{ep}-1080p.mp4",
    "low": "https://backup.demo-aninext.test/vod/{slug}-{ep}-480p.mp4",
}


def _episode_slug(item: dict[str, Any], season: int, number: int) -> str:
    """`severance-s2e1` for multi-season titles, `frieren-3` otherwise."""
    if season > 0 and len(season_numbers(item)) > 1:
        return f"s{season}e{number}"
    return str(number)


def episode_rows(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Episode entries as dicts, accepting both 3- and 4-tuples.

    A 4-tuple is (season, title, duration, aired); 3-tuples belong to season 1
    when the title has several seasons, and to "no season" otherwise.
    """
    raw = item.get("episodes") or []
    multi = any(len(row) == 4 for row in raw)
    rows: list[dict[str, Any]] = []
    for row in raw:
        if len(row) == 4:
            season, name, duration, aired = row
        else:
            name, duration, aired = row
            season = 1 if multi else 0
        rows.append({"season": int(season), "title": name, "duration": duration, "aired": aired})
    return rows


def season_numbers(item: dict[str, Any]) -> list[int]:
    return sorted({row["season"] for row in episode_rows(item) if row["season"] > 0})


def _find(slug_or_query: str) -> dict[str, Any] | None:
    needle = slug_or_query.strip().lower()
    for item in CATALOG:
        if item["slug"] == needle:
            return item
    for item in CATALOG:
        if needle in item["title"].lower() or needle in item["slug"]:
            return item
    return None


# --------------------------------------------------------------------------
# html rendering
# --------------------------------------------------------------------------

def _search_page(query: str, page: int) -> str:
    needle = query.strip().lower()
    matches = [
        item for item in CATALOG
        if not needle
        or needle in item["title"].lower()
        or needle in item["slug"]
        or any(needle in g.lower() for g in item["genres"])
    ]
    start = (page - 1) * PER_PAGE
    chunk = matches[start:start + PER_PAGE]

    cards = []
    for item in chunk:
        cards.append(
            f"""
      <article class="card item" data-id="{item['slug']}" data-kind="{item['kind']}">
        <a class="poster" href="/anime/{item['slug']}">
          <img data-src="{item['poster']}" alt="{item['title']} poster">
        </a>
        <h3 class="title"><a href="/anime/{item['slug']}">{item['title']}</a></h3>
        <div class="meta">
          <span class="year">{item['year']}</span>
          <span class="type">{item['type']}</span>
          <span class="eps-count">{len(item['episodes'])} eps</span>
          <span class="score">{item['score']}</span>
          <span class="status">{item['status']}</span>
        </div>
        <p class="synopsis">{item['synopsis']}</p>
      </article>"""
        )

    next_link = ""
    if start + PER_PAGE < len(matches):
        next_link = f'<a class="next" rel="next" href="/search?q={query}&page={page + 1}">Next</a>'

    if not cards:
        cards = ['<p class="no-results">No results found.</p>']

    return f"""<!doctype html>
<html><head><title>Demo site - search {query}</title></head>
<body>
  <header><form action="/search"><input name="q" value="{query}"></form></header>
  <div class="anime-list" id="results">{"".join(cards)}</div>
  <nav class="pagination">{next_link}</nav>
</body></html>"""


def _detail_page(item: dict[str, Any], page: int) -> str:
    episodes = episode_rows(item)
    start = (page - 1) * EPS_PER_PAGE
    chunk = list(enumerate(episodes, start=1))[start:start + EPS_PER_PAGE]

    # episode numbers restart in every season, exactly like a real site
    counters: dict[int, int] = {}
    numbers: list[int] = []
    for row in episodes:
        counters[row["season"]] = counters.get(row["season"], 0) + 1
        numbers.append(counters[row["season"]])

    rows = []
    current_season = None
    for index, (number, row) in enumerate(chunk):
        season = row["season"]
        if season and season != current_season and start == 0:
            current_season = season
            rows.append(f'\n      <h3 class="season-head" data-season="{season}">Season {season}</h3>')
        rows.append(
            f"""
      <li class="episode" data-number="{numbers[start + index]}" data-season="{season}" data-audio="sub dub">
        <a href="/watch/{item['slug']}-{_episode_slug(item, season, numbers[start + index])}">
          <span class="ep-num">{number}</span>
          <span class="ep-title">{row['title']}</span>
        </a>
        <span class="duration">{row['duration']}</span>
        <span class="aired" data-date="{row['aired']}">{row['aired']}</span>
      </li>"""
        )
    next_link = ""
    if start + EPS_PER_PAGE < len(episodes):
        next_link = (
            f'<a class="next-page" rel="next" href="/anime/{item["slug"]}?page={page + 1}">Next page</a>'
        )

    genres = "".join(f'<a href="/genre/{g.lower()}">{g}</a>' for g in item["genres"])

    return f"""<!doctype html>
<html><head><title>{item['title']} - Demo site</title></head>
<body>
  <div class="anime-detail" data-slug="{item['slug']}" data-kind="{item['kind']}">
    <h1 class="entry-title">{item['title']}</h1>
    <div class="poster"><img src="{item['poster']}" alt="{item['title']}"></div>
    <div class="info">
      <span class="score">{item['score']}</span>
      <span class="year">{item['year']}</span>
      <span class="type">{item['type']}</span>
      <span class="status">{item['status']}</span>
      <span class="studio">{item['studio']}</span>
      <span class="total-eps">{len(episodes)} eps</span>
    </div>
    <div class="genres">{genres}</div>
    <div class="description"><p>{item['synopsis']}</p></div>
    <div class="eplister">
      <ol class="episodes">{"".join(rows)}</ol>
      <div class="pagination">{next_link}</div>
    </div>
    <div class="related">
      <h4>Related</h4>
      <ul>
        <li><a href="/anime/severance">Severance</a><span class="rel-type">Series</span></li>
        <li><a href="/anime/blade-runner-2049">Blade Runner 2049</a><span class="rel-type">Movie</span></li>
      </ul>
    </div>
  </div>
</body></html>"""


def _episode_page(item: dict[str, Any], number: int, season: int = 0) -> str:
    slug = item["slug"]
    if not season:
        season = 1 if len(season_numbers(item)) > 1 else 0
    label = f"Season {season} " if season else ""
    servers = [
        ("HLS Server", f"/embed/hls?slug={slug}&ep={number}"),
        ("Backup 1080p", f"/embed/mp4?slug={slug}&ep={number}&q=1080p"),
        ("Direct MP4", _STREAMS["backup"].format(slug=slug, ep=number)),
    ]
    server_html = "".join(
        f'<div class="server-item" data-server="{name}">'
        f'<a class="server-link" data-embed="{url}" href="#">{name}</a></div>'
        for name, url in servers
    )
    downloads = "".join(
        f'<a class="download-link" href="{url}" data-quality="{q}" data-size="{s}">'
        f'{q} · {s}</a>'
        for q, s, url in (
            ("1080p", "1.4 GB", f"https://dl.demo-aninext.test/{slug}/{number}/1080p.mp4"),
            ("720p", "820 MB", f"https://dl.demo-aninext.test/{slug}/{number}/720p.mp4"),
            ("480p", "410 MB", f"https://dl.demo-aninext.test/{slug}/{number}/480p.mp4"),
        )
    )
    subtitle_html = (
        '<track kind="subtitles" label="English" srclang="en" '
        f'src="https://subs.demo-aninext.test/{slug}/{number}/en.vtt">'
        '<track kind="subtitles" label="Spanish" srclang="es" '
        f'src="https://subs.demo-aninext.test/{slug}/{number}/es.vtt">'
    )
    return f"""<!doctype html>
<html><head><title>Watch {item['title']} episode {number}</title></head>
<body>
  <div class="watch-page">
    <h1 class="entry-title">{item['title']} - {label}Episode {number}</h1>
    <div class="servers" id="servers">{server_html}</div>
    <div class="downloads" id="downloads">{downloads}</div>
    <div class="subtitles">{subtitle_html}</div>
    <video class="player" controls></video>
  </div>
</body></html>"""


def _pack(script: str, base: int = 36) -> str:
    """Produce a real Dean Edwards packed payload (used to keep us honest)."""
    words = sorted(set(re.findall(r"[A-Za-z0-9_$]+", script)), key=len, reverse=True)
    index = {w: i for i, w in enumerate(words)}

    def digit(value: int) -> str:
        return chr(value + 29) if value > 35 else "0123456789abcdefghijklmnopqrstuvwxyz"[value]

    def encode(value: int) -> str:
        return digit(value) if value < base else encode(value // base) + digit(value % base)

    payload = re.sub(
        r"[A-Za-z0-9_$]+",
        lambda m: encode(index[m.group(0)]) if m.group(0) in index else m.group(0),
        script,
    ).replace("'", "\\'")
    return (
        "eval(function(p,a,c,k,e,d){e=function(c){return(c<a?'':e(parseInt(c/a)))+"
        "((c=c%a)>35?String.fromCharCode(c+29):c.toString(36))};if(!''.replace(/^/,String))"
        "{while(c--){d[e(c)]=k[c]||e(c)}k=[function(e){return d[e]}];e=function(){return'\\\\w+'};c=1};"
        "while(c--){if(k[c]){p=p.replace(new RegExp('\\\\b'+e(c)+'\\\\b','g'),k[c])}}return p}"
        f"('{payload}',{base},{len(words)},'{'|'.join(words)}'.split('|'),0,{{}}))"
    )


def _embed_page(kind: str, slug: str, number: int, quality: str) -> str:
    if kind == "mp4":
        url = _STREAMS["backup" if quality == "1080p" else "low"].format(slug=slug, ep=number)
        return f"""<!doctype html>
<html><body>
  <video controls autoplay><source src="{url}" type="video/mp4" label="{quality}"></video>
  <script>var player = {{file: "{url}", type: "mp4"}};</script>
</body></html>"""

    # HLS page: one URL only appears inside packed javascript, another only
    # after base64 decoding - exactly what real players do.
    b64 = base64.b64encode(
        _STREAMS["backup"].format(slug=slug, ep=number).encode("utf-8")
    ).decode("ascii")
    script = (
        f'var config = {{"src": "{_STREAMS["hls"].format(slug=slug)}", "type": "hls"}};'
        " player.setup({file: config.src});"
    )
    return f"""<!doctype html>
<html><head><title>hls player</title></head><body>
  <div id="player"></div>
  <script src="/static/player.js"></script>
  <script>{_pack(script)}</script>
  <script>
    var _0x1a = "{b64}";
  </script>
</body></html>"""


# --------------------------------------------------------------------------
# local client
# --------------------------------------------------------------------------

class _LocalClient(Client):
    """Serves the fake site; only the demo host resolves."""

    def __init__(self, **kw) -> None:
        kw.setdefault("use_cache", False)
        super().__init__(**kw)
        self.served = 0

    def get(self, url: str, **kw) -> Response:  # type: ignore[override]
        from urllib.parse import parse_qs, urlsplit

        parts = urlsplit(url)
        if parts.netloc and parts.netloc != urlsplit(BASE).netloc:
            raise HttpError(f"demo provider refuses to fetch {url}", None, url)
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        path = parts.path or "/"
        self.served += 1
        html: str | None = None

        if path.startswith("/search"):
            html = _search_page(params.get("q", ""), int(params.get("page", 1) or 1))
        elif path.startswith("/anime/"):
            slug = path.split("/anime/", 1)[1].strip("/")
            item = _find(slug)
            if item is None:
                return Response(url=url, status=404, headers={}, body=b"not found")
            html = _detail_page(item, int(params.get("page", 1) or 1))
        elif path.startswith("/watch/"):
            raw = path.split("/watch/", 1)[1]
            m = re.match(r"(?P<slug>.+?)-(?:s(?P<season>\d+)e)?(?P<num>\d+)$", raw)
            if not m:
                return Response(url=url, status=404, headers={}, body=b"bad episode url")
            item = _find(m.group("slug"))
            if item is None:
                return Response(url=url, status=404, headers={}, body=b"not found")
            html = _episode_page(item, int(m.group("num")), int(m.group("season") or 0))
        elif path.startswith("/embed/"):
            kind = "mp4" if path.startswith("/embed/mp4") else "hls"
            html = _embed_page(kind, params.get("slug", "frieren"), int(params.get("ep", 1) or 1),
                               params.get("q", "1080p"))
        elif path.startswith("/static/"):
            return Response(url=url, status=200, headers={"content-type": "text/javascript"},
                            body=b"// player.js\n")

        if html is None:
            return Response(url=url, status=404, headers={}, body=b"demo: not found")

        return Response(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=html.encode("utf-8"),
            elapsed=0.0,
        )


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

RULE: dict[str, Any] = {
    "id": "demo",
    "name": "Demo Site (offline)",
    "base": BASE,
    "kinds": ["anime", "movie", "series"],
    "priority": 5,
    "enabled": True,
    "note": "Offline sample catalogue - no network needed.",
    "kind_map": {"movie": "movie", "special": "anime", "ova": "anime"},
    "search": {
        "url": "/search?q={query}&page={page}",
        "items": "div.anime-list article.card",
        "max_pages": 5,
        "next": "nav.pagination a.next @href",
        "fields": {
            "kind": "@data-kind",
            "title": "h3.title a ::text",
            "url": "h3.title a @href",
            "poster": "a.poster img @data-src",
            "year": "span.year ::text",
            "score": "span.score ::text",
            "status": "span.status ::text",
            "episodes": "span.eps-count ::text",
            "synopsis": "p.synopsis ::text",
        },
        "post": {"episodes": ["regex:(\\d+)"]},
    },
    "details": {
        "url": "/anime/{id}",
        "fields": {
            "kind": ".anime-detail @data-kind",
            "title": "h1.entry-title ::text",
            "poster": "div.poster img @src",
            "synopsis": "div.description ::text",
            "score": "span.score ::text",
            "year": "span.year ::text",
            "type": "span.type ::text",
            "status": "span.status ::text",
            "studios": "span.studio ::text",
            "genres": "div.genres a ::text",
            "total_episodes": "span.total-eps ::text",
        },
        "episodes": {
            "items": "ol.episodes li.episode",
            "max_pages": 5,
            "next": "div.eplister div.pagination a.next-page",
            "fields": {
                "number": "@data-number | span.ep-num ::text",
                "season": "@data-season",
                "title": "span.ep-title ::text",
                "url": "a @href",
                "duration": "span.duration ::text",
                "aired": "span.aired @data-date",
                "audio": "@data-audio",
            },
            "post": {},
        },
        "related": {
            "items": "div.related ul li",
            "fields": {"title": "a ::text", "url": "a @href", "type": "span.rel-type ::text"},
        },
    },
    "episode": {
        "servers": {
            "items": "div.servers div.server-item",
            "fields": {
                "label": "a.server-link ::text",
                "embed": "a.server-link @data-embed | a.server-link @href",
            },
        },
        "downloads": {
            "items": "div.downloads a.download-link",
            "fields": {
                "url": "@href",
                "label": "::text",
                "quality": "@data-quality",
                "size": "@data-size",
            },
        },
        "subtitles": {
            "items": "div.subtitles track",
            "fields": {"label": "@label", "url": "@src", "lang": "@srclang"},
        },
    },
}


class DemoProvider(RuleProvider):
    priority = 5

    def make_client(self) -> Client:  # type: ignore[override]
        return _LocalClient(user_agent=self.config.get("user_agent", "aninext-demo"))



def build() -> DemoProvider:
    rule = json.loads(json.dumps(RULE))  # deep copy so callers can't mutate the template
    rule["__path__"] = "<built-in:demo>"
    return DemoProvider(rule)
