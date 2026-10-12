"""SQLite store: library, watchlist, history, downloads, cache and settings.

Uses only the stdlib `sqlite3`. FTS5 is used when the bundled SQLite has it
(Android's does); otherwise search silently falls back to LIKE.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import log
from .models import HistoryEntry, Title
from .paths import db_file

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS library (
    key         TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    kind        TEXT DEFAULT 'anime',
    provider    TEXT,
    url         TEXT,
    poster      TEXT,
    year        TEXT,
    status      TEXT,
    score       TEXT,
    total_eps   INTEGER DEFAULT 0,
    watched_eps INTEGER DEFAULT 0,
    auto_download INTEGER DEFAULT 0,
    notify      INTEGER DEFAULT 1,
    tags        TEXT DEFAULT '[]',
    data        TEXT DEFAULT '{}',
    added_at    REAL,
    updated_at  REAL
);

CREATE TABLE IF NOT EXISTS episodes (
    key         TEXT NOT NULL,
    season      INTEGER NOT NULL DEFAULT 0,
    number      REAL NOT NULL,
    title       TEXT,
    url         TEXT,
    thumbnail   TEXT,
    duration    TEXT,
    provider    TEXT,
    watched     INTEGER DEFAULT 0,
    progress    REAL DEFAULT 0,
    duration_s  REAL DEFAULT 0,
    updated_at  REAL,
    PRIMARY KEY (key, season, number)
);

CREATE TABLE IF NOT EXISTS watchlist (
    key       TEXT PRIMARY KEY,
    title     TEXT,
    status    TEXT DEFAULT 'plan',      -- watching | plan | completed | dropped
    provider  TEXT,
    poster    TEXT,
    url       TEXT,
    position  INTEGER DEFAULT 0,
    added_at  REAL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title_key   TEXT,
    title       TEXT,
    season      INTEGER DEFAULT 0,
    episode     REAL,
    episode_title TEXT,
    provider    TEXT,
    url         TEXT,
    position    REAL DEFAULT 0,
    duration    REAL DEFAULT 0,
    watched_at  REAL
);

CREATE TABLE IF NOT EXISTS downloads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title_key  TEXT,
    title      TEXT,
    season     INTEGER DEFAULT 0,
    episode    REAL,
    quality    TEXT,
    url        TEXT,
    path       TEXT,
    size       INTEGER DEFAULT 0,
    status     TEXT DEFAULT 'queued',  -- queued|downloading|done|failed|paused
    error      TEXT,
    provider   TEXT,
    added_at   REAL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS cache (
    url        TEXT PRIMARY KEY,
    body       TEXT,
    stored_at  REAL
);

CREATE INDEX IF NOT EXISTS idx_history_time  ON history(watched_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_key   ON history(title_key, episode);
CREATE INDEX IF NOT EXISTS idx_episodes_key  ON episodes(key, season, number);
CREATE INDEX IF NOT EXISTS idx_dl_status     ON downloads(status);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS titles_fts USING fts5(
    key UNINDEXED, title, alt_titles, genres, synopsis
);
"""


