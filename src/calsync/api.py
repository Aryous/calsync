"""The local JSON API under ``/api``: what ``calsync trigger`` and the web UI talk to.

The daemon serves it on a unix socket, ``<state_dir>/calsync.sock`` (mode 0600), so only its
user can reach it. Responses carry event titles (``upcoming`` and ``changes``), like ``--json``.

Every view request opens its own read-only ``StateReader`` and closes it afterwards: a reader
opened without a ``-wal`` file reads the database as immutable and would not see later writes.
The view is built in a worker thread, so the event loop (and the sync cycle on it) never waits
on SQLite. Errors are JSON, ``{"error": {"kind": ..., "message": ...}}``, never a traceback.

``api_routes`` is the route list; block ③ mounts it into an app of its own, with
authentication and static files around it.
"""

import contextlib
import functools
import json
import logging
import os
import socket
import sqlite3
from collections.abc import Awaitable, Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import anyio.to_thread
import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from calsync import __version__, views
from calsync.config import Config
from calsync.lock import DaemonServing
from calsync.wiring import state_paths

if TYPE_CHECKING:
    import asyncio

    from calsync.daemon import DaemonInfo

log = logging.getLogger(__name__)

MAX_HOURS = 168
MAX_RUNS = 500
MAX_CHANGES = 1000
NO_STATE = "No sync has run yet."
UNREADABLE = "The state database could not be read."

Clock = Callable[[], datetime]


class DaemonControl(Protocol):
    """What the API needs of the daemon (``calsync.daemon.Daemon``)."""

    def request_cycle(self, rule: str | None = None) -> bool: ...

    def snapshot(self) -> "DaemonInfo": ...


class ApiError(Exception):
    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message


def _now() -> datetime:
    return datetime.now(UTC)


def error_response(
    status: int, kind: str, message: str, headers: Mapping[str, str] | None = None
) -> Response:
    return JSONResponse({"error": {"kind": kind, "message": message}}, status_code=status, headers=headers)


# ----- request parameters ---------------------------------------------------------------


def _int(request: Request, name: str) -> int | None:
    text = request.query_params.get(name)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        raise ApiError(400, "bad_request", f"{name}: expected an integer, got {text!r}") from None


def _clamped(request: Request, name: str, default: int, high: int) -> int:
    value = _int(request, name)
    return default if value is None else min(max(value, 1), high)


def _instant(request: Request, name: str) -> datetime | None:
    """An ISO 8601 time; one without an offset is local time (as on the command line)."""
    text = request.query_params.get(name)
    if text is None:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise ApiError(400, "bad_request", f"{name}: expected an ISO 8601 time, got {text!r}") from None
    return value if value.tzinfo is not None else value.astimezone()


async def _trigger_rule(request: Request) -> str | None:
    """The optional ``{"rule": "R"}`` body; no body means every rule."""
    raw = await request.body()
    if not raw.strip():
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        raise ApiError(400, "bad_request", "body: expected JSON") from None
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", 'body: expected an object such as {"rule": "R"}')
    rule: Any = body.get("rule")
    if rule is not None and not isinstance(rule, str):
        raise ApiError(400, "bad_request", "rule: expected a string or null")
    return rule


# ----- routes ---------------------------------------------------------------------------


