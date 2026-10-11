"""Configuration: ~/.aninext/config.json plus environment overrides."""
from __future__ import annotations

import json
import os
from typing import Any

from . import log
from .paths import config_file, ensure_dirs

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "provider": "auto",              # default provider id or "auto"
    "providers": {},                 # per-provider overrides: {"demo": {"enabled": false}}
    "quality": "1080p",              # preferred quality
    "fallback_quality": "720p",
    "download_dir": "",              # empty -> auto (Termux shared storage when available)
    "workers": 4,                    # parallel network workers
    "download_workers": 3,           # parallel downloads
    "concurrent_fragments": 16,      # ffmpeg hls fragment parallelism
    "use_aria2": True,               # use aria2c for downloads when installed
    "use_ffmpeg": True,              # use ffmpeg for HLS/remux when installed
    "yt_dlp_fallback": True,         # fall back to yt-dlp for unknown hosts
    "subtitles": ["en"],
    "download_subtitles": True,
    "notifications": True,           # Termux notifications during downloads
    "wake_lock": True,               # hold a wake lock while downloading
    "open_player": "auto",           # auto | mpv | vlc | intent
    "auto_next": False,              # play next episode automatically (mpv)
    "history": True,                 # record watch history
    "search_limit": 24,
    "timeout": 25,
    "retries": 3,
    "rate_limit": 0.0,               # seconds between requests to the same host
    "colour": "auto",                # auto | always | never
    "log_level": "info",
    "sort": "relevance",
    "kind": "anime",                 # default media kind for searches
    "archive_collection": "",        # default archive.org collection
    "user_agent": "",
}


class Config(dict):
    """Dict with dot-friendly helpers and JSON persistence."""

    def __init__(self, data: dict[str, Any] | None = None, path=None) -> None:
        super().__init__()
        self.path = path or config_file()
        merged = dict(DEFAULTS)
        merged.update(data or {})
        self.update(merged)
        self._dirty = False

    # -- loading / saving --------------------------------------------------

    @classmethod
    def load(cls, path=None) -> "Config":
        path = path or config_file()
        data: dict[str, Any] = {}
        try:
            if path.exists():
                raw = path.read_text("utf-8")
                if raw.strip():
                    data = json.loads(raw)
        except (OSError, ValueError) as exc:
            log.warn("could not read %s (%s); using defaults", path, exc)
        cfg = cls(data, path)
        cfg.apply_env()
        return cfg

    def save(self) -> None:
        ensure_dirs()
        try:
            self.path.write_text(json.dumps(dict(self), indent=2, sort_keys=True) + "\n", "utf-8")
            self._dirty = False
        except OSError as exc:
            log.error("could not write %s: %s", self.path, exc)

    def apply_env(self) -> None:
        """Environment variables win over the config file."""
        mapping = {
            "ANINEXT_PROVIDER": "provider",
            "ANINEXT_QUALITY": "quality",
            "ANINEXT_DOWNLOAD_DIR": "download_dir",
            "ANINEXT_WORKERS": "workers",
            "ANINEXT_KIND": "kind",
            "ANINEXT_LOG": "log_level",
            "ANINEXT_PLAYER": "open_player",
        }
        for env, key in mapping.items():
            value = os.environ.get(env)
            if value not in (None, ""):
                self[key] = _coerce(value)

    # -- access ------------------------------------------------------------

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "y")

    def get_list(self, key: str, default: list | None = None) -> list:
        value = self.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return list(default or [])

    def provider_config(self, provider_id: str) -> dict[str, Any]:
        overrides = self.get("providers") or {}
        cfg = dict(overrides.get(provider_id) or {})
        cfg.setdefault("worker_count", self.get_int("workers", 4))
        cfg.setdefault("timeout", self.get_float("timeout", 25))
        cfg.setdefault("retries", self.get_int("retries", 3))
        cfg.setdefault("delay", self.get_float("rate_limit", 0.0))
        cfg.setdefault("use_cache", True)
        if self.get("user_agent"):
            cfg.setdefault("user_agent", self["user_agent"])
        return cfg

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        """``cfg.get("providers.consumet.enabled")`` also works."""
        if isinstance(key, str) and "." in key:
            node: Any = dict.get(self, key.split(".")[0])
            for part in key.split(".")[1:]:
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node
        return dict.get(self, key, default)

    def _set_path(self, parts: list[str], value: Any) -> None:
        section: dict = self
        for part in parts[:-1]:
            child = section.get(part)
            if not isinstance(child, dict):
                child = {}
                section[part] = child
            section = child
        leaf = parts[-1]
        if isinstance(value, str):
            value = _coerce(value)
        section[leaf] = value
        self._dirty = True

    def set(self, key: str, value: Any) -> None:  # type: ignore[override]
        if isinstance(key, str) and "." in key:
            self._set_path(key.split("."), value)
            return
        if key in DEFAULTS and isinstance(DEFAULTS[key], bool) and not isinstance(value, bool):
            value = str(value).lower() in ("1", "true", "yes", "on", "y")
        elif key in DEFAULTS and isinstance(DEFAULTS[key], int) and not isinstance(DEFAULTS[key], bool):
            try:
                value = int(value)
            except (TypeError, ValueError):
                pass
        elif key in DEFAULTS and isinstance(DEFAULTS[key], float):
            try:
                value = float(value)
            except (TypeError, ValueError):
                pass
        elif key in DEFAULTS and isinstance(DEFAULTS[key], list) and isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        dict.__setitem__(self, key, value)
        self._dirty = True

    def reset(self) -> None:
        self.clear()
        self.update(DEFAULTS)
        self._dirty = True

    def describe(self) -> str:
        lines = [f"config file: {self.path}"]
        for key in sorted(DEFAULTS):
            value = self.get(key)
            if isinstance(value, dict) and value:
                lines.append(f"  {key}:")
                for child in sorted(value):
                    lines.append(f"    {child:<20} {value[child]!r}")
            else:
                lines.append(f"  {key:<22} {value!r}")
        return "\n".join(lines)


def _coerce(value: str) -> Any:
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


_default: Config | None = None


def get_config(reload: bool = False) -> Config:
    global _default
    if _default is None or reload:
        _default = Config.load()
    return _default


def set_config(cfg: Config) -> None:
    global _default
    _default = cfg


def download_dir() -> str:
    """Resolved download directory (config -> shared storage -> home)."""
    from .paths import default_download_dir

    cfg = get_config()
    configured = str(cfg.get("download_dir") or "").strip()
    if configured:
        return str(os.path.expanduser(configured))
    return str(default_download_dir())
