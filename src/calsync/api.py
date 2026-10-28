"""The local JSON API under ``/api``: what ``calsync trigger`` and the web UI talk to.

The daemon serves it on a unix socket, ``<state_dir>/calsync.sock`` (mode 0600), so only its
user can reach it. Responses carry event titles (``upcoming``, ``events``, ``event`` and ``changes``),
like ``--json``.

Every view request opens its own read-only ``StateReader`` and closes it afterwards: a reader
opened without a ``-wal`` file reads the database as immutable and would not see later writes.
The view is built in a worker thread, so the event loop (and the sync cycle on it) never waits
on SQLite. Errors are JSON, ``{"error": {"kind": ..., "message": ...}}``, never a traceback.

``api_routes`` is the route list; block ③ mounts it into an app of its own, with
authentication and static files around it.
"""

import contextlib
import errno
import functools
import json
import logging
import os
import shlex
import socket
import sqlite3
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import anyio.to_thread
import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from calsync import __version__, credentials, debug, events_view, ops, views
from calsync.config import Config
from calsync.configfile import ConfigConflict
from calsync.errors import (
    AuthError,
    ConfigError,
    ForbiddenError,
    ListenError,
    NotFoundError,
    RulePaused,
    TransientError,
    plain_error,
)
from calsync.lock import DaemonServing
from calsync.sources.google_write import WriteConflict
from calsync.wiring import state_paths

if TYPE_CHECKING:
    import asyncio

    from calsync.daemon import DaemonInfo

log = logging.getLogger(__name__)

MAX_HOURS = 168
MAX_RUNS = 500
MAX_CHANGES = 1000
MAX_CONFIG_CHANGES = 200  # the store keeps no more
MAX_BODY = 1024 * 1024  # bytes: a request body (an operation's arguments; a batch of events) is never larger
MAX_WRITES = 500  # the store keeps no more
CONFLICT = "The event changed in Google since it was read; read it again."
NO_STATE = "No sync has run yet."
UNREADABLE = "The state database could not be read."

Clock = Callable[[], datetime]


class DaemonControl(Protocol):
    """What the API needs of the daemon (``calsync.daemon.Daemon``)."""

    def request_cycle(self, rule: str | None = None) -> bool: ...

    def snapshot(self) -> "DaemonInfo": ...

    async def run_op(
        self, name: str, args: dict[str, Any], dry_run: bool, *, source: ops.Source
    ) -> ops.OpResult: ...