class _Endpoints:
    def __init__(
        self,
        config_provider: Callable[[], Config],
        state_dir: Path,
        daemon: DaemonControl | None,
        clock: Clock,
    ) -> None:
        self._config = config_provider
        self._db_path, _ = state_paths(state_dir)
        self._daemon = daemon
        self._clock = clock

    async def _view(self, build: Callable[[Config, views.StateReader], Any]) -> dict[str, Any]:
        """``build(config, reader).to_json()`` in a worker thread, with a reader of its own."""
        config = self._config()
        db_path = self._db_path

        def read() -> dict[str, Any]:
            # Opened, used and closed in this one thread: a sqlite3 connection is thread-bound.
            with views.StateReader(db_path) as reader:
                return build(config, reader).to_json()

        try:
            return await anyio.to_thread.run_sync(read)
        except views.StateUnavailable:
            raise ApiError(503, "no_state", NO_STATE) from None
        except sqlite3.DatabaseError:  # not a database, corrupt, or locked
            raise ApiError(500, "state_unreadable", UNREADABLE) from None
        except Exception as exc:
            raise _internal(exc) from None

    async def status(self, request: Request) -> Response:
        now = self._clock()
        body = await self._view(lambda config, reader: views.build_status(config, reader, now))
        body["version"] = __version__
        body["daemon"] = None if self._daemon is None else self._daemon.snapshot().to_json()
        return JSONResponse(body)

    async def upcoming(self, request: Request) -> Response:
        hours = _clamped(request, "hours", 24, MAX_HOURS)
        rule = request.query_params.get("rule")
        if rule is not None and rule not in {r.name for r in self._config().rules}:
            raise ApiError(404, "unknown_rule", f"Unknown rule {rule!r}.")
        now = self._clock()
        return JSONResponse(
            await self._view(
                lambda config, reader: views.build_upcoming(config, reader, now, hours=hours, rule=rule)
            )
        )

    async def runs(self, request: Request) -> Response:
        # History outlives the config: a rule removed since is still a valid filter.
        limit = _clamped(request, "limit", 50, MAX_RUNS)
        rule = request.query_params.get("rule")
        return JSONResponse(
            await self._view(lambda config, reader: views.build_runs(config, reader, rule=rule, limit=limit))
        )

    async def changes(self, request: Request) -> Response:
        limit = _clamped(request, "limit", 100, MAX_CHANGES)
        rule = request.query_params.get("rule")
        since = _instant(request, "since")
        run_id = _int(request, "run_id")
        return JSONResponse(
            await self._view(
                lambda config, reader: views.build_changes(
                    config, reader, rule=rule, since=since, limit=limit, run_id=run_id
                )
            )
        )

    async def trigger(self, request: Request) -> Response:
        rule = await _trigger_rule(request)
        if self._daemon is None:
            raise ApiError(409, "read_only", "No daemon runs sync cycles here.")
        try:
            accepted = self._daemon.request_cycle(rule)
        except ValueError:
            raise ApiError(404, "unknown_rule", f"Unknown rule {rule!r}.") from None
        if not accepted:
            raise ApiError(409, "read_only", "The daemon runs with --no-sync: it runs no sync cycles.")
        return JSONResponse({"accepted": True, "rule": rule}, status_code=202)


def _internal(exc: Exception) -> ApiError:
    """A bug: logged by type only (its text can carry a title or a URL), answered with a fixed
    500, so neither Starlette nor uvicorn logs a traceback."""
    log.error("an API request failed (%s)", type(exc).__name__)
    return ApiError(500, "internal", "Internal error.")


Endpoint = Callable[[Request], Awaitable[Response]]


def guarded(endpoint: Endpoint) -> Endpoint:
    """``endpoint`` with every unexpected error turned into the fixed JSON 500."""

    @functools.wraps(endpoint)
    async def wrapper(request: Request) -> Response:
        try:
            return await endpoint(request)
        except (ApiError, HTTPException):
            raise
        except Exception as exc:
            raise _internal(exc) from None

    return wrapper


def api_routes(
    *,
    config_provider: Callable[[], Config],
    state_dir: Path,
    daemon: DaemonControl | None,
    clock: Clock = _now,
) -> list[Route]:
    """The ``/api`` routes. Their errors are ``ApiError``: install ``exception_handlers`` too."""
    endpoints = _Endpoints(config_provider, state_dir, daemon, clock)
    return [
        Route("/api/status", guarded(endpoints.status), methods=["GET"]),
        Route("/api/upcoming", guarded(endpoints.upcoming), methods=["GET"]),
        Route("/api/runs", guarded(endpoints.runs), methods=["GET"]),
        Route("/api/changes", guarded(endpoints.changes), methods=["GET"]),
        Route("/api/trigger", guarded(endpoints.trigger), methods=["POST"]),
    ]


