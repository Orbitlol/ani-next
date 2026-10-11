# aninext

An anime / movie / series scraper and media kit that runs on **Termux** — and
on any other Linux — using **nothing but the Python standard library**.

Search a title, pick a stream, play it, keep a library, download episodes,
grab subtitles, or wrap the whole engine in your own script:

```console
$ aninext -p demo search frieren
  1.  Frieren: Beyond Journey's End (2023)  8ep  9.0  [demo]

  tip: use a number to pick, e.g. `aninext info 2`

$ aninext info 1              # full details, episodes, related titles
$ aninext play 1 -e 3         # resolve a stream and hand it to the player
$ aninext download 1 -E 1-4 -Q 720p
```

## Why there are no pip dependencies

Termux runs Android's Bionic libc, so the manylinux wheels on PyPI simply do
not load — anything with a C extension (`lxml`, `numpy`, `cryptography`) has to
be compiled locally, which regularly fails. `aninext` avoids the problem
instead of fighting it:

| normally you'd pip install | aninext uses instead                    |
| -------------------------- | --------------------------------------- |
| `requests` + `urllib3`     | `urllib.request`, cookies, gzip, retries |
| `beautifulsoup4`/`lxml`    | `aninext.htmldom`, a small CSS-lite DOM  |
| `yt-dlp` (as a library)    | optional `yt-dlp` **binary** if present  |
| `click`/`rich`             | `argparse` + ANSI helpers                |
| `sqlite`/`SQLAlchemy`      | `sqlite3` with FTS5                      |

`ffmpeg`, `aria2c`, `yt-dlp` and the `termux-api` commands are detected at
runtime and used when available, but nothing is required.

## Install (Termux)

```bash
pkg install python            # the only hard requirement
git clone https://github.com/Orbitlol/ani-next
cd ani-next
bash install.sh               # writes $PREFIX/bin/aninext

# optional: ffmpeg (HLS muxing), aria2 (faster downloads), yt-dlp, termux-api
bash install.sh --with-extras
termux-setup-storage          # run once, so downloads land in shared storage
```

Then:

```bash
aninext doctor                # first thing to run: checks python, sqlite, ffmpeg, storage
aninext search "spy x family"
aninext --provider demo play frieren -e 1     # works fully offline
```

Running from the checkout without installing also works: `./aninext-cli ...`
or `python3 -m aninext ...`.

## Commands

```
search      search for a title across providers       trending    what is popular right now
info        show details for a title                  episodes    list episodes
links       show streams, downloads and subtitles     play        resolve and play an episode
watch       continue a show (next unwatched episode)  download    download episodes
downloads   list or clean download records            library     manage your library
watchlist   plan what to watch                        history     watch history / continue watching
schedule    airing schedule (anime)                   season      anime of a season
providers   list providers and their status           health      ping providers and report what works
resolve     resolve an embed/player page to streams   sources     list/validate your own scraper rules
config      read or change settings                   cache       inspect or clear the http cache
subs        inspect/convert subtitle files            open        open a url/file with the system handler
doctor      diagnose the environment
```

Global flags work before **or** after the subcommand:

```
-p/--provider ID   -k/--kind KIND   --json   -q   -v   --no-colour   --home DIR
```

Anything a search prints can be referenced by number afterwards
(`aninext play 2`), or by `provider:id`, or by URL.

### Examples

```bash
aninext episodes frieren -n 10                   # newest first (--first flips it)
aninext search "blade runner" -k movie --json | jq '.[0].title'
aninext links frieren -e 1                      # streams, downloads and subtitles
aninext play frieren -e 1 --quality 1080p       # mpv -> vlc -> termux-open -> xdg-open
aninext watch frieren                           # next unwatched episode, then the one after
aninext download frieren -E 1-3,7 -Q 720p   # -e is one episode, -E takes a range
aninext config set subtitles en,es          # sidecar subs for downloads
aninext library add frieren --tag comfort
aninext history continue                        # what to watch next
aninext season 2023 fall                        # anime of a season
aninext resolve https://some.site/embed/xyz     # unpack players / packed JS
aninext subs ./episode.vtt --to srt --shift 2.5
```

## Providers

Providers are pluggable. Metadata providers describe titles; scrapers turn
site pages into streams. Priority decides who wins when results overlap.

| id         | what it does                                              | notes |
| ---------- | --------------------------------------------------------- | ----- |
| `anilist`  | anime metadata, schedules, seasons (GraphQL)              | on by default |
| `archive`  | Internet Archive movies, series and cartoons              | on by default, no key |
| `tmdb`     | movie & TV metadata                                       | needs `TMDB_API_KEY` |
| `consumet` | any Consumet-style JSON API (anime + movies)            | off until `CONSUMET_API` is set |
| `demo`     | a complete fake site served from memory                   | offline demo + tests |
| *your own* | JSON scraper rules (see below)                            | drop-in, no code |

