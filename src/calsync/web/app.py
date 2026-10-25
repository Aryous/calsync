"""The web UI over TCP: the front end, the ``/api`` routes behind a pairing cookie, and checks
on every request against DNS rebinding and cross-site requests.

- ``GET /``: ``?k=<key>`` pairs this browser (cookie, then 303 to ``/``); otherwise the app
  when paired, else the pair page. ``POST /pair`` (form field ``key``) pairs too.
- ``/api/*`` needs the cookie (401 ``unpaired`` without it). ``/static/*`` is public.
- The ``Host`` header must name this machine: ``localhost``, a loopback address, the listen
  host, an ``allowed_hosts`` entry, or (when listening on every address) any IP literal.
- A request other than GET or HEAD that carries ``Origin`` must come from this same origin.
- 10 wrong keys within 10 minutes from one client address: every attempt from it is 429 until
  the oldest of them is 10 minutes old.

The unix-socket app (``calsync.api.create_app``) has none of this: the socket's file mode is
its access control.
"""

import ipaddress
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request, cookie_parser
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from calsync import api
from calsync.config import Config
from calsync.web.auth import COOKIE, COOKIE_MAX_AGE, WILDCARDS, Keys, is_ip

STATIC_DIR = Path(__file__).resolve().parent / "static"
NOTICE_MARKER = "<!-- calsync:notice -->"
INVALID_NOTICE = (
    '<p class="notice" role="alert">This link is not valid any more. Run <code>calsync web pair</code> '
    "on the computer running calsync to get a new one.</p>"
)
LIMIT_NOTICE = '<p class="notice" role="alert">Too many attempts. Try again in a few minutes.</p>'
UNPAIRED = "This browser is not paired."
MAX_FAILURES = 10
FAILURE_WINDOW = timedelta(minutes=10)
MAX_FORM = 4096  # bytes: the pair form holds one key
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
NO_CACHE = {"Cache-Control": "no-cache"}

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


def _hostname(host: str) -> str | None:
    """The name in a ``Host`` header (``name``, ``name:port``, ``[v6]:port``), lower case."""
    host = host.strip().lower()
    if host.startswith("["):
        name, sep, rest = host[1:].partition("]")
        if not sep or (rest and not (rest.startswith(":") and rest[1:].isdigit())):
            return None
    else:
        name, sep, port = host.partition(":")
        if sep and not port.isdigit():
            return None
    return name.rstrip(".") or None


def _same_origin(origin: str, host: str) -> bool:
    parts = urlsplit(origin.strip().lower())
    return parts.scheme in {"http", "https"} and parts.netloc == host.strip().lower()


class _Attempts:
    """Wrong keys per client address within ``FAILURE_WINDOW``."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._failures: dict[str, deque[datetime]] = {}

    def _recent(self, client: str, now: datetime) -> deque[datetime]:
        times = self._failures.get(client, deque())
        while times and times[0] <= now - FAILURE_WINDOW:
            times.popleft()
        return times

    def retry_after(self, client: str) -> int | None:
        """Seconds until ``client`` may try again, or None if it may now."""
        now = self._clock()
        times = self._recent(client, now)
        if len(times) < MAX_FAILURES:
            return None
        return max(1, int((times[0] + FAILURE_WINDOW - now).total_seconds() + 0.999))

    def fail(self, client: str) -> None:
        now = self._clock()
        if len(self._failures) > 1000:  # forget the addresses that are quiet again
            for quiet in [c for c in self._failures if not self._recent(c, now)]:
                del self._failures[quiet]
        times = self._recent(client, now)
        times.append(now)
        self._failures[client] = times


class _Guard:
    """Host check, Origin check and the cookie for ``/api``, before any route runs."""

    def __init__(
        self, app: ASGIApp, *, keys: Keys, config_provider: Callable[[], Config], listen_host: str
    ) -> None:
        self._app = app
        self._keys = keys
        self._config = config_provider
        self._listen_host = listen_host.lower()

    def _host_ok(self, name: str | None) -> bool:
        if name is None:
            return False
        if name in LOOPBACK or name in self._config().web.allowed_hosts:
            return True
        if not is_ip(name):
            return name == self._listen_host
        address = ipaddress.ip_address(name)
        if address.is_loopback or self._listen_host in WILDCARDS:
            return True
        # The listen host itself, also when spelled differently ("::0001").
        return is_ip(self._listen_host) and address == ipaddress.ip_address(self._listen_host)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":  # nothing here speaks it; refused before any check
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        response: Response | None = None
        if not self._host_ok(_hostname(host)):
            response = api.error_response(400, "bad_host", "This host name is not served here.")
        elif (
            scope["method"] not in {"GET", "HEAD"}
            and "origin" in headers
            and not _same_origin(headers["origin"], host)
        ):
            response = api.error_response(403, "cross_origin", "Cross-origin requests are refused.")
        if response is None and (scope["path"] == "/api" or scope["path"].startswith("/api/")):
            cookie = cookie_parser(headers.get("cookie", "")).get(COOKIE)
            if not self._keys.valid_cookie(cookie):
                response = api.error_response(401, "unpaired", UNPAIRED)
        if response is not None:
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


class _Static(StaticFiles):
    """Static files, revalidated on every use (``no-cache``): an upgrade never mixes versions."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _page(
    name: str, *, status: int = 200, notice: str = "", headers: dict[str, str] | None = None
) -> Response:
    text = (STATIC_DIR / name).read_text(encoding="utf-8")
    if notice:
        text = text.replace(NOTICE_MARKER, notice, 1)
    return HTMLResponse(text, status_code=status, headers={**NO_CACHE, **(headers or {})})


