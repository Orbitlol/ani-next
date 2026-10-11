"""aninext command line interface."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import __version__, aggregate, log, termux
from .config import download_dir, get_config
from .models import Episode, SearchResult, Stream, Title, quality_rank
from .net import HttpError
from .providers import ProviderError, all_providers, available, get, ids as provider_ids, load_builtin, require
from .providers.base import SearchQuery

PROG = "aninext"
KINDS = ("anime", "movie", "series", "tv", "cartoon", "donghua", "all")


# --------------------------------------------------------------------------
# small output helpers
# --------------------------------------------------------------------------

def _out(text: str = "") -> None:
    log.plain(text)


def _json(data: Any) -> None:
    log.plain(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _table(rows: Sequence[Sequence[str]], headers: Sequence[str] = (), *, indent: str = "  ") -> str:
    rows = [list(map(str, r)) for r in rows]
    cols = len(headers) or (len(rows[0]) if rows else 0)
    widths = [0] * cols
    for r in ([list(headers)] if headers else []) + rows:
        for i in range(min(cols, len(r))):
            widths[i] = max(widths[i], len(str(r[i])))
    lines = []
    if headers:
        lines.append(indent + "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
        lines.append(indent + "  ".join("-" * widths[i] for i in range(cols)))
    for r in rows:
        lines.append(indent + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r[:cols])).rstrip())
    return "\n".join(lines)


def _bar(percent: float, width: int = 28) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(width * percent / 100)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {percent:5.1f}%"


def _progress_printer(prefix: str = "") -> Any:
    state = {"last": 0.0}

    def show(fields: dict) -> None:
        now = time.time()
        if now - state["last"] < 0.5 and fields.get("percent", 0) < 100:
            return
        state["last"] = now
        percent = fields.get("percent")
        if percent is None:
            downloaded, total = fields.get("downloaded", 0), fields.get("total", 0)
            percent = (downloaded / total * 100) if total else 0
        backend = fields.get("backend", "")
        item = fields.get("item")
        label = f"{item.label} " if item is not None else ""
        sys.stderr.write(f"\r  {prefix}{label}{_bar(float(percent))} {backend:<8}")
        sys.stderr.flush()
        if percent >= 100:
            sys.stderr.write("\n")

    return show


def _head(text: str) -> None:
    _out(log.colour(text, "bold", "cyan"))


def _kv(pairs: Iterable[tuple[str, Any]], *, indent: str = "  ") -> None:
    pairs = [(k, v) for k, v in pairs if v not in (None, "", [], {})]
    if not pairs:
        return
    width = max(len(k) for k, _ in pairs)
    for key, value in pairs:
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        _out(f"{indent}{log.colour(key.ljust(width), 'grey')}  {value}")


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_results(results: Sequence[SearchResult], *, numbered: bool = True, show_provider: bool = True) -> None:
    if not results:
        _out("no results")
        return
    lines = []
    for index, r in enumerate(results, start=1):
        label = r.title
        if r.year:
            label += f" ({r.year})"
        bits = [f"{index:>3}." if numbered else "   ", label]
        if r.episodes:
            bits.append(f"{r.episodes}ep")
        if r.score:
            bits.append(r.score)
        if show_provider:
            bits.append(log.colour(f"[{r.provider}]", "grey"))
        lines.append("  ".join(bits))
    _out("\n".join(lines))
    _out()
    _out(log.colour("  tip: use a number to pick, e.g. `aninext info 2`", "grey"))


def render_title(title: Title, *, episodes: bool = True, limit: int = 60) -> None:
    _head(title.title)
    _kv(
        [
            ("kind", title.kind),
            ("year", title.year),
            ("type", title.type_),
            ("status", title.status),
            ("score", title.score),
            ("country", title.country),
            ("duration", title.duration),
            ("episodes", title.total_episodes or len(title.episodes)),
            ("genres", title.genres),
            ("studios", title.studios),
            ("provider", title.provider),
            ("url", title.url),
        ]
    )
    if title.alt_titles:
        _out(f"  {'aka'.ljust(6)}  {', '.join(title.alt_titles[:4])}")
    if title.synopsis:
        _out()
        _out(_wrap(title.synopsis, 78, indent="  "))
    if episodes and title.episodes:
        _out()
        _head("  Episodes")
        rows = []
        for ep in title.sorted_episodes()[:limit]:
            label = f"{ep.number:g}"
            name = ep.title or ""
            meta = " ".join(x for x in (ep.duration, ep.aired) if x)
            rows.append([label.rjust(4), name[:56], meta])
        _out(_table(rows))
        if len(title.episodes) > limit:
            _out(f"  ... {len(title.episodes) - limit} more (use --limit)")
    if title.related:
        _out()
        _head("  Related")
        for item in title.related[:8]:
            _out(f"    {item.get('title','?'):<44} {log.colour(item.get('type',''), 'grey')}")


def _wrap(text: str, width: int, indent: str = "") -> str:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return "\n".join(indent + line for line in lines)


# --------------------------------------------------------------------------
# reference resolution helpers
# --------------------------------------------------------------------------

def _resolve_reference(args, providers: Sequence[str] | None, kind: str | None,
                       *, allow_number: bool = True) -> tuple[Title, Any]:
    """Accept 'frieren', 'demo:frieren', a URL, or the number of a previous search."""
    reference = str(args.reference or "").strip()
    if allow_number and reference.isdigit():
        cached = _load_last_results()
        index = int(reference)
        if cached and 1 <= index <= len(cached):
            item = cached[index - 1]
            reference = f"{item['provider']}:{item['url'] or item['id']}"
    return aggregate.resolve_title(reference, providers=providers, kind=kind)


def _last_results_path() -> Path:
    from .paths import home

    return home() / "last-search.json"


def _save_last_results(results: Sequence[SearchResult]) -> None:
    try:
        path = _last_results_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([r.to_dict() for r in results], ensure_ascii=False),
            "utf-8",
        )
    except OSError as exc:
        log.debug("could not cache last results: %s", exc)


def _load_last_results() -> list[dict]:
    try:
        raw = _last_results_path().read_text("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _pick_stream(streams: Sequence[Stream], quality: str | None, *, prefer: str = "") -> Stream | None:
    if not streams:
        return None
    cfg = get_config()
    want = (quality or cfg.get("quality") or "").lower()
    aliases = {"2160p": "4k", "4k": "2160p", "hd": "1080p", "sd": "480p"}
    ordered = sorted(streams, key=lambda s: (s.playable, s.rank), reverse=True)
    if want and want not in ("best", "auto"):
        wanted = {want, aliases.get(want, want)}
        for stream in ordered:
            if stream.quality.lower() in wanted:
                return stream
        for stream in ordered:
            if any(w in stream.quality.lower() for w in wanted):
                return stream
    if prefer:
        for stream in ordered:
            if prefer in (stream.server or "").lower() or prefer in (stream.kind or ""):
                return stream
    return ordered[0]


def _parse_episode_spec(spec: str, title: Title) -> list[float]:
    """'1-5,8,12' -> [1,2,3,4,5,8,12]."""
    out: list[float] = []
    for chunk in str(spec).replace(" ", "").split(","):
        if not chunk:
            continue
        if chunk in ("all", "*"):
            return [e.number for e in title.sorted_episodes()]
        m = re.match(r"^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$", chunk)
        if m:
            start, end = float(m.group(1)), float(m.group(2))
            step = 1.0 if end >= start else -1.0
            value = start
            while (step > 0 and value <= end + 1e-9) or (step < 0 and value >= end - 1e-9):
                out.append(value)
                value += step
            continue
        try:
            out.append(float(chunk))
        except ValueError:
            continue
    return out


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_search(args, cfg) -> int:
    providers = args.provider or None
    kind = None if args.kind in (None, "all") else args.kind
    results = aggregate.search(
        args.query,
        providers=providers,
        kind=kind,
        year=args.year,
        genre=args.genre,
        limit=args.limit,
        sort=args.sort,
    )
    _save_last_results(results)
    code = 0 if results else 1
    if args.json:
        _json([r.to_dict() for r in results])
        return code
    if not results:
        log.warn("no results for %r", args.query)
    render_results(results)
    return code


def cmd_trending(args, cfg) -> int:
    kind = None if args.kind in (None, "all") else args.kind
    results = aggregate.trending(kind=kind, limit=args.limit, providers=args.provider or None)
    _save_last_results(results)
    if args.json:
        _json([r.to_dict() for r in results])
        return 0
    _head(f"Trending{' ' + kind if kind else ''}")
    render_results(results)
    return 0


def cmd_info(args, cfg) -> int:
    title, provider = _resolve_reference(args, args.provider or None, args.kind)
    if not args.no_episodes:
        aggregate.load_episodes(title, provider)
    if args.json:
        _json(title.to_dict())
        return 0
    render_title(title, episodes=not args.no_episodes, limit=args.limit)
    return 0


def cmd_episodes(args, cfg) -> int:
    title, provider = _resolve_reference(args, args.provider or None, args.kind)
    episodes = aggregate.load_episodes(title, provider)
    if args.json:
        _json([e.to_dict() for e in episodes])
        return 0
    _head(f"{title.title} - {len(episodes)} episodes")
    ordered = title.sorted_episodes()
    if not getattr(args, "first", False):
        ordered = list(reversed(ordered))
    if getattr(args, "limit", 0):
        ordered = ordered[: args.limit]
    rows = []
    for ep in ordered:
        audio = ",".join(ep.audio)
        rows.append([f"{ep.number:g}".rjust(5), ep.title[:52], ep.duration, ep.aired, audio])
    _out(_table(rows, ["ep", "title", "len", "aired", "audio"]))
    return 0


def _load_streams_for(args, cfg) -> tuple[Title, Any, Episode, list[Stream]]:
    title, provider = _resolve_reference(args, args.provider or None, args.kind)
    aggregate.load_episodes(title, provider)
    episode = aggregate.pick_episode(title, args.episode, latest=getattr(args, "latest", False))
    streams = aggregate.get_streams(title, episode, provider, allow_fallback=not getattr(args, "no_fallback", False))
    if not streams:
        raise ProviderError(f"no playable streams for {title.title} {episode.label}")
    return title, provider, episode, streams


def cmd_links(args, cfg) -> int:
    title, provider, episode, streams = _load_streams_for(args, cfg)
    downloads = aggregate.get_downloads(title, episode, provider)
    subs = aggregate.get_subtitles(title, episode, provider)
    if args.json:
        _json(
            {
                "title": title.to_dict(with_episodes=False),
                "episode": episode.to_dict(),
                "streams": [s.to_dict() for s in streams],
                "downloads": [d.to_dict() for d in downloads],
                "subtitles": [s.to_dict() for s in subs],
            }
        )
        return 0
    _head(f"{title.title} - {episode.label}")
    _out()
    _head("  Streams")
    rows = []
    for s in streams:
        rows.append([s.quality or "-", s.kind, (s.server or "")[:26], "yes" if s.playable else "no", s.url[:76]])
    _out(_table(rows, ["quality", "kind", "server", "direct", "url"]))
    if downloads:
        _out()
        _head("  Downloads")
        rows = []
        for d in downloads:
            size = f"{d.size / (1024*1024):.0f}MB" if d.size else "-"
            rows.append([d.quality or "-", size, (d.server or "")[:20], d.url[:80]])
        _out(_table(rows, ["quality", "size", "server", "url"]))
    if subs:
        _out()
        _head("  Subtitles")
        for s in subs:
            _out(f"    {s.label:<18} {s.lang:<6} {s.url[:80]}")
    return 0


def cmd_play(args, cfg) -> int:
    from . import player as player_mod

    title, provider, episode, streams = _load_streams_for(args, cfg)
    stream = _pick_stream(streams, args.quality, prefer=args.server or "")
    if stream is None:
        raise ProviderError("no stream selected")

    if args.print_url:
        _out(stream.url)
        return 0

    subs = aggregate.get_subtitles(title, episode, provider)
    if subs:
        log.debug("%d subtitle tracks available", len(subs))

    played = player_mod.play_episode(
        title,
        episode,
        stream,
        player=args.player or cfg.get("open_player", "auto"),
        playlist=args.playlist,
        open_with=args.open,
        record=cfg.get_bool("history", True) and not args.no_history,
    )
    if played and cfg.get_bool("auto_next", False) and not args.no_next:
        nxt = _next_episode(title, episode)
        if nxt is not None:
            log.info("up next: %s", nxt.label)
    return 0 if played else 1


def _next_episode(title: Title, episode: Episode) -> Episode | None:
    episodes = title.sorted_episodes()
    for index, ep in enumerate(episodes):
        if abs(ep.number - episode.number) < 1e-6:
            return episodes[index + 1] if index + 1 < len(episodes) else None
    return None


def cmd_watch(args, cfg) -> int:
    """Continue a show: pick the next unwatched episode and play it."""
    title, provider = _resolve_reference(args, args.provider or None, args.kind)
    aggregate.load_episodes(title, provider)
    episode = aggregate.pick_episode(title, args.episode, latest=args.latest)
    args.episode = episode.number
    streams = aggregate.get_streams(title, episode, provider)
    if not streams:
        raise ProviderError(f"no streams for {title.title} {episode.label}")
    stream = _pick_stream(streams, args.quality)

    from . import player as player_mod

    if args.print_url:
        _out(stream.url)
        return 0
    played = player_mod.play_episode(
        title, episode, stream,
        player=args.player or cfg.get("open_player", "auto"),
        playlist=args.playlist, open_with=args.open,
        record=cfg.get_bool("history", True),
    )
    return 0 if played else 1


def cmd_download(args, cfg) -> int:
    from . import download as dl

    title, provider = _resolve_reference(args, args.provider or None, args.kind)
    aggregate.load_episodes(title, provider)

    if args.episodes:
        numbers = _parse_episode_spec(args.episodes, title)
    elif args.all:
        numbers = [e.number for e in title.sorted_episodes()]
    elif args.latest:
        numbers = [title.sorted_episodes()[-1].number]
    else:
        numbers = [aggregate.pick_episode(title, args.episode).number]

    wanted = set(numbers)
    episodes = [e for e in title.sorted_episodes() if e.number in wanted]
    if not episodes:
        raise ProviderError("no episodes matched")

    if args.dir:
        cfg["download_dir"] = args.dir
    directory = download_dir()
    log.info("downloading %d episode(s) of %s to %s", len(episodes), title.title, directory)

    items = []
    skipped = 0
    for episode in episodes:
        existing = dl.find_existing(title, episode, directory=directory)
        if existing and not args.force:
            log.ok("already downloaded: %s", existing.name)
            skipped += 1
            continue
        streams = aggregate.get_streams(title, episode, provider)
        if not streams:
            log.warn("no streams for %s", episode.label)
            continue
        stream = _pick_stream(streams, args.quality, prefer=args.server or "")
        if stream is None:
            continue
        subs = []
        if cfg.get_bool("download_subtitles", True):
            subs = aggregate.get_subtitles(title, episode, provider)
        items.append(dl.prepare_item(title, episode, stream, quality=args.quality or "",
                                     directory=directory, subtitles=subs))

    if not items:
        if skipped:
            log.ok("everything is already downloaded (%d episode(s))", skipped)
            return 0
        log.warn("nothing to download")
        return 1

    results = dl.run_queue(items, workers=args.jobs or cfg.get_int("download_workers", 2),
                           progress=_progress_printer("download "))
    ok = sum(1 for r in results if r.ok)
    total = sum(r.size for r in results if r.ok)
    for item, result in zip(items, results):
        if result.ok and result.path:
            log.ok("%s -> %s (%s)", item.label, result.path.name, dl.human_size(result.size))
        else:
            log.error("%s: %s", item.label, result.error or "failed")
    log.info("%d/%d complete, %s total", ok, len(items), dl.human_size(total))
    return 0 if ok else 1


def cmd_downloads(args, cfg) -> int:
    from .db import get_db

    db = get_db()
    if args.action == "clear":
        rows = db.downloads_list()
        for row in rows:
            if row["status"] in ("failed", "paused"):
                db.download_delete(row["id"])
        log.ok("cleared failed/paused entries")
        return 0
    rows = db.downloads_list(status=args.status, limit=args.limit)
    if args.json:
        _json(rows)
        return 0
    if not rows:
        _out("no downloads recorded")
        return 0
    table = []
    for row in rows:
        size = f"{row['size'] / (1024*1024):.0f}MB" if row.get("size") else "-"
        table.append([
            str(row["id"]),
            str(row["title"])[:32],
            f"{row['episode']:g}",
            row.get("quality") or "-",
            size,
            row.get("status") or "",
            (Path(row.get("path") or "").name or "")[:30],
        ])
    _out(_table(table, ["id", "title", "ep", "quality", "size", "status", "file"]))
    return 0


def cmd_library(args, cfg) -> int:
    from .db import get_db

    db = get_db()
    if args.action == "add":
        title, provider = _resolve_reference(args, args.provider or None, args.kind)
        aggregate.load_episodes(title, provider)
        db.library_add(title, auto_download=args.auto_download, tags=args.tag or [])
        log.ok("added %s to the library (%d episodes)", title.title, len(title.episodes))
        return 0
    if args.action == "remove":
        title, provider = _resolve_reference(args, args.provider or None, args.kind)
        db.library_remove(title.key)
        log.ok("removed %s", title.title)
        return 0
    if args.action == "search":
        rows = db.search_library(args.reference or args.query or "")
    else:
        rows = db.library_list(limit=args.limit)
    if args.json:
        _json(rows)
        return 0
    if not rows:
        _out("library is empty - add something with `aninext library add <title>`")
        return 0
    table = []
    for row in rows:
        table.append([
            str(row["title"])[:40],
            row.get("kind") or "",
            row.get("year") or "",
            f"{row.get('watched_eps') or 0}/{row.get('total_eps') or 0}",
            row.get("status") or "",
        ])
    _out(_table(table, ["title", "kind", "year", "watched", "status"]))
    return 0


def cmd_watchlist(args, cfg) -> int:
    from .db import get_db

    db = get_db()
    if args.action in ("add", "remove"):
        title, provider = _resolve_reference(args, args.provider or None, args.kind)
        if args.action == "add":
            status = args.status or "plan"
            db.watchlist_add(title.key, title.title, status=status, provider=title.provider,
                             poster=title.poster, url=title.url)
            log.ok("watchlist: %s -> %s", title.title, status)
        else:
            db.watchlist_remove(title.key)
            log.ok("watchlist: removed %s", title.title)
        return 0
    rows = db.watchlist_list(None if args.status in (None, "any") else args.status)
    if args.json:
        _json(rows)
        return 0
    if not rows:
        _out("watchlist is empty")
        return 0
    _out(_table([[str(r["title"])[:42], r["status"], str(r.get("provider") or "")] for r in rows],
                ["title", "status", "provider"]))
    return 0


def cmd_history(args, cfg) -> int:
    from .db import get_db

    db = get_db()
    if getattr(args, "action", "list") == "continue":
        args.continue_ = True
    if getattr(args, "action", "list") == "clear":
        args.clear = True
    if args.clear:
        count = db.history_clear(None)
        log.ok("cleared %d history entries", count)
        return 0
    if args.continue_:
        rows = db.continue_watching(limit=args.limit)
        if args.json:
            _json(rows)
            return 0
        if not rows:
            _out("nothing in progress")
            return 0
        _out(_table([[str(r["title"])[:38], f"{r['episode']:g}", f"{r.get('next_episode') or '-'}",
                      time.strftime("%d %b %H:%M", time.localtime(r["watched_at"] or 0))]
                     for r in rows], ["title", "last ep", "next ep", "watched"]))
        return 0
    rows = db.history_list(limit=args.limit, unique_titles=args.unique)
    if args.json:
        _json(rows)
        return 0
    if not rows:
        _out("no history yet")
        return 0
    _out(_table([[str(r["title"])[:38], f"{r['episode']:g}", str(r.get("episode_title") or "")[:30],
                  time.strftime("%d %b %H:%M", time.localtime(r["watched_at"] or 0))]
                 for r in rows], ["title", "ep", "episode", "when"]))
    return 0


def cmd_providers(args, cfg) -> int:
    load_builtin()
    rows = []
    for provider in all_providers():
        describe = getattr(provider, "describe", None)
        info = describe() if callable(describe) else {
            "id": provider.id, "name": provider.name, "kind": "scraper",
            "kinds": list(provider.kinds), "base": getattr(provider, "base", ""),
            "enabled": provider.is_enabled(), "note": provider.note,
        }
        info["enabled"] = provider.is_enabled()
        rows.append(info)
    if args.json:
        _json(rows)
        return 0
    table = []
    for row in rows:
        table.append([
            row["id"],
            str(row.get("name") or "")[:24],
            "yes" if row.get("enabled") else "no",
            ",".join(row.get("kinds") or [])[:26],
            str(row.get("base") or "")[:34],
            str(row.get("note") or "")[:38],
        ])
    _out(_table(table, ["id", "name", "on", "kinds", "base", "note"]))
    return 0


def cmd_schedule(args, cfg) -> int:
    rows = aggregate.schedule(days=args.days, limit=args.limit, providers=args.provider or None)
    if args.json:
        _json(rows)
        return 0
    if not rows:
        _out("no schedule information available")
        return 0
    _out(_table([[str(r.get("time") or ""), str(r.get("title") or "")[:46],
                  str(r.get("episode") if r.get("episode") is not None else "")] for r in rows],
                ["when", "title", "ep"]))
    return 0


def cmd_season(args, cfg) -> int:
    results = aggregate.season(year=args.year, season_name=args.season_name, limit=args.limit)
    _save_last_results(results)
    if args.json:
        _json([r.to_dict() for r in results])
        return 0
    _head(f"{args.season_name.title()} {args.year}")
    render_results(results)
    return 0


def cmd_resolve(args, cfg) -> int:
    from .resolve import Resolver, upgrade_embeds

    patterns: list[str] = []
    if args.pattern:
        patterns.extend(args.pattern)
    resolver = Resolver(patterns=patterns, depth=args.depth)
    streams = resolver.resolve(args.url, referer=args.referer or "")
    if args.json:
        _json([s.to_dict() for s in streams])
        return 0
    if not streams:
        _out("no direct streams found (the player may be encrypted or JS-driven)")
        return 1
    _out(_table([[s.quality or "-", s.kind, (s.server or "")[:24], s.url[:88]] for s in streams],
                ["quality", "kind", "server", "url"]))
    return 0


def cmd_sources(args, cfg) -> int:
    from .providers import rules

    if args.action == "path":
        _out(str(rules.sources_dir()))
        return 0
    if args.action == "validate":
        path = Path(args.file).expanduser()
        if not path.is_file():
            log.error("no such file: %s", path)
            return 1
        try:
            data = json.loads(path.read_text("utf-8"))
        except ValueError as exc:
            log.error("invalid JSON: %s", exc)
            return 1
        entries = data.get("providers") if isinstance(data, dict) and "providers" in data else [data]
        problems: list[str] = []
        for entry in entries:
            problems.extend(rules.validate_rule(entry))
        if problems:
            _out("problems found:")
            for problem in problems:
                _out(f"  - {problem}")
            return 1
        log.ok("%s looks valid (%d provider definition(s))", path.name, len(entries))
        return 0
    providers = rules.load_all()
    if args.json:
        _json([p.describe() for p in providers])
        return 0
    if not providers:
        _out("no rule files found")
    else:
        _out(_table([[p.id, p.name[:28], "yes" if p.is_enabled() else "no", p.source_path or ""]
                     for p in providers], ["id", "name", "on", "file"]))
    _out()
    _out(f"  drop your own rule files in: {rules.sources_dir()}")
    _out("  example format: aninext/sources.d/example.json")
    return 0


def cmd_config(args, cfg) -> int:
    if args.action == "path":
        _out(str(cfg.path))
        return 0
    if args.action == "reset":
        cfg.reset()
        cfg.save()
        log.ok("config reset to defaults")
        return 0
    if args.action == "set":
        if not args.key:
            log.error("usage: aninext config set <key> <value>")
            return 2
        cfg.set(args.key, args.value)
        cfg.save()
        log.ok("%s = %r", args.key, cfg.get(args.key))
        return 0
    if args.action == "get":
        _out(str(cfg.get(args.key, "")))
        return 0
    if args.json:
        _json(dict(cfg))
        return 0
    _out(cfg.describe())
    return 0


def cmd_cache(args, cfg) -> int:
    from .net import DiskCache

    cache = DiskCache()
    if args.action == "clear":
        count = cache.clear()
        log.ok("cleared %d cached responses", count)
        return 0
    _out(f"  path      {cache.path}")
    _out(f"  entries   {len(list(cache.path.rglob('*.json')))}")
    _out(f"  size      {cache.size() / 1024:.0f} KB")
    return 0


def cmd_subs(args, cfg) -> int:
    from . import subtitles

    path = Path(args.file).expanduser()
    if not path.is_file():
        log.error("no such file: %s", path)
        return 1
    raw = path.read_text("utf-8", errors="replace")
    if args.stats:
        info = subtitles.stats(raw)
        if args.json:
            _json(info)
        else:
            _kv(list(info.items()))
        return 0
    if args.to:
        out = subtitles.convert_file(path, to=args.to, shift=args.shift)
        log.ok("wrote %s", out)
        return 0
    cues = subtitles.parse(raw)
    if args.json:
        _json([{"start": c.start, "end": c.end, "text": c.text} for c in cues])
        return 0
    _out(f"{path.name}: {len(cues)} cues, format {subtitles.detect_format(raw)}")
    for cue in cues[: args.limit]:
        _out(f"  {subtitles.format_timestamp(cue.start)} -> {cue.text[:70]}")
    return 0


def cmd_doctor(args, cfg) -> int:
    from .paths import is_termux, home, tool_report

    _head("aninext doctor")
    _kv([
        ("version", __version__),
        ("python", sys.version.split()[0]),
        ("termux", "yes" if is_termux() else "no"),
        ("home", home()),
        ("config", cfg.path),
        ("downloads", download_dir()),
    ])
    _out()
    _head("  tools")
    tools = tool_report()
    for name, path in tools.items():
        mark = log.colour("ok ", "green") if path else log.colour("-- ", "yellow")
        _out(f"    {mark} {name:<20} {path or 'not installed'}")
    _out()
    _head("  sqlite")
    import sqlite3

    con = sqlite3.connect(":memory:")
    fts = bool(con.execute("select 1 from pragma_compile_options where compile_options like '%FTS5%'").fetchall())
    _kv([("sqlite", sqlite3.sqlite_version), ("fts5", "yes" if fts else "no")])
    _out()
    _head("  storage")
    for label, path in (("home", home()), ("downloads", Path(download_dir()))):
        try:
            probe = path
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            writable = os.access(probe, os.W_OK)
            _kv([(label, f"{path} ({'ok' if writable else 'read-only'})")])
        except OSError as exc:
            _kv([(label, f"{path} (error: {exc})")])
    if not tools.get("ffmpeg"):
        _out()
        _out(log.colour("  hint: pkg install ffmpeg aria2 yt-dlp termux-api", "yellow"))
        _out(log.colour("  (ffmpeg enables HLS muxing, aria2c speeds up downloads)", "grey"))
    return 0


def cmd_open(args, cfg) -> int:
    if termux.open_url(args.url) or termux.open_path(args.url):
        log.ok("opened %s", args.url)
        return 0
    log.error("could not open %s (no termux-open/xdg-open)", args.url)
    _out(args.url)
    return 1


def cmd_health(args, cfg) -> int:
    """Ping every enabled provider and report what works right now."""
    load_builtin()
    rows = []
    for provider in all_providers():
        if args.only and provider.id not in args.only:
            continue
        if not provider.is_enabled() and not args.all:
            rows.append([provider.id, "disabled", "", ""])
            continue
        started = time.time()
        status, detail = "ok", ""
        try:
            results = provider.search(SearchQuery(text=args.query, limit=2, kind=None))
            detail = f"{len(results)} result(s)" if results else "no results"
            if not results:
                status = "empty"
        except Exception as exc:  # noqa: BLE001
            status = "fail"
            detail = str(exc)[:60]
        rows.append([provider.id, status, f"{time.time() - started:.2f}s", detail])
    if args.json:
        _json([{"provider": r[0], "status": r[1], "time": r[2], "detail": r[3]} for r in rows])
        return 0
    _out(_table(rows, ["provider", "status", "time", "detail"]))
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="anime / movie / series scraper & media kit for Termux (no pip dependencies)",
        epilog="run `aninext doctor` if something does not work, or `aninext <command> -h`",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument("-p", "--provider", action="append", help="provider id (repeatable)")
    parser.add_argument("-k", "--kind", choices=KINDS, help="media kind")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--no-colour", "--no-color", dest="no_colour", action="store_true")
    parser.add_argument("--home", help="use an alternative ANINEXT_HOME directory")

    # the same flags again on every subcommand so both orders work
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--provider", action="append", default=argparse.SUPPRESS)
    common.add_argument("-k", "--kind", choices=KINDS, default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--no-colour", "--no-color", dest="no_colour", action="store_true",
                        default=argparse.SUPPRESS)
    common.add_argument("--home", default=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    _add = sub.add_parser

    def add_parser(*a, **kw):
        kw.setdefault("parents", [common])
        return _add(*a, **kw)

    sub.add_parser = add_parser  # type: ignore[assignment]

    p = sub.add_parser("search", help="search for a title across providers")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("-n", "--limit", type=int, default=0)
    p.add_argument("-y", "--year")
    p.add_argument("-g", "--genre")
    p.add_argument("--sort", default=None)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("trending", help="what is popular right now")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.set_defaults(func=cmd_trending)

    p = sub.add_parser("info", help="show details for a title")
    p.add_argument("reference")
    p.add_argument("-n", "--limit", type=int, default=60)
    p.add_argument("--no-episodes", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("episodes", help="list episodes")
    p.add_argument("reference")
    p.add_argument("-n", "--limit", type=int, default=0, help="show only the first N (0 = all)")
    p.add_argument("--first", action="store_true", help="oldest first")
    p.set_defaults(func=cmd_episodes)

    p = sub.add_parser("links", help="show streams, downloads and subtitles")
    p.add_argument("reference")
    p.add_argument("-e", "--episode", type=float)
    p.add_argument("--latest", action="store_true")
    p.add_argument("--no-fallback", action="store_true")
    p.set_defaults(func=cmd_links)

    p = sub.add_parser("play", help="resolve and play an episode")
    p.add_argument("reference")
    p.add_argument("-e", "--episode", type=float)
    p.add_argument("--latest", action="store_true")
    p.add_argument("-Q", "--quality")
    p.add_argument("--server")
    p.add_argument("--player", help="auto | mpv | vlc | intent")
    p.add_argument("--playlist", action="store_true", help="write a .m3u8 and open that")
    p.add_argument("--open", action="store_true", help="hand the url to Android instead of mpv")
    p.add_argument("--print-url", action="store_true", help="print the stream url and stop")
    p.add_argument("--no-history", action="store_true")
    p.add_argument("--no-next", action="store_true")
    p.add_argument("--no-fallback", action="store_true")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("watch", help="continue a show (next unwatched episode)")
    p.add_argument("reference")
    p.add_argument("-e", "--episode", type=float)
    p.add_argument("--latest", action="store_true")
    p.add_argument("-Q", "--quality")
    p.add_argument("--player")
    p.add_argument("--playlist", action="store_true")
    p.add_argument("--open", action="store_true")
    p.add_argument("--print-url", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("download", help="download episodes")
    p.add_argument("reference")
    p.add_argument("-e", "--episode", type=float)
    p.add_argument("-E", "--episodes", help="1-5,8 / all")
    p.add_argument("--all", action="store_true")
    p.add_argument("--latest", action="store_true")
    p.add_argument("-Q", "--quality")
    p.add_argument("--server")
    p.add_argument("-d", "--dir", help="download directory")
    p.add_argument("-j", "--jobs", type=int, help="parallel downloads")
    p.add_argument("-f", "--force", action="store_true", help="re-download existing files")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("downloads", help="list or clean download records")
    p.add_argument("action", nargs="?", default="list", choices=["list", "clear"])
    p.add_argument("--status")
    p.add_argument("-n", "--limit", type=int, default=40)
    p.set_defaults(func=cmd_downloads)

    p = sub.add_parser("library", help="manage your library")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove", "search"])
    p.add_argument("reference", nargs="?")
    p.add_argument("-n", "--limit", type=int, default=60)
    p.add_argument("--auto-download", action="store_true")
    p.add_argument("--tag", action="append")
    p.set_defaults(func=cmd_library, query="")

    p = sub.add_parser("watchlist", help="plan what to watch")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove"])
    p.add_argument("reference", nargs="?")
    p.add_argument("--status", default=None,
                   choices=["watching", "plan", "completed", "dropped", "any"])
    p.set_defaults(func=cmd_watchlist)

    p = sub.add_parser("history", help="watch history / continue watching")
    p.add_argument("action", nargs="?", default="list", choices=["list", "continue", "clear"])
    p.add_argument("-n", "--limit", type=int, default=30)
    p.add_argument("-c", "--continue", dest="continue_", action="store_true")
    p.add_argument("--unique", action="store_true")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("providers", help="list providers and their status")
    p.set_defaults(func=cmd_providers)

    p = sub.add_parser("health", help="ping providers and report what works")
    p.add_argument("--query", default="a")
    p.add_argument("--all", action="store_true", help="include disabled providers")
    p.add_argument("--only", action="append")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("schedule", help="airing schedule (anime)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("-n", "--limit", type=int, default=40)
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("season", help="anime of a season, e.g. aninext season 2023 fall")
    p.add_argument("year", type=int)
    p.add_argument("season_name", choices=["winter", "spring", "summer", "fall"])
    p.add_argument("-n", "--limit", type=int, default=24)
    p.set_defaults(func=cmd_season)

    p = sub.add_parser("resolve", help="resolve an embed/player page to direct streams")
    p.add_argument("url")
    p.add_argument("--referer")
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--pattern", action="append", help="extra regex to look for")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("sources", help="list/validate your own scraper rules")
    p.add_argument("action", nargs="?", default="list", choices=["list", "validate", "path"])
    p.add_argument("file", nargs="?")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("config", help="read or change settings")
    p.add_argument("action", nargs="?", default="show", choices=["show", "set", "get", "reset", "path"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("cache", help="inspect or clear the http cache")
    p.add_argument("action", nargs="?", default="info", choices=["info", "clear"])
    p.set_defaults(func=cmd_cache)

    p = sub.add_parser("subs", help="inspect/convert subtitle files")
    p.add_argument("file")
    p.add_argument("--to", choices=["srt", "vtt"])
    p.add_argument("--shift", type=float, default=0.0, help="seconds to shift (can be negative)")
    p.add_argument("--stats", action="store_true")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.set_defaults(func=cmd_subs)

    p = sub.add_parser("open", help="open a url/file with the system handler")
    p.add_argument("url")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("doctor", help="diagnose the environment (do this first on Termux)")
    p.set_defaults(func=cmd_doctor)

    return parser


def apply_globals(args) -> None:
    if getattr(args, "home", None):
        os.environ["ANINEXT_HOME"] = os.path.expanduser(args.home)
    from . import termux as _termux

    if getattr(args, "verbose", False):
        log.set_level("debug")
    elif getattr(args, "quiet", False):
        log.set_level("error")
    if getattr(args, "no_colour", False):
        os.environ["NO_COLOR"] = "1"
    termux_wake = get_config().get_bool("wake_lock", True)
    if not termux_wake:
        os.environ.setdefault("ANINEXT_NO_WAKE", "1")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    apply_globals(args)
    from .paths import ensure_dirs

    ensure_dirs()
    cfg = get_config()
    if not args.json and args.command != "doctor":
        log.set_level("info")
    load_builtin()

    if getattr(args, "limit", None) == 0:
        args.limit = cfg.get_int("search_limit", 24)
    if not getattr(args, "kind", None):
        args.kind = cfg.get("kind")

    handler = getattr(args, "func", None)
    if handler is None:  # pragma: no cover
        parser.print_help()
        return 0

    try:
        return int(handler(args, cfg) or 0)
    except ProviderError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        log.warn("interrupted")
        return 130
    except BrokenPipeError:
        # `aninext search x | head` - exit quietly
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 0
    except HttpError as exc:
        log.error("network error: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "verbose", False):
            raise
        log.error("%s: %s", type(exc).__name__, exc)
        log.info("run with -v for a traceback")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