```bash
aninext providers                       # what is loaded and enabled
aninext health                          # which providers actually answer
aninext config set providers.tmdb.enabled false     # dotted keys work too
export CONSUMET_API=http://127.0.0.1:3000          # enable the consumet provider
```

### Your own scraper rules

No Python needed — a JSON file in `~/.aninext/sources/` maps a site onto
aninext's model:

```json
{
  "id": "mysite",
  "base": "https://mysite.example",
  "kinds": ["anime"],
  "search": {
    "url": "/search?q={query}&page={page}",
    "items": "div.anime-list article.card",
    "next": "nav.pagination a.next @href",
    "fields": {
      "title": "h3.title a ::text",
      "url": "h3.title a @href",
      "poster": "a.poster img @data-src | a.poster img @src",
      "kind": "span.type ::text"
    }
  },
  "details": {
    "fields": { "synopsis": "div.description ::text" },
    "episodes": { "items": "ul.episodes li",
                  "fields": { "number": "span.num ::text", "url": "a @href" } }
  },
  "episode": {
    "servers": { "items": "div.server",
                 "fields": { "label": "::text", "embed": "@data-embed" } }
  }
}
```

If a rule has no `details.url` template (many sites have no predictable page
URL), `aninext` finds the page through the site's own search instead. That is
what the test in `tests/test_cli_download.py` does end to end.

Selectors support `a > b`, `a + b`, `.class`, `#id`, `[attr=value]`,
`:first-child`, `:nth-child(n)`, `:not()`, `:has()`, `:contains()`, plus
`@attr`, `::text`, `html` and `a | b` fallbacks. `aninext sources validate
file.json` checks your file; `aninext/sources.d/example.json` documents every
field. Rules you write are your responsibility — only scrape sites whose terms
allow it.

## Downloads

`aninext download` handles plain files and HLS (`.m3u8`) playlists:

* resumes from `.part` files (HTTP `Range`)
* parallel episode and fragment downloads
* names files `Show - S01E03 - Episode Title [1080p].mp4`
* sidecar subtitles (`episode.mp4.en.vtt`)
* Termux notification + wake lock while running
* skips anything already downloaded

Optional helpers make it better: `aria2c` for plain files, `ffmpeg` for HLS
muxing and remuxing, `yt-dlp` as a last resort for hosts it knows.

## Library, history and watchlist

Everything is one SQLite file (`~/.aninext/aninext.db`) with FTS5 search:

```bash
aninext library add frieren        aninext library search elf
aninext watchlist add frieren --status watching
aninext history                    aninext history continue
```

## Configuration

`~/.aninext/config.json` (or `$ANINEXT_HOME/config.json`):

```bash
aninext config                       # show everything
aninext config set quality 720p
aninext config set download_dir /sdcard/Anime
aninext config set rate_limit 1      # be polite to sites that ask for it
```

Useful keys: `provider`, `providers`, `quality`, `fallback_quality`,
`download_dir`, `workers`, `download_workers`, `use_aria2`, `use_ffmpeg`,
`yt_dlp_fallback`, `subtitles`, `download_subtitles`, `notifications`,
`wake_lock`, `open_player`, `auto_next`, `history`, `search_limit`, `timeout`,
`retries`, `rate_limit`, `colour`, `log_level`, `kind`.

Environment overrides: `ANINEXT_HOME`, `ANINEXT_PROVIDER`, `ANINEXT_QUALITY`,
`ANINEXT_DOWNLOAD_DIR`, `ANINEXT_WORKERS`, `ANINEXT_KIND`, `ANINEXT_LOG`,
`ANINEXT_PLAYER`, `TMDB_API_KEY`, `NO_COLOR`.

## Use it from Python

```python
from aninext import api

results = api.search("frieren", limit=5)
title = api.info(results[0])
streams = api.streams(title, episode=1)
print(streams[0].url)

api.download(title, episodes="1-3", quality="720p")
```

`api` returns plain dataclasses (`Title`, `Episode`, `Stream`, `DownloadLink`,
`Subtitle`) — no JSON wrangling required.

## Tests

```bash
./tests/run.sh              # 213 tests, no network and no pip packages
./tests/run.sh -v
./tests/run.sh test_db      # a single module
```

The suite runs the real rule engine against the offline `demo` provider and
local HTTP servers, so it exercises search, pagination, episode listing,
packed-JS players, base64 embeds, downloads, resume, subtitles, the library
database and the CLI without touching the internet.

## Notes

* `aninext` only downloads what you point it at. Respect copyright and each
  site's terms; keep the rate limit on for sites that ask for it.
* DRM-protected streams are detected and skipped — never circumvented.
* `aninext doctor` explains what is missing and how to install it.
* The `demo` provider is fake data served from memory: perfect for trying
  things out and for the test suite, but its stream URLs do not exist.

## License

MIT.
