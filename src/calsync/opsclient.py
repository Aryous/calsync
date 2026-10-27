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


def call(name: str, args: dict[str, Any], *, dry_run: bool, config_path: Path, state_dir: Path) -> "OpResult":
    """Run ``name`` through the daemon if one serves ``state_dir``, else in this process."""
    from calsync.wiring import socket_path

    sock = socket_path(state_dir)
    if _daemon_listens(sock):
        return _over_socket(sock, name, args, dry_run)
    return run_in_process(name, args, dry_run=dry_run, config_path=config_path, state_dir=state_dir)


def _daemon_listens(sock: Path) -> bool:
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


def _over_socket(sock: Path, name: str, args: dict[str, Any], dry_run: bool) -> "OpResult":
    """The daemon's answer. A transport failure (the daemon stopped meanwhile) is raised as
    httpx's error: the operation may or may not have run, so it is not retried here."""
    import httpx

    transport = httpx.HTTPTransport(uds=str(sock))
    timeout = httpx.Timeout(10.0, read=OP_TIMEOUT)
    with httpx.Client(transport=transport, base_url="http://calsync", timeout=timeout) as client:
        response = client.post(f"/api/ops/{name}", json={"args": args, "dry_run": dry_run})
    return _result(name, response)


def _result(name: str, response: "httpx.Response") -> "OpResult":
    from calsync.configfile import ConfigConflict
    from calsync.ops import OpResult, OpsUnavailable

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
    if response.status_code == 409:
        raise OpsUnavailable(message)
    if response.status_code == 404:
        raise ConfigError([f'unknown operation "{name}"'])
    if response.status_code == 400:
        raise ConfigError([message])
    raise CalsyncError(f"the daemon answered HTTP {response.status_code}")