async def _api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return error_response(exc.status, exc.kind, exc.message)


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    kinds = {404: "not_found", 405: "method_not_allowed"}
    return error_response(exc.status_code, kinds.get(exc.status_code, "http_error"), exc.detail, exc.headers)


async def _internal_error(request: Request, exc: Exception) -> Response:
    return error_response(500, "internal", "Internal error.")


exception_handlers: dict[Any, Callable[[Request, Exception], Any]] = {
    ApiError: _api_error,
    HTTPException: _http_error,
    Exception: _internal_error,
}


def create_app(
    *,
    config_provider: Callable[[], Config],
    state_dir: Path,
    daemon: DaemonControl | None,
    clock: Clock = _now,
) -> Starlette:
    routes = api_routes(config_provider=config_provider, state_dir=state_dir, daemon=daemon, clock=clock)
    return Starlette(routes=routes, exception_handlers=exception_handlers)


# ----- serving on a unix socket ---------------------------------------------------------


@contextlib.contextmanager
def unix_socket(path: Path) -> Iterator[socket.socket]:
    """A listening socket at ``path`` (mode 0600), removed again on exit.

    A socket file left behind by a process that died is removed first; one that still accepts
    connections belongs to a running daemon: ``DaemonServing``."""
    if path.is_socket():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(2)
        try:
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            path.unlink(missing_ok=True)  # stale: nobody listens on it
        except TimeoutError:
            raise DaemonServing(path) from None  # alive but busy
        else:
            raise DaemonServing(path)
        finally:
            probe.close()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    except BaseException:
        sock.close()
        raise
    inode = os.stat(path).st_ino
    try:
        os.chmod(path, 0o600)
        sock.listen(100)
        sock.setblocking(False)
        yield sock
    finally:
        sock.close()
        with contextlib.suppress(FileNotFoundError):
            if os.stat(path).st_ino == inode:  # still ours: not replaced meanwhile
                path.unlink()


class _Server(uvicorn.Server):
    """A uvicorn server that leaves signal handling alone: the daemon owns SIGINT and SIGTERM
    (uvicorn would replace their handlers with its own and re-raise them on exit)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@contextlib.contextmanager
def tcp_socket(host: str, port: int) -> Iterator[socket.socket]:
    """A listening TCP socket on ``host:port`` (port 0: any free port), closed on exit. Bound
    here, before any cycle, so a port in use stops the start."""
    family, kind, proto, _, address = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    )[0]
    sock = socket.socket(family, kind, proto)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address)
        sock.listen(100)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    try:
        yield sock
    finally:
        sock.close()


async def serve(app: Starlette, sock: socket.socket, stop: "asyncio.Event") -> None:
    """Serve ``app`` on the listening ``sock`` (unix or TCP) until ``stop`` is set (requests in
    flight finish). The server stopping on its own is an error."""
    import asyncio

    if sock.family == socket.AF_UNIX:
        where: dict[str, Any] = {"uds": sock.getsockname()}
    else:
        host, port = sock.getsockname()[:2]
        where = {"host": host, "port": port}
    config = uvicorn.Config(
        app,
        **where,
        proxy_headers=False,  # no X-Forwarded-* from anyone: the client address is the peer
        server_header=False,
        log_level="warning",
        lifespan="off",
        access_log=False,
        log_config=None,  # uvicorn's warnings go through the daemon's logging setup
        timeout_graceful_shutdown=5,
    )
    server = _Server(config)
    serving = asyncio.ensure_future(server.serve(sockets=[sock]))
    stopped = asyncio.ensure_future(stop.wait())
    try:
        done, _ = await asyncio.wait({serving, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if serving in done:
            serving.result()
            raise RuntimeError("the API server stopped")
        server.should_exit = True
        await serving
    finally:
        for task in (serving, stopped):
            task.cancel()
        await asyncio.gather(serving, stopped, return_exceptions=True)
