"""Provider registry.

A provider knows how to search one source for titles, load their details and
episodes, and hand back playable streams.

Built-in providers:
    anilist    metadata / discovery (GraphQL, no key needed)
    archive    Internet Archive - legal, public-domain & CC movies, anime, series
    demo       offline sample data, for testing without a network
    rules      user-defined site scrapers driven by JSON rule files  (see sources.d/)
    consumet   optional JSON API gateway (self-hostable)
    tmdb       optional metadata enrichment (needs TMDB_API_KEY)
"""
from __future__ import annotations

from itertools import count
from typing import Iterable

from .. import log
from .base import MediaProvider, ProviderError, SearchQuery

_registry: dict[str, MediaProvider] = {}
_order: list[str] = []


def register(provider: MediaProvider, *, replace: bool = False) -> MediaProvider:
    pid = provider.id
    if pid in _registry and not replace:
        log.debug("provider %s already registered", pid)
        return _registry[pid]
    if pid not in _registry:
        _order.append(pid)
    _registry[pid] = provider
    return provider


def unregister(pid: str) -> None:
    _registry.pop(pid, None)
    if pid in _order:
        _order.remove(pid)


def get(pid: str) -> MediaProvider | None:
    return _registry.get(pid)


def require(pid: str) -> MediaProvider:
    p = _registry.get(pid)
    if p is None:
        raise ProviderError(f"unknown provider: {pid!r} (have: {', '.join(ids()) or 'none'})")
    return p


def all_providers() -> list[MediaProvider]:
    return [_registry[p] for p in _order if p in _registry]


def ids() -> list[str]:
    return [p for p in _order if p in _registry]


def enabled() -> list[MediaProvider]:
    return [p for p in all_providers() if p.is_enabled()]


def available(*, kind: str | None = None) -> list[MediaProvider]:
    out = []
    for p in enabled():
        if kind and kind not in p.kinds and "all" not in p.kinds:
            continue
        out.append(p)
    return out


def resolve(pid: str | None, *, kind: str | None = None) -> MediaProvider:
    """Pick a provider: explicit id, else the first available one."""
    if pid and pid != "auto":
        return require(pid)
    pool = available(kind=kind)
    if not pool:
        raise ProviderError(
            "no providers enabled - run `aninext providers` to see what is available"
        )
    return pool[0]


def load_builtin(*, sources_dir: str | None = None) -> list[str]:
    """Import and register the bundled providers. Safe to call repeatedly."""
    from . import anilist, archive, demo, rules  # noqa: F401  (import side effects)

    loaded = []
    for mod in (anilist, archive, demo):
        try:
            provider = mod.build() if hasattr(mod, "build") else None
            if provider:
                register(provider)
                loaded.append(provider.id)
        except Exception as exc:  # noqa: BLE001
            log.debug("provider %s failed to load: %s", mod.__name__, exc)

    # rule-driven providers from disk
    try:
        for provider in rules.load_all(sources_dir):
            register(provider)
            loaded.append(provider.id)
    except Exception as exc:  # noqa: BLE001
        log.debug("rule providers failed to load: %s", exc)

    # optional providers that stay silent unless configured
    for name in ("tmdb", "consumet"):
        try:
            mod = __import__(f"{__name__}.{name}", fromlist=["build"])
            provider = mod.build()
            if provider is not None:
                register(provider)
                loaded.append(provider.id)
        except Exception as exc:  # noqa: BLE001
            log.debug("optional provider %s not loaded: %s", name, exc)
    return loaded


__all__ = [
    "MediaProvider",
    "ProviderError",
    "SearchQuery",
    "register",
    "unregister",
    "get",
    "require",
    "all_providers",
    "enabled",
    "available",
    "resolve",
    "ids",
    "load_builtin",
]