def _pasted_key(text: str) -> str:
    """The key from the pair form: the key itself, or a whole pair link pasted."""
    text = text.strip()
    if "?" in text:
        found = parse_qs(urlsplit(text).query).get("k")
        if found:
            return found[0].strip()
    return text


class _Pages:
    def __init__(self, keys: Keys, attempts: _Attempts) -> None:
        self._keys = keys
        self._attempts = attempts

    def _paired(self, request: Request) -> bool:
        return self._keys.valid_cookie(request.cookies.get(COOKIE))

    def _attempt(self, request: Request, key: str) -> Response:
        client = request.client.host if request.client else ""
        wait = self._attempts.retry_after(client)
        if wait is not None:
            return _page("pair.html", status=429, notice=LIMIT_NOTICE, headers={"Retry-After": str(wait)})
        if not self._keys.matches(key):
            self._attempts.fail(client)
            return _page("pair.html", status=403, notice=INVALID_NOTICE)
        token = self._keys.token()
        assert token is not None  # the key just matched
        cookie = f"{COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={COOKIE_MAX_AGE}"
        if request.url.scheme == "https":
            cookie += "; Secure"
        response = RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store"})
        response.headers["Set-Cookie"] = cookie
        return response

    async def home(self, request: Request) -> Response:
        key = request.query_params.get("k")
        if key is not None:
            return self._attempt(request, key)
        return _page("index.html" if self._paired(request) else "pair.html")

    async def pair(self, request: Request) -> Response:
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_FORM:
                return api.error_response(413, "too_large", "The form is too large.")
        fields = parse_qs(body.decode("utf-8", errors="replace"))
        return self._attempt(request, _pasted_key(fields.get("key", [""])[0]))


def create_web_app(
    *,
    config_provider: Callable[[], Config],
    state_dir: Path,
    daemon: api.DaemonControl | None,
    listen_host: str,
    clock: Clock = _now,
) -> Starlette:
    """The web UI app: the pages, ``/static`` and the ``/api`` routes, behind the checks."""
    keys = Keys(state_dir)
    pages = _Pages(keys, _Attempts(clock))
    routes = [
        Route("/", api.guarded(pages.home), methods=["GET"]),
        Route("/pair", api.guarded(pages.pair), methods=["POST"]),
        *api.api_routes(config_provider=config_provider, state_dir=state_dir, daemon=daemon, clock=clock),
        Mount("/static", app=_Static(directory=STATIC_DIR), name="static"),
    ]
    guard = Middleware(_Guard, keys=keys, config_provider=config_provider, listen_host=listen_host)
    middleware = [Middleware(api.ContainErrors), guard]  # errors in the checks are contained too
    return Starlette(routes=routes, middleware=middleware, exception_handlers=api.exception_handlers)