class Database:
    """Thread-safe-ish SQLite wrapper (a connection per thread)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else db_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self.fts = False
        self._setup()

    # -- plumbing ----------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        con = getattr(self._local, "conn", None)
        if con is None:
            con = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA foreign_keys=ON")
            self._local.conn = con
        return con

    def close(self) -> None:
        con = getattr(self._local, "conn", None)
        if con is not None:
            con.close()
            self._local.conn = None

    def _setup(self) -> None:
        with self._init_lock:
            self.conn.executescript(_SCHEMA)
            try:
                self.conn.executescript(_FTS_SCHEMA)
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
                log.debug("FTS5 unavailable; falling back to LIKE search")
            cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            row = cur.fetchone()
            version = int(row[0]) if row else 0
            if version < SCHEMA_VERSION:
                self._migrate(version)
                self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.conn.commit()

    def _migrate(self, from_version: int) -> None:
        log.debug("database schema %s -> %s", from_version, SCHEMA_VERSION)
        if from_version < 3:
            # v2 -> v3: episodes are per season (numbers restart every season),
            # and history/downloads remember which season an episode was in.
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS episodes_v3 (
                    key         TEXT NOT NULL,
                    season      INTEGER NOT NULL DEFAULT 0,
                    number      REAL NOT NULL,
                    title       TEXT,
                    url         TEXT,
                    thumbnail   TEXT,
                    duration    TEXT,
                    provider    TEXT,
                    watched     INTEGER DEFAULT 0,
                    progress    REAL DEFAULT 0,
                    duration_s  REAL DEFAULT 0,
                    updated_at  REAL,
                    PRIMARY KEY (key, season, number)
                );
                INSERT OR IGNORE INTO episodes_v3
                    (key, season, number, title, url, thumbnail, duration, provider,
                     watched, progress, duration_s, updated_at)
                    SELECT key, 0, number, title, url, thumbnail, duration, provider,
                           watched, progress, duration_s, updated_at FROM episodes;
                DROP TABLE episodes;
                ALTER TABLE episodes_v3 RENAME TO episodes;
                DROP INDEX IF EXISTS idx_episodes_key;
                CREATE INDEX IF NOT EXISTS idx_episodes_key ON episodes(key, season, number);
                """
            )
            for table in ("history", "downloads"):
                columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
                if "season" not in columns:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN season INTEGER DEFAULT 0"
                    )
            self.conn.commit()

    @staticmethod
    def _bind(params: Any) -> Any:
        """Accept tuples/lists and dicts (for named placeholders)."""
        if params is None:
            return ()
        if isinstance(params, dict):
            return params
        return tuple(params)

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, self._bind(params))
        self.conn.commit()
        return cur

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, self._bind(params)).fetchall()

    def one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, self._bind(params)).fetchone()

    # -- meta --------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    # -- library -----------------------------------------------------------

    def library_add(self, title: Title, *, auto_download: bool = False, tags: list[str] | None = None) -> None:
        now = time.time()
        existing = self.one("SELECT added_at FROM library WHERE key=?", (title.key,))
        self.execute(
            """
            INSERT INTO library(key,title,kind,provider,url,poster,year,status,score,
                                total_eps,auto_download,tags,data,added_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                title=excluded.title, kind=excluded.kind, provider=excluded.provider,
                url=excluded.url, poster=excluded.poster, year=excluded.year,
                status=excluded.status, score=excluded.score,
                total_eps=excluded.total_eps, auto_download=excluded.auto_download,
                tags=excluded.tags, data=excluded.data, updated_at=excluded.updated_at
            """,
            (
                title.key, title.title, title.kind, title.provider, title.url, title.poster,
                title.year, title.status, title.score, title.total_episodes,
                1 if auto_download else 0, json.dumps(tags or []),
                json.dumps(title.to_dict(with_episodes=False)),
                existing["added_at"] if existing else now,
                now,
            ),
        )
        if self.fts:
            self._fts_upsert(title)
        if title.episodes:
            self.episodes_upsert(title)

    def library_remove(self, key: str) -> None:
        self.execute("DELETE FROM library WHERE key=?", (key,))
        self.execute("DELETE FROM episodes WHERE key=?", (key,))
        if self.fts:
            try:
                self.execute("DELETE FROM titles_fts WHERE key=?", (key,))
            except sqlite3.OperationalError:
                pass

    def library_get(self, key: str) -> dict | None:
        row = self.one("SELECT * FROM library WHERE key=?", (key,))
        return self._library_row(row) if row else None

    def library_list(self, *, kind: str | None = None, limit: int = 0, order: str = "updated_at DESC") -> list[dict]:
        sql = "SELECT * FROM library"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += f" ORDER BY {order}"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._library_row(r) for r in self.query(sql, params)]

    def library_find(self, needle: str) -> list[dict]:
        like = f"%{needle.lower()}%"
        rows = self.query(
            "SELECT * FROM library WHERE lower(title) LIKE ? OR lower(key) LIKE ? ORDER BY updated_at DESC",
            (like, like),
        )
        if rows:
            return [self._library_row(r) for r in rows]
        # fall back to fuzzy slug match
        from .models import slugify

        slug = slugify(needle)
        rows = self.query("SELECT * FROM library WHERE key LIKE ?", (f"%{slug}%",))
        return [self._library_row(r) for r in rows]

    def _library_row(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except ValueError:
            d["tags"] = []
        try:
            d["data"] = json.loads(d.get("data") or "{}")
        except ValueError:
            d["data"] = {}
        return d

    def _fts_upsert(self, title: Title) -> None:
        try:
            self.execute("DELETE FROM titles_fts WHERE key=?", (title.key,))
            self.execute(
                "INSERT INTO titles_fts(key,title,alt_titles,genres,synopsis) VALUES(?,?,?,?,?)",
                (
                    title.key,
                    title.title,
                    " ".join(title.alt_titles),
                    " ".join(title.genres),
                    title.synopsis,
                ),
            )
        except sqlite3.OperationalError as exc:
            log.debug("fts upsert failed: %s", exc)

    def search_library(self, term: str, limit: int = 30) -> list[dict]:
        if self.fts and term:
            try:
                rows = self.query(
                    "SELECT key FROM titles_fts WHERE titles_fts MATCH ? LIMIT ?",
                    (_fts_query(term), limit),
                )
                out = []
                for r in rows:
                    item = self.library_get(r["key"])
                    if item:
                        out.append(item)
                if out:
                    return out
            except sqlite3.OperationalError:
                pass
        return self.library_find(term)[:limit]

    # -- episodes ----------------------------------------------------------

    def episodes_upsert(self, title: Title) -> int:
        now = time.time()
        rows = [
            (
                title.key, int(e.season or 0), float(e.number), e.title, e.url, e.thumbnail,
                e.duration, e.provider or title.provider, now,
            )
            for e in title.episodes
        ]
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT INTO episodes(key,season,number,title,url,thumbnail,duration,provider,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key,season,number) DO UPDATE SET
                title=excluded.title, url=excluded.url, thumbnail=excluded.thumbnail,
                duration=excluded.duration, provider=excluded.provider,
                updated_at=excluded.updated_at
            """,
            rows,
        )
        self.conn.commit()
        self.execute(
            "UPDATE library SET total_eps=MAX(total_eps, ?) WHERE key=?",
            (len(rows), title.key),
        )
        return len(rows)

    def episodes_list(self, key: str) -> list[dict]:
        rows = self.query("SELECT * FROM episodes WHERE key=? ORDER BY number", (key,))
        return [dict(r) for r in rows]

    def episode_mark(self, key: str, number: float, *, season: int = 0, watched: bool = True,
                     progress: float | None = None, duration: float | None = None) -> None:
        now = time.time()
        self.execute(
            """
            INSERT INTO episodes(key,season,number,watched,progress,duration_s,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(key,season,number) DO UPDATE SET
                watched=excluded.watched,
                progress=COALESCE(?, episodes.progress),
                duration_s=COALESCE(?, episodes.duration_s),
                updated_at=excluded.updated_at
            """,
            (key, int(season or 0), float(number), 1 if watched else 0, progress or 0,
             duration or 0, now, progress, duration),
        )
        if watched:
            self.execute(
                "UPDATE library SET watched_eps=(SELECT COUNT(*) FROM episodes WHERE key=? AND watched=1),"
                " updated_at=? WHERE key=?",
                (key, now, key),
            )

    def episode_progress(self, key: str) -> dict[tuple[int, float], dict]:
        """Watched/progress state, keyed by (season, number)."""
        rows = self.query("SELECT * FROM episodes WHERE key=?", (key,))
        return {(int(r["season"] or 0), float(r["number"])): dict(r) for r in rows}

    def next_unwatched(self, key: str, season: int | None = None) -> float | None:
        """The next episode to watch.

        With a season: inside that season. Without one, and for shows that have
        several seasons, we continue the season the user was last watching
        (falling back to the first, then to the newest for what has not aired
        yet) - episode numbers restart every season, so a plain MIN() would
        happily send you back to season 1.
        """
        if season is None:
            seasons = {
                int(row["season"] or 0)
                for row in self.query("SELECT DISTINCT season FROM episodes WHERE key=?", (key,))
            }
            if len(seasons) > 1:
                return self._next_across_seasons(key, seasons)

        if season is not None:
            where, params = "key=? AND season=?", (key, int(season))
        else:
            where, params = "key=?", (key,)
        row = self.one(
            f"SELECT MIN(number) AS n FROM episodes WHERE {where} AND watched=0 AND number > 0",
            params,
        )
        if row and row["n"] is not None:
            return float(row["n"])
        row = self.one(f"SELECT MAX(number) AS n FROM episodes WHERE {where} AND watched=1", params)
        if row and row["n"] is not None:
            return float(row["n"]) + 1
        return None

    def _next_across_seasons(self, key: str, seasons: set[int]) -> float | None:
        last = self.one(
            "SELECT season FROM episodes WHERE key=? AND watched=1 AND season > 0 "
            "ORDER BY updated_at DESC, number DESC LIMIT 1",
            (key,),
        )
        ordered = sorted(s for s in seasons if s > 0) or [0]
        start = int(last["season"]) if last and int(last["season"]) in ordered else ordered[0]
        for candidate in [s for s in ordered if s >= start]:
            number = self.next_unwatched(key, season=candidate)
            if number is None:
                continue
            known = self.one(
                "SELECT 1 FROM episodes WHERE key=? AND season=? AND number=? AND watched=0",
                (key, candidate, number),
            )
            if known:
                return number
        # nothing left to watch in any season: offer whatever comes after the
        # newest one, which is how ongoing shows announce new episodes
        return self.next_unwatched(key, season=ordered[-1])

    # -- watchlist ---------------------------------------------------------

    def watchlist_add(self, key: str, title: str, *, status: str = "plan", provider: str = "",
                      poster: str = "", url: str = "") -> None:
        now = time.time()
        self.execute(
            """
            INSERT INTO watchlist(key,title,status,provider,poster,url,added_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET status=excluded.status, title=excluded.title,
                poster=excluded.poster, url=excluded.url, updated_at=excluded.updated_at
            """,
            (key, title, status, provider, poster, url, now, now),
        )

    def watchlist_remove(self, key: str) -> None:
        self.execute("DELETE FROM watchlist WHERE key=?", (key,))

    def watchlist_list(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.query(
                "SELECT * FROM watchlist WHERE status=? ORDER BY updated_at DESC", (status,)
            )
        else:
            rows = self.query("SELECT * FROM watchlist ORDER BY updated_at DESC")
        return [dict(r) for r in rows]

    def watchlist_status(self, key: str) -> str | None:
        row = self.one("SELECT status FROM watchlist WHERE key=?", (key,))
        return row["status"] if row else None

    def watchlist_bump(self, key: str, position: int) -> None:
        self.execute(
            "UPDATE watchlist SET position=?, updated_at=?, status=CASE WHEN status='plan' "
            "THEN 'watching' ELSE status END WHERE key=?",
            (position, time.time(), key),
        )

    # -- history -----------------------------------------------------------

    def history_add(self, entry: HistoryEntry | dict) -> None:
        if isinstance(entry, HistoryEntry):
            data = {
                "title_key": entry.title_key,
                "title": entry.title,
                "season": entry.season,
                "episode": entry.episode,
                "episode_title": entry.episode_title,
                "provider": entry.provider,
                "url": entry.url,
                "position": entry.position,
                "duration": entry.duration,
            }
        else:
            data = dict(entry)
            data.setdefault("season", 0)      # plain dicts may not carry one
        data.setdefault("season", 0)
        data["watched_at"] = data.get("watched_at") or time.time()
        self.execute(
            """
            INSERT INTO history(title_key,title,season,episode,episode_title,provider,url,
                                position,duration,watched_at)
            VALUES(:title_key,:title,:season,:episode,:episode_title,:provider,:url,
                   :position,:duration,:watched_at)
            """,
            {
                "title_key": data.get("title_key", ""),
                "title": data.get("title", ""),
                "season": int(data.get("season") or 0),
                "episode": float(data.get("episode") or 0),
                "episode_title": data.get("episode_title", ""),
                "provider": data.get("provider", ""),
                "url": data.get("url", ""),
                "position": float(data.get("position") or 0),
                "duration": float(data.get("duration") or 0),
                "watched_at": data["watched_at"],
            },
        )
        key = data.get("title_key")
        if key:
            # remember where the user stopped watching, so playback can resume
            self.execute(
                "UPDATE episodes SET watched=1, progress=?, duration_s=?, updated_at=? "
                "WHERE key=? AND season=? AND number=?",
                (
                    float(data.get("position") or 0),
                    float(data.get("duration") or 0),
                    time.time(),
                    key,
                    int(data.get("season") or 0),
                    float(data.get("episode") or 0),
                ),
            )

    def history_list(self, limit: int = 30, *, title_key: str | None = None,
                     unique_titles: bool = False) -> list[dict]:
        if unique_titles:
            sql = """
                SELECT h.* FROM history h
                JOIN (SELECT title_key, MAX(watched_at) AS mx FROM history
                      WHERE title_key <> '' GROUP BY title_key) t
                  ON h.title_key = t.title_key AND h.watched_at = t.mx
                ORDER BY h.watched_at DESC LIMIT ?
            """
            return [dict(r) for r in self.query(sql, (limit,))]
        if title_key:
            rows = self.query(
                "SELECT * FROM history WHERE title_key=? ORDER BY watched_at DESC LIMIT ?",
                (title_key, limit),
            )
        else:
            rows = self.query("SELECT * FROM history ORDER BY watched_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def history_last(self, title_key: str) -> dict | None:
        row = self.one(
            "SELECT * FROM history WHERE title_key=? ORDER BY watched_at DESC LIMIT 1", (title_key,)
        )
        return dict(row) if row else None

    def history_clear(self, title_key: str | None = None) -> int:
        if title_key:
            cur = self.execute("DELETE FROM history WHERE title_key=?", (title_key,))
        else:
            cur = self.execute("DELETE FROM history")
        return cur.rowcount

    def continue_watching(self, limit: int = 20) -> list[dict]:
        """Shows with an unfinished episode, most recent first."""
        rows = self.query(
            """
            SELECT h.*, l.poster AS poster, l.total_eps AS total_eps, l.watched_eps AS watched_eps,
                   l.kind AS kind
            FROM history h
            LEFT JOIN library l ON l.key = h.title_key
            WHERE h.title_key <> ''
            GROUP BY h.title_key
            HAVING h.watched_at = MAX(h.watched_at)
            ORDER BY h.watched_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        out = []
        for r in rows:
            d = dict(r)
            remaining = None
            try:
                nxt = self.next_unwatched(d["title_key"]) if d["title_key"] else None
                remaining = nxt
            except Exception:  # noqa: BLE001
                remaining = None
            d["next_episode"] = remaining
            out.append(d)
        return out

    # -- downloads ---------------------------------------------------------

    def download_add(self, *, title_key: str, title: str, episode: float, quality: str,
                     season: int = 0,
                     url: str, path: str = "", size: int = 0, provider: str = "",
                     status: str = "queued") -> int:
        now = time.time()
        cur = self.execute(
            """
            INSERT INTO downloads(title_key,title,season,episode,quality,url,path,size,status,
                                  provider,added_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (title_key, title, int(season or 0), float(episode), quality, url, path, size,
             status, provider, now, now),
        )
        return int(cur.lastrowid or 0)

    def download_update(self, dl_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE downloads SET {cols} WHERE id=?", (*fields.values(), dl_id))

    def download_delete(self, dl_id: int) -> None:
        self.execute("DELETE FROM downloads WHERE id=?", (dl_id,))

    def download_get(self, dl_id: int) -> dict | None:
        row = self.one("SELECT * FROM downloads WHERE id=?", (dl_id,))
        return dict(row) if row else None

    def downloads_list(self, *, status: str | None = None, title_key: str | None = None,
                       limit: int = 0) -> list[dict]:
        sql = "SELECT * FROM downloads WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if title_key:
            sql += " AND title_key=?"
            params.append(title_key)
        sql += " ORDER BY added_at DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in self.query(sql, params)]

    def download_find(self, *, title_key: str, episode: float) -> dict | None:
        row = self.one(
            "SELECT * FROM downloads WHERE title_key=? AND episode=? AND status IN ('done','downloading')"
            " ORDER BY added_at DESC LIMIT 1",
            (title_key, float(episode)),
        )
        return dict(row) if row else None

    # -- misc --------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        def count(table: str, where: str = "") -> int:
            row = self.one(f"SELECT COUNT(*) AS c FROM {table} {where}".strip())
            return int(row["c"]) if row else 0

        return {
            "library": count("library"),
            "watchlist": count("watchlist"),
            "history": count("history"),
            "episodes": count("episodes"),
            "downloads": count("downloads"),
            "downloaded": count("downloads", "WHERE status='done'"),
        }

    def vacuum(self) -> None:
        self.execute("VACUUM")

    def reset(self) -> None:
        for table in ("library", "episodes", "watchlist", "history", "downloads", "cache", "meta"):
            try:
                self.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                pass
        if self.fts:
            try:
                self.execute("DELETE FROM titles_fts")
            except sqlite3.OperationalError:
                pass


def _fts_query(term: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression."""
    clean = "".join(c if (c.isalnum() or c in " -_") else " " for c in term)
    words = [w for w in clean.split() if w]
    if not words:
        return '""'
    return " ".join(f"{w}*" for w in words)


# --------------------------------------------------------------------------
# module level default database
# --------------------------------------------------------------------------

_default: Database | None = None
_lock = threading.Lock()


def get_db() -> Database:
    global _default
    with _lock:
        if _default is None:
            _default = Database()
        return _default


def set_db(db: Database) -> None:
    global _default
    _default = db


def reset_db() -> None:
    global _default
    if _default is not None:
        _default.close()
    _default = None
