"""Shared HTTP plumbing: client factory, request accounting, retry with backoff.

Every request carries a ``calsync_kind`` extension (e.g. ``google-events``,
``sync-collection``). The request log records it so tests can assert the
per-cycle request budget exactly.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from calsync import __version__
from calsync.errors import TransientError

USER_AGENT = f"calsync/{__version__} (gzip)"
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
THROTTLE_STATUS = frozenset({429, 503})
MAX_RETRY_AFTER = 60.0

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class LoggedRequest:
    method: str
    host: str
    path: str
    kind: str


@dataclass(slots=True)
class RequestLog:
    entries: list[LoggedRequest] = field(default_factory=list)
    cleared: int = 0  # entries dropped by clear(); marks count them, so they stay comparable

    def record(self, request: httpx.Request) -> None:
        kind = str(request.extensions.get("calsync_kind", ""))
        self.entries.append(LoggedRequest(request.method, request.url.host, request.url.path, kind))

    def count(self, kind: str | None = None) -> int:
        return sum(1 for e in self.entries if kind is None or e.kind == kind)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.entries]

    def mark(self) -> int:
        """The number of requests recorded so far, cleared ones included."""
        return self.cleared + len(self.entries)

    def since(self, mark: int) -> list[LoggedRequest]:
        """The kept entries recorded after ``mark`` (cleared ones are gone)."""
        return self.entries[max(mark - self.cleared, 0) :]

    def clear(self) -> None:
        """Drop the kept entries (a long-lived process clears it every cycle); marks keep counting."""
        self.cleared += len(self.entries)
        self.entries.clear()


def make_client(
    log: RequestLog,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    auth: httpx.Auth | None = None,
    timeout: float = 30.0,
) -> httpx.AsyncClient:
    async def on_request(request: httpx.Request) -> None:
        log.record(request)

    options: dict[str, Any] = {
        "headers": {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
        "timeout": httpx.Timeout(timeout, connect=10.0),
        "event_hooks": {"request": [on_request]},
        "auth": auth,
        "follow_redirects": False,
        "trust_env": True,  # honours HTTPS_PROXY / HTTP_PROXY / NO_PROXY
    }
    if transport is not None:
        options["transport"] = transport
    else:
        options["http2"] = True
    return httpx.AsyncClient(**options)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return min(float(raw), MAX_RETRY_AFTER)
    except ValueError:
        return None


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    kind: str,
    max_retries: int = 3,
    sleep: Sleep = asyncio.sleep,
    on_throttle: Callable[[], None] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request; retry transport errors and 429/5xx with exponential backoff."""
    delay = 1.0
    extensions = {**kwargs.pop("extensions", {}), "calsync_kind": kind}
    for attempt in range(max_retries + 1):
        last = attempt == max_retries
        try:
            response = await client.request(method, url, extensions=extensions, **kwargs)
        except httpx.TransportError as exc:
            if last:
                raise TransientError(f"{method} {url}: {exc!r}") from exc
            await sleep(delay)
            delay *= 2
            continue
        if response.status_code not in RETRY_STATUS:
            return response
        if response.status_code in THROTTLE_STATUS and on_throttle is not None:
            on_throttle()
        if last:
            raise TransientError(f"{method} {url}: HTTP {response.status_code}")
        await sleep(_retry_after(response) or delay)
        delay *= 2
    raise AssertionError("unreachable")
