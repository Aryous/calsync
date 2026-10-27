"""Run an operation where it belongs: in the running daemon, or here with the run lock.

With a daemon serving ``<state_dir>/calsync.sock`` the operation is posted to it
(``POST /api/ops/<name>``) and runs between its cycles, so the daemon's store and config stay
in one writer's hands. Without one (no socket file, or one nobody listens on) it runs in this
process while holding the run lock; a lock held by another process (``calsync run``) is
``AlreadyRunning``, exit code 4.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from calsync.errors import CalsyncError, ConfigError

if TYPE_CHECKING:
    import httpx

    from calsync.ops import OpResult

# An operation over the socket waits for a running cycle to finish; a first full sync of a
# large calendar can take minutes.
OP_TIMEOUT = 600.0
# ``trigger --wait``: the daemon answers by its own timeout (it never waits for its lock first);
# the client waits this much longer, so a slow answer is the daemon's ``timeout``, not a lost one.
TRIGGER_MARGIN = 30.0


class DaemonLost(CalsyncError):
    """The daemon accepted the operation and then went away (stopped, timed out, hung up)
    before answering: it may have run it. Not retried, and not run here instead."""

    def __init__(self) -> None:
        super().__init__(
            "lost contact with the daemon; the operation may still be applied — "
            "check `calsync config history` before retrying"
        )


def _now() -> datetime:
    return datetime.now(UTC)


def root_os_error(exc: BaseException) -> OSError | None:
    """The ``OSError`` that httpx wrapped (it chains it as the cause or the context)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, OSError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def call(
    name: str,
    args: dict[str, Any],
    *,
    dry_run: bool,
    config_path: Path,
    state_dir: Path,
    read_timeout: float | None = None,
) -> "OpResult":
    """Run ``name`` through the daemon if one serves ``state_dir`` (waiting at most
    ``read_timeout`` seconds for its answer; default ``OP_TIMEOUT``), else in this process."""
    from calsync.ops import internal_arg
    from calsync.wiring import socket_path

    if (internal := internal_arg(args)) is not None:
        raise ConfigError([f"{internal}: an internal argument"])
    sock = socket_path(state_dir)
    if daemon_listens(sock):
        return _over_socket(sock, name, args, dry_run, read_timeout)
    return run_in_process(name, args, dry_run=dry_run, config_path=config_path, state_dir=state_dir)


def daemon_listens(sock: Path) -> bool:
    """Whether a daemon accepts connections on ``sock`` (the probe ``api.unix_socket`` uses).

    A plain socket, not httpx: httpcore's sync transport leaks its socket when a unix-socket
    connect is refused. No socket file, or one nobody listens on (left by a daemon that died):
    False. A daemon too busy to accept within the timeout still counts as there."""
    import socket

    if not sock.is_socket():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(2)
    try:
        probe.connect(str(sock))
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except TimeoutError:
        return True
    except OSError as exc:  # no permission, …: someone's daemon is there, unreachable
        raise CalsyncError(f"cannot connect to the daemon socket ({type(exc).__name__})") from None
    finally:
        probe.close()
    return True


DAEMON_STATUS_TIMEOUT = 5.0  # seconds: `calsync status` never hangs on a wedged daemon


def daemon_status(state_dir: Path) -> dict[str, Any] | None:
    """The running daemon's own block (``DaemonInfo.to_json``, from ``GET /api/status`` on its
    socket); None when no daemon serves ``state_dir`` or it does not answer in time."""
    import httpx

    from calsync.wiring import socket_path

    sock = socket_path(state_dir)
    try:
        if not daemon_listens(sock):
            return None
    except CalsyncError:  # a socket it cannot reach: no block, the status shows anyway
        return None
    transport = httpx.HTTPTransport(uds=str(sock))
    try:
        with httpx.Client(
            transport=transport, base_url="http://calsync", timeout=DAEMON_STATUS_TIMEOUT
        ) as client:
            body: Any = client.get("/api/status").json()
    except (httpx.HTTPError, ValueError):
        return None
    daemon = body.get("daemon") if isinstance(body, dict) else None
    return daemon if isinstance(daemon, dict) else None


def run_in_process(
    name: str,
    args: dict[str, Any],
    *,
    dry_run: bool,
    config_path: Path,
    state_dir: Path,
    clock: Callable[[], datetime] = _now,
) -> "OpResult":
    """Run ``name`` here, holding the run lock for its duration (``AlreadyRunning`` if taken)."""
    from calsync.lock import RunLock
    from calsync.ops import OpContext, run_op
    from calsync.store import Store
    from calsync.wiring import state_paths

    db_path, lock_path = state_paths(state_dir)
    with RunLock(lock_path), Store(db_path) as store:
        ctx = OpContext(config_path=config_path, state_dir=state_dir, store=store, source="cli", clock=clock)
        return run_op(ctx, name, args, dry_run)


def _over_socket(
    sock: Path, name: str, args: dict[str, Any], dry_run: bool, read_timeout: float | None = None
) -> "OpResult":
    """The daemon's answer. A transport failure after the probe (the daemon stopped, hung up or
    did not answer in time) is ``DaemonLost``: the operation may or may not have run, so it is
    neither retried nor run here."""
    import httpx

    transport = httpx.HTTPTransport(uds=str(sock))
    timeout = httpx.Timeout(10.0, read=OP_TIMEOUT if read_timeout is None else read_timeout)
    try:
        with httpx.Client(transport=transport, base_url="http://calsync", timeout=timeout) as client:
            response = client.post(f"/api/ops/{name}", json={"args": args, "dry_run": dry_run})
    except httpx.HTTPError as exc:
        raise DaemonLost() from exc
    return _result(name, response)


def _result(name: str, response: "httpx.Response") -> "OpResult":
    from calsync.configfile import ConfigConflict
    from calsync.errors import RulePaused
    from calsync.ops import DaemonStopping, OpResult, OpsUnavailable

    try:
        body: Any = response.json()
    except ValueError:
        body = None
    if response.status_code == 200 and isinstance(body, dict):
        return OpResult.from_json(body)
    error: Any = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    message = str(error.get("message") or f"the daemon answered HTTP {response.status_code}")
    if response.status_code == 422:
        if error.get("kind") == "config_conflict":
            raise ConfigConflict()
        problems = error.get("problems")
        raise ConfigError(
            [str(p) for p in problems] if isinstance(problems, list) and problems else [message]
        )
    if response.status_code == 409 and error.get("kind") == "paused" and isinstance(error.get("rule"), str):
        raise RulePaused(error["rule"])
    if response.status_code == 409:
        raise OpsUnavailable(message)
    if response.status_code == 503 and error.get("kind") == "unavailable":
        raise DaemonStopping()
    if response.status_code == 404:
        raise ConfigError([f'unknown operation "{name}"'])
    if response.status_code == 400:
        raise ConfigError([message])
    raise CalsyncError(f"the daemon answered HTTP {response.status_code}")
