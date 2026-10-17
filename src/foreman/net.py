"""One DNS lookup per host, for everything in the process that fetches.

A sweep crawls every hostname a portfolio serves, reads TLS on each, and
re-fetches every fact source — and each of those HTTP requests resolved its
host again, because nothing between httpx, urllib and ssl caches a lookup.
Thirty-four hosts and a few dozen pages each is over a thousand queries in
a minute, which is where a home resolver's rate limit sits, and the limit
took the operator's own machine off the network with it.

Every path Foreman fetches on ends in `socket.getaddrinfo`: httpx through
anyio and the event loop's `getaddrinfo`, urllib through
`socket.create_connection`, the TLS probe through the same. So that is the one
place to remember an answer. `install()` wraps it with a cache keyed by host
and lookup kind, holding each answer for TTL seconds and each failure for
NEGATIVE_TTL, and `stats()` says what it saved — the sweep logs that line so
the saving is visible rather than assumed.

Port is not part of the key. A host resolved for 443 is the same host for 80;
the cached result is returned with the port the caller asked for.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

# How long an answer stands. Long enough to cover a sweep end to end; short
# enough that a record moved by hand is seen on the next one.
TTL_S = 600.0
# A failure is remembered briefly: a host that does not resolve would
# otherwise be asked about once per page the crawl meant to read from it.
NEGATIVE_TTL_S = 60.0
# A cap, so a long-lived dashboard process cannot grow this without bound.
MAX_ENTRIES = 4096

# The system resolver, kept twice: `_system` is what uninstall restores and is
# never touched; `_resolve` is what the cache asks, and is what a test swaps.
_system = socket.getaddrinfo
_resolve = _system
_lock = threading.Lock()
_cache: dict[tuple[Any, ...], tuple[float, list[tuple] | OSError]] = {}
_installed = False


@dataclass
class Stats:
    lookups: int = 0  # calls answered, from the cache or the resolver
    resolved: int = 0  # calls that went to the resolver
    served: int = 0  # calls answered from the cache

    @property
    def hosts(self) -> int:
        with _lock:
            return len(_cache)

    def line(self) -> str:
        return (
            f"DNS: {self.resolved} lookup(s) resolved, {self.served} served from cache "
            f"across {self.lookups} request(s)"
        )


_stats = Stats()


def _with_port(entries: list[tuple], port: Any) -> list[tuple]:
    out = []
    for family, type_, proto, canon, sockaddr in entries:
        addr = (sockaddr[0], port, *sockaddr[2:]) if port is not None else sockaddr
        out.append((family, type_, proto, canon, addr))
    return out


def _cached_getaddrinfo(
    host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
) -> list[tuple]:
    # Numeric ports only are rewritten; a service name ("https") resolves to a
    # port inside getaddrinfo, so those calls are keyed with the port and
    # returned as they came.
    numeric = port is None or isinstance(port, int) or (isinstance(port, str) and port.isdigit())
    key_port = None if numeric else port
    key = (
        str(host).lower() if isinstance(host, (str, bytes)) else host,
        key_port,
        family,
        type,
        proto,
        flags,
    )
    now = time.monotonic()
    with _lock:
        _stats.lookups += 1
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            _stats.served += 1
            answer = hit[1]
            if isinstance(answer, OSError):
                raise answer
            return _with_port(answer, int(port) if numeric and port is not None else None)
    try:
        # Resolve with port 0 so one answer serves every port.
        entries = _resolve(host, 0 if numeric else port, family, type, proto, flags)
    except OSError as exc:
        with _lock:
            _stats.resolved += 1
            _cache[key] = (now + NEGATIVE_TTL_S, exc)
            _trim()
        raise
    with _lock:
        _stats.resolved += 1
        _cache[key] = (now + TTL_S, list(entries))
        _trim()
    return _with_port(entries, int(port) if numeric and port is not None else None)


def _trim() -> None:
    # Caller holds the lock. Drop expired entries first, then the oldest.
    if len(_cache) <= MAX_ENTRIES:
        return
    now = time.monotonic()
    for k in [k for k, (until, _) in _cache.items() if until <= now]:
        del _cache[k]
    while len(_cache) > MAX_ENTRIES:
        del _cache[next(iter(_cache))]


def install() -> None:
    """Route every lookup in this process through the cache. Idempotent."""
    global _installed
    with _lock:
        if _installed:
            return
        socket.getaddrinfo = _cached_getaddrinfo  # type: ignore[assignment]
        _installed = True


def uninstall() -> None:
    global _installed
    with _lock:
        socket.getaddrinfo = _system  # type: ignore[assignment]
        _installed = False
        _cache.clear()


def clear() -> None:
    """Forget every answer. A sweep starts here, so it sees the world as it is."""
    with _lock:
        _cache.clear()


def stats() -> Stats:
    return _stats


def reset_stats() -> None:
    with _lock:
        _stats.lookups = _stats.resolved = _stats.served = 0