class _Json:
    """A body that is JSON already, for ``_Endpoints._view``."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def to_json(self) -> dict[str, Any]:
        return self._body


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        kind: str,
        message: str,
        problems: Sequence[str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message
        self.problems = problems
        self.extra = extra  # more fields of the error object (``needs_human``: the command)


def _now() -> datetime:
    return datetime.now(UTC)


def error_response(
    status: int,
    kind: str,
    message: str,
    headers: Mapping[str, str] | None = None,
    problems: Sequence[str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Response:
    error: dict[str, Any] = {"kind": kind, "message": message}
    if problems is not None:
        error["problems"] = list(problems)
    error.update(extra or {})
    return JSONResponse({"error": error}, status_code=status, headers=headers)


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
        # A local time near the ends of the calendar may have no instant (ValueError, OverflowError).
        return value if value.tzinfo is not None else value.astimezone()
    except (ValueError, OverflowError):
        raise ApiError(400, "bad_request", f"{name}: expected an ISO 8601 time, got {text!r}") from None


async def _body(request: Request) -> bytes:
    """The request body, refused (413) beyond ``MAX_BODY`` bytes without reading the rest."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise ApiError(413, "too_large", f"The request body is larger than {MAX_BODY // 1024} KiB.")
    return body


async def _trigger_rule(request: Request) -> str | None:
    """The optional ``{"rule": "R"}`` body; no body means every rule."""
    raw = await _body(request)
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


async def _op_body(request: Request) -> tuple[dict[str, Any], bool]:
    """The ``{"args": {...}, "dry_run": bool}`` body; no body means no arguments."""
    raw = await _body(request)
    if not raw.strip():
        return {}, False
    try:
        body = json.loads(raw)
    except ValueError:
        raise ApiError(400, "bad_request", "body: expected JSON") from None
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", 'body: expected an object such as {"args": {}, "dry_run": false}')
    unknown = sorted(set(body) - {"args", "dry_run"})
    if unknown:
        raise ApiError(400, "bad_request", f"{unknown[0]}: unknown field")
    args: Any = body.get("args", {})
    dry_run: Any = body.get("dry_run", False)
    if not isinstance(args, dict):
        raise ApiError(400, "bad_request", "args: expected an object")
    if not isinstance(dry_run, bool):
        raise ApiError(400, "bad_request", "dry_run: expected true or false")
    return args, dry_run


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

    async def _view(
        self,
        build: Callable[[Config, views.StateReader], Any],
        *,
        no_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``build(config, reader).to_json()`` in a worker thread, with a reader of its own.
        Without a state database: ``no_state`` if given (a list that is simply empty then), else
        503. ``events_view.NotFound`` is 404."""
        config = self._config()
        db_path = self._db_path

        def read() -> dict[str, Any]:
            # Opened, used and closed in this one thread: a sqlite3 connection is thread-bound.
            with views.StateReader(db_path) as reader:
                return build(config, reader).to_json()

        try:
            return await anyio.to_thread.run_sync(read)
        except views.StateUnavailable:
            if no_state is not None:
                return no_state
            raise ApiError(503, "no_state", NO_STATE) from None
        except events_view.NotFound:
            raise ApiError(404, "not_found", "No such event.") from None
        except sqlite3.DatabaseError:  # not a database, corrupt, or locked
            raise ApiError(500, "state_unreadable", UNREADABLE) from None
        except Exception as exc:
            raise _internal(exc) from None

    def _rule(self, request: Request) -> str | None:
        """The ``rule`` parameter, a configured rule (404 otherwise)."""
        rule = request.query_params.get("rule")
        if rule is not None and rule not in {r.name for r in self._config().rules}:
            raise ApiError(404, "unknown_rule", f"Unknown rule {rule!r}.")
        return rule

    async def status(self, request: Request) -> Response:
        now = self._clock()
        body = await self._view(lambda config, reader: views.build_status(config, reader, now))
        body["version"] = __version__
        body["daemon"] = None if self._daemon is None else self._daemon.snapshot().to_json()
        return JSONResponse(body)

    async def upcoming(self, request: Request) -> Response:
        hours = _clamped(request, "hours", 24, MAX_HOURS)
        rule = self._rule(request)
        now = self._clock()
        return JSONResponse(
            await self._view(
                lambda config, reader: views.build_upcoming(config, reader, now, hours=hours, rule=rule)
            )
        )

    async def blocked(self, request: Request) -> Response:
        rule = self._rule(request)
        return JSONResponse(
            await self._view(lambda config, reader: views.build_blocked(config, reader, rule))
        )

    async def events(self, request: Request) -> Response:
        """``GET /api/events?from&to&rule&target&q``: ``from`` / ``to`` ISO dates or times (local
        without an offset; ``to`` exclusive; default: today and a week), at most
        ``MAX_EVENT_DAYS`` apart."""
        now = self._clock()
        try:
            start, end = events_view.event_range(_instant(request, "from"), _instant(request, "to"), now)
        except (ValueError, OverflowError) as exc:
            raise ApiError(400, "bad_request", str(exc)) from None
        rule = self._rule(request)
        target: tuple[str, str] | None = None
        if (text := request.query_params.get("target")) is not None:
            try:
                target = events_view.parse_target(text)
            except ValueError as exc:
                raise ApiError(400, "bad_request", str(exc)) from None
            if not events_view.known_target(self._config(), target):
                raise ApiError(404, "unknown_target", f"No rule syncs into {text!r}.")
        query = request.query_params.get("q") or None
        return JSONResponse(
            await self._view(
                lambda config, reader: events_view.build_events(
                    config, reader, start, end, rule=rule, target=target, query=query, now=now
                )
            )
        )

    async def event(self, request: Request) -> Response:
        """``GET /api/event?id``: one event with its copies (404 ``not_found``: no such event)."""
        eid = request.query_params.get("id")
        if not eid:
            raise ApiError(400, "bad_request", "id: required")
        now = self._clock()
        return JSONResponse(
            await self._view(lambda config, reader: events_view.build_event(config, reader, eid, now))
        )

    async def config(self, request: Request) -> Response:
        """``GET /api/config``: the running config, masked (revealing exists only on the CLI)."""
        return JSONResponse(views.config_json(self._config()))

    async def config_history(self, request: Request) -> Response:
        limit = _clamped(request, "limit", 50, MAX_CONFIG_CHANGES)
        return JSONResponse(
            await self._view(
                lambda config, reader: _Json(views.config_history_json(reader.config_changes(limit))),
                no_state={"changes": []},
            )
        )

    async def calendars(self, request: Request) -> Response:
        """``GET /api/calendars?account``: the account's cached calendar list."""
        account = request.query_params.get("account")
        if not account:
            raise ApiError(400, "bad_request", "account: required")
        if account not in self._config().accounts:
            raise ApiError(404, "unknown_account", f"Unknown account {account!r}.")
        return JSONResponse(
            await self._view(
                lambda config, reader: _Json(
                    views.calendars_json(account, reader.account_calendars(account))
                ),
                no_state=views.calendars_json(account, []),
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

    async def writes(self, request: Request) -> Response:
        """``GET /api/writes?limit``: writes to Google made through calsync, newest first."""
        limit = _clamped(request, "limit", 50, MAX_WRITES)
        return JSONResponse(
            await self._view(
                lambda config, reader: _Json(views.writes_json(reader, limit)), no_state={"writes": []}
            )
        )

    async def trigger(self, request: Request) -> Response:
        rule = await _trigger_rule(request)
        if self._daemon is None:
            raise ApiError(409, "read_only", "No daemon runs sync cycles here.")
        try:
            accepted = self._daemon.request_cycle(rule)
        except RulePaused as exc:
            raise ApiError(409, "paused", str(exc)) from None
        except ValueError:
            raise ApiError(404, "unknown_rule", f"Unknown rule {rule!r}.") from None
        except ops.OpsUnavailable as exc:  # the scheduler has stopped: nothing would run it
            raise ApiError(503, "unavailable", str(exc)) from None
        if not accepted:
            raise ApiError(409, "read_only", "The daemon runs with --no-sync: it runs no sync cycles.")
        return JSONResponse({"accepted": True, "rule": rule}, status_code=202)

    def op(self, source: ops.Source) -> "Endpoint":
        """``POST /api/ops/{name}``: one operation, run by the daemon between its cycles (except
        ``trigger``, which only requests one and may wait for it)."""

        async def endpoint(request: Request) -> Response:
            name = request.path_params["name"]
            args, dry_run = await _op_body(request)
            if self._daemon is None:
                raise ApiError(409, "read_only", "No daemon runs operations here.")
            if name not in ops.OPS:
                raise ApiError(404, "unknown_op", f"Unknown operation {name!r}.")
            if (internal := ops.internal_arg(args)) is not None:
                raise ApiError(400, "bad_request", f"{internal}: an internal argument")
            if source == "ui":
                human = _terminal_only(name, args) or await _repoints_credential(name, args)
                if human is not None:
                    raise ApiError(403, "needs_human", human["reason"], extra={"needs_human": True, **human})
            try:
                result = await self._daemon.run_op(name, args, dry_run, source=source)
            except ConfigConflict as exc:
                raise ApiError(422, "config_conflict", str(exc), exc.problems) from None
            except ConfigError as exc:
                raise ApiError(422, "config", str(exc), exc.problems) from None
            except RulePaused as exc:
                raise ApiError(409, "paused", str(exc), extra={"rule": exc.rule}) from None
            except ops.DaemonStopping as exc:
                raise ApiError(503, "unavailable", str(exc)) from None
            except ops.OpsUnavailable as exc:
                raise ApiError(409, "read_only", str(exc)) from None
            except ops.TooSoon as exc:
                raise ApiError(429, "too_soon", str(exc)) from None
            except WriteConflict:
                raise ApiError(409, "conflict", CONFLICT) from None
            except NotFoundError:
                raise ApiError(404, "not_found", "Google has no such event (any more).") from None
            except AuthError as exc:  # the grant itself failed (revoked, expired)
                account = _account_of(args)
                extra = {
                    "needs_human": True,
                    "command": f"calsync auth {account}" if account else "calsync auth",
                }
                raise ApiError(409, "auth", plain_error(exc, "Google"), extra=extra) from None
            except (TransientError, TimeoutError):
                raise ApiError(502, "unreachable", "Google did not answer; try again.") from None
            except ForbiddenError:
                raise ApiError(403, "forbidden", "Google refused the change for this calendar.") from None
            return JSONResponse(result.to_json())

        return endpoint


def _account_of(args: Mapping[str, Any]) -> str | None:
    """The account an event write names: ``account``, or the one its event ``id`` belongs to."""
    if isinstance(args.get("account"), str):
        return args["account"]
    eid = args.get("id")
    if not isinstance(eid, str):
        return None
    try:
        key = events_view._decode(eid)  # pyright: ignore[reportPrivateUsage]
    except events_view.NotFound:
        return None
    return key.split("/", 1)[0] or None


def _terminal_only(name: str, args: Mapping[str, Any]) -> dict[str, str] | None:
    """What the web UI may not do, as the command a person runs instead: deleting a credential
    (credentials are managed from the terminal). None: allowed."""
    if name == "account.remove" and args.get("delete_credential") is True:  # a mistyped value: the op says so
        account = args.get("name")
        shown = account if isinstance(account, str) and account else "NAME"
        return {
            "command": f"calsync account remove {shlex.quote(shown)} --delete-credential",
            "reason": "credentials are managed from the terminal",
        }
    return None


async def _repoints_credential(name: str, args: Mapping[str, Any]) -> dict[str, str] | None:
    """``account.add`` from the web UI when the keychain already holds the new account's entry
    (left by an account removed without ``--delete-credential``): the browser would re-point a
    stored password at a server of its choosing. A person adds it from the terminal. A keychain
    that cannot be asked refuses too. None: allowed (or arguments the op itself refuses)."""
    account = args.get("name")
    if name != "account.add" or not isinstance(account, str) or not account:
        return None
    exists = await anyio.to_thread.run_sync(credentials.secret_exists, ops.account_credential(account))
    if exists is False:
        return None
    words = ["calsync", "account", "add", account]
    for key in ("provider", "server_url", "username"):
        if isinstance(value := args.get(key), str) and value:
            words += ["--" + key.replace("_", "-"), value]
    reason = (
        "the keychain already holds a credential for this account; add it from the terminal"
        if exists
        else "the keychain could not be checked; add the account from the terminal"
    )
    return {"command": shlex.join(words), "reason": reason}


def _internal(exc: Exception) -> ApiError:
    """A bug: logged by type only (its text can carry a title or a URL), answered with a fixed
    500, so neither Starlette nor uvicorn logs a traceback."""
    log.error("an API request failed (%s)", type(exc).__name__)
    debug.details(log, "an API request failed", exc)  # CALSYNC_DEBUG=1 only
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


class ContainErrors:
    """Pure ASGI middleware, outermost of an app's own: an error that escapes everything inside
    it (the checks around the routes, a mount, the router) becomes the fixed JSON 500, logged by
    type only. It is not raised on: Starlette's ``ServerErrorMiddleware`` would log it with its
    traceback after answering. Once a response has started, the error is only logged."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        started = False

        async def tracking(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self._app(scope, receive, tracking)
        except Exception as exc:
            error = _internal(exc)
            if not started:
                await error_response(error.status, error.kind, error.message)(scope, receive, send)


def api_routes(
    *,
    config_provider: Callable[[], Config],
    state_dir: Path,
    daemon: DaemonControl | None,
    clock: Clock = _now,
    ops_source: ops.Source | None = None,
) -> list[Route]:
    """The ``/api`` routes. Their errors are ``ApiError``: install ``exception_handlers`` too.

    ``ops_source``: also ``POST /api/ops/{name}``, recording changes as made from there. The
    unix-socket app passes ``"cli"``, the web UI ``"ui"`` (behind its pairing cookie and Origin
    check; deleting a credential is refused there with the command to run instead)."""
    endpoints = _Endpoints(config_provider, state_dir, daemon, clock)
    routes = [
        Route("/api/status", guarded(endpoints.status), methods=["GET"]),
        Route("/api/upcoming", guarded(endpoints.upcoming), methods=["GET"]),
        Route("/api/blocked", guarded(endpoints.blocked), methods=["GET"]),
        Route("/api/events", guarded(endpoints.events), methods=["GET"]),
        Route("/api/event", guarded(endpoints.event), methods=["GET"]),
        Route("/api/config", guarded(endpoints.config), methods=["GET"]),
        Route("/api/config/history", guarded(endpoints.config_history), methods=["GET"]),
        Route("/api/calendars", guarded(endpoints.calendars), methods=["GET"]),
        Route("/api/runs", guarded(endpoints.runs), methods=["GET"]),
        Route("/api/changes", guarded(endpoints.changes), methods=["GET"]),
        Route("/api/writes", guarded(endpoints.writes), methods=["GET"]),
        Route("/api/trigger", guarded(endpoints.trigger), methods=["POST"]),
    ]
    if ops_source is not None:
        routes.append(Route("/api/ops/{name}", guarded(endpoints.op(ops_source)), methods=["POST"]))
    return routes


async def _api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return error_response(exc.status, exc.kind, exc.message, problems=exc.problems, extra=exc.extra)


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
    # The socket's app: only this user reaches it (0600), so the CLI's operations run here.
    routes = api_routes(
        config_provider=config_provider, state_dir=state_dir, daemon=daemon, clock=clock, ops_source="cli"
    )
    return Starlette(
        routes=routes, middleware=[Middleware(ContainErrors)], exception_handlers=exception_handlers
    )


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
    old_umask = os.umask(0o177)  # created 0600: never reachable by others, not even briefly
    try:
        sock.bind(str(path))
    except BaseException:
        sock.close()
        raise
    finally:
        os.umask(old_umask)
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


def _where(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def bind_problem(host: str, port: int, exc: OSError) -> str | None:
    """What to tell the user when ``host:port`` cannot be bound; None for an unexpected error."""
    where = _where(host, port)
    reasons = {
        errno.EADDRINUSE: ("address already in use", "port"),
        errno.EACCES: ("permission denied", "port"),
        errno.EADDRNOTAVAIL: ("this computer has no such address", "address"),
    }
    if exc.errno not in reasons:
        return None
    reason, other = reasons[exc.errno]
    return f'cannot listen on {where}: {reason}; set [web] listen to another {other} or "off"'


@contextlib.contextmanager
def tcp_socket(host: str, port: int) -> Iterator[socket.socket]:
    """A listening TCP socket on ``host:port`` (port 0: any free port), closed on exit. Bound
    here, before any cycle, so a port in use stops the start: ``ListenError`` (exit code 2)."""
    try:
        family, kind, proto, _, address = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )[0]
    except socket.gaierror:
        raise ListenError(
            f'cannot listen on {_where(host, port)}: no such host name; set [web] listen to another address or "off"'
        ) from None
    sock = socket.socket(family, kind, proto)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address)
        sock.listen(100)
        sock.setblocking(False)
    except OSError as exc:
        sock.close()
        problem = bind_problem(host, port, exc)
        if problem is None:
            raise
        raise ListenError(problem) from None
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
