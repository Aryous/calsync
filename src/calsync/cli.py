"""Command-line entry point. Heavy modules are imported inside each command
so ``calsync --version`` and ``config check`` start instantly."""

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from calsync import __version__

if TYPE_CHECKING:  # type-only: importing these at runtime would load httpx/icalendar
    from calsync.config import Config
    from calsync.engine import CycleResult, RuleResult
    from calsync.views import ChangesView, RunsView, StateReader, StatusView, UpcomingView

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_AUTH, EXIT_RUNNING = 0, 1, 2, 3, 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calsync", description="One-way calendar sync.")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument(
        "--config", type=Path, help="config file (default: $CALSYNC_CONFIG or ~/.config/calsync/config.toml)"
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="state directory (default: $CALSYNC_STATE_DIR or ~/.local/state/calsync)",
    )
    sub = parser.add_subparsers(dest="command")

    config = sub.add_parser("config", help="configuration commands")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("check", help="validate the configuration file")

    calendars = sub.add_parser("calendars", help="list calendars of an account")
    calendars.add_argument("account")

    auth = sub.add_parser("auth", help="store credentials for an account")
    auth.add_argument("account")
    auth.add_argument(
        "--refresh-token-stdin", action="store_true", help="google: read an existing refresh token from stdin"
    )

    run = sub.add_parser("run", help="run one sync cycle")
    run.add_argument("--rule", action="append", dest="rules", help="only this rule (repeatable)")
    run.add_argument("--dry-run", action="store_true", help="show the plan without writing")
    run.add_argument("--allow-deletes", action="store_true", help="bypass the mass-delete guard once")

    daemon = sub.add_parser(
        "daemon",
        help="sync on a schedule until stopped (SIGUSR1: sync now)",
        description="Sync on a schedule until SIGTERM or Ctrl-C, which stop after the current cycle. "
        "A second Ctrl-C does not force-quit; SIGKILL (kill -9) is the escape hatch.",
    )
    daemon.add_argument(
        "--no-sync", action="store_true", help="run no sync cycles (no lock, no credentials): serve only"
    )
    web_listener = daemon.add_mutually_exclusive_group()
    web_listener.add_argument(
        "--listen", type=_listen, help="serve the web UI on HOST:PORT (default: [web] listen)"
    )
    web_listener.add_argument(
        "--no-web", action="store_true", help="no web UI (no TCP listener); the local API socket stays"
    )

    web = sub.add_parser("web", help="the web UI: pair a browser")
    web_sub = web.add_subparsers(dest="web_command", required=True)
    for name, text in (
        ("pair", "print the pair link(s); creates the key if there is none"),
        ("reset-key", "replace the key: every paired browser has to pair again"),
    ):
        command = web_sub.add_parser(name, help=text)
        command.add_argument("--listen", type=_listen, help="the daemon's HOST:PORT (default: [web] listen)")

    trigger = sub.add_parser("trigger", help="ask the running daemon to sync now")
    trigger.add_argument("--rule", help="only this rule")

    status = sub.add_parser("status", help="sync health per rule (read-only)")
    status.add_argument("--json", action="store_true", help="print JSON")

    upcoming = sub.add_parser("upcoming", help="events due soon and whether they reached the target")
    upcoming.add_argument("--hours", type=_positive_int, default=24, help="how far ahead (default: 24)")
    upcoming.add_argument("--rule", help="only this rule")
    upcoming.add_argument("--json", action="store_true", help="print JSON")

    runs = sub.add_parser("runs", help="recorded sync runs, newest first")
    runs.add_argument("--rule", help="only this rule")
    runs.add_argument("--limit", type=_positive_int, default=50, help="at most this many (default: 50)")
    runs.add_argument("--json", action="store_true", help="print JSON")

    changes = sub.add_parser("changes", help="recorded changes to target copies, newest first")
    changes.add_argument("--rule", help="only this rule")
    changes.add_argument("--since", type=_instant, help="only changes at or after this ISO 8601 time")
    changes.add_argument("--limit", type=_positive_int, default=100, help="at most this many (default: 100)")
    changes.add_argument("--json", action="store_true", help="print JSON")
    return parser


def _listen(text: str) -> tuple[str, int]:
    from calsync.config import parse_listen

    try:
        return parse_listen(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}")
    return value


def _instant(text: str) -> datetime:
    """An ISO 8601 time; one without an offset is local time."""
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected an ISO 8601 time such as 2026-10-24T08:00, got {text!r}"
        ) from None
    return value if value.tzinfo is not None else value.astimezone()


def _load(args: argparse.Namespace) -> "Config":
    from calsync.config import default_config_path, load_config

    return load_config(args.config or default_config_path())


def _state_dir(args: argparse.Namespace) -> Path:
    from calsync.config import default_state_dir  # light: `web pair` loads no httpx

    return args.state_dir or default_state_dir()


def exit_code_for(exc: BaseException) -> int:
    """The exit code for an error that stopped a command."""
    from calsync.errors import AuthError, ConfigError
    from calsync.lock import AlreadyRunning

    if isinstance(exc, ConfigError):
        return EXIT_CONFIG
    if isinstance(exc, AuthError):
        return EXIT_AUTH
    if isinstance(exc, AlreadyRunning):
        return EXIT_RUNNING
    return EXIT_FAILED


def exit_code_for_results(results: Iterable["RuleResult"]) -> int:
    """`calsync run`: any auth failure 3, else any config error 2, else any non-ok rule 1."""
    results = list(results)
    kinds = {r.error_kind for r in results}
    if "auth" in kinds:
        return EXIT_AUTH
    if "config" in kinds:
        return EXIT_CONFIG
    return EXIT_OK if all(r.status == "ok" for r in results) else EXIT_FAILED


def _describe(exc: BaseException) -> str:
    """The message for an error that stopped a command (no traceback)."""
    from calsync.errors import AuthError, CalsyncError, ConfigError
    from calsync.lock import AlreadyRunning, DaemonServing

    if isinstance(exc, ConfigError):
        return "configuration error:\n" + "".join(f"  - {p}\n" for p in exc.problems)
    if isinstance(exc, AuthError):
        return f"authentication error: {exc}\n"
    if isinstance(exc, DaemonServing):
        return f"{exc}; use a copy of the state directory to serve a second one\n"
    if isinstance(exc, AlreadyRunning):
        return f"{exc}; the daemon is running: use `calsync trigger`\n"
    if isinstance(exc, CalsyncError):  # network and other expected failures
        return f"error: {exc}\n"
    return f"error: {type(exc).__name__}: {exc}\n"


def cmd_config_check(args: argparse.Namespace, out: TextIO) -> int:
    config = _load(args)
    out.write(f"ok: {len(config.accounts)} accounts, {len(config.rules)} rules\n")
    return EXIT_OK


async def _calendars(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.wiring import build_adapters

    config = _load(args)
    if args.account not in config.accounts:
        out.write(f"unknown account {args.account!r}\n")
        return EXIT_CONFIG
    adapters = await build_adapters(config, {args.account})
    try:
        adapters.adapter(args.account)  # raises the account's setup error, if any
        if args.account in adapters.sources:
            for cal in await adapters.sources[args.account].list_calendars():
                out.write(f"{cal.id}\t{cal.name}\n")
        else:
            for tcal in await adapters.targets[args.account].list_calendars():
                out.write(f"{tcal.href}\t{tcal.name}\t{tcal.mode}\n")
    finally:
        await adapters.aclose()
    return EXIT_OK


async def _google_browser_sign_in(config: "Config") -> str:
    """Browser OAuth (loopback + PKCE); returns a refresh token. Progress goes to stderr."""
    import webbrowser

    from calsync.errors import ConfigError
    from calsync.http import RequestLog, make_client
    from calsync.sources.google_auth import load_client_secret
    from calsync.sources.google_oauth import sign_in

    if config.google_client_secret_file is None:
        raise ConfigError(["google.client_secret_file: required for google accounts"])
    client_id, client_secret = load_client_secret(config.google_client_secret_file)
    async with make_client(RequestLog()) as client:
        return await sign_in(
            client,
            client_id=client_id,
            client_secret=client_secret,
            open_browser=webbrowser.open,
            out=sys.stderr,
        )


def _read_stdin_secret(prompt: str, stdin: TextIO) -> str:
    if stdin.isatty():
        return getpass.getpass(prompt)
    return stdin.read().strip()


async def _auth(args: argparse.Namespace, out: TextIO, stdin: TextIO) -> int:
    import json

    from calsync.credentials import write_secret
    from calsync.errors import CalsyncError
    from calsync.wiring import adapters_with_secret

    config = _load(args)
    account = config.accounts.get(args.account)
    if account is None:
        out.write(f"unknown account {args.account!r}\n")
        return EXIT_CONFIG
    google = account.provider == "google"
    if args.refresh_token_stdin and not google:
        out.write(f"--refresh-token-stdin applies only to google accounts; {args.account!r} is caldav\n")
        return EXIT_CONFIG
    if google and not args.refresh_token_stdin:
        try:
            raw = await _google_browser_sign_in(config)
        except CalsyncError as exc:
            out.write(f"sign-in failed, credentials not stored (existing ones unchanged): {exc}\n")
            return exit_code_for(exc)
    else:
        raw = _read_stdin_secret("Refresh token: " if google else "App-specific password: ", stdin)
    if not raw:
        out.write("empty input: nothing stored, the existing credential is unchanged\n")
        return EXIT_CONFIG
    secret = json.dumps({"refresh_token": raw}) if google else raw
    # Verify the candidate in memory first; store it only once it works.
    try:
        adapters = await adapters_with_secret(config, args.account, secret)
        try:
            found = await adapters.adapter(args.account).list_calendars()
        finally:
            await adapters.aclose()
    except CalsyncError as exc:
        out.write(f"verification failed, credentials not stored (existing ones unchanged): {exc}\n")
        return exit_code_for(exc)
    write_secret(account.credential, secret)
    out.write(f"ok: credentials stored, {len(found)} calendars visible\n")
    return EXIT_OK


def _flat(text: str) -> str:
    """One line of user text: newlines and other whitespace runs become single spaces."""
    return " ".join(text.split())


def _print_cycle(result: "CycleResult", out: TextIO, verbose: bool) -> None:
    from calsync.cycle import counts_line

    for name, r in result.rules.items():
        line = counts_line(name, r)
        if r.error:
            line += f" error={r.error!r}"
        out.write(line + "\n")
        # Titles are listed in plan.delete order only when every one was read.
        titled = r.status == "blocked" and r.blocked_titles_complete
        if r.status == "blocked":
            for title in r.blocked_titles:
                out.write(f"  - {_flat(title)}\n")
            if not r.blocked_titles_complete:
                out.write("  (titles incomplete)\n")
            out.write("  re-run with --allow-deletes if this is intended\n")
        if verbose and r.plan is not None:
            for want in r.plan.create:
                out.write(f"  + {_flat(want.series.title)} @ {want.series.start.value.isoformat()}\n")
            for want, _ in r.plan.update:
                out.write(f"  ~ {_flat(want.series.title)} @ {want.series.start.value.isoformat()}\n")
            if not (titled and len(r.blocked_titles) == len(r.plan.delete)):
                for have in r.plan.delete:  # no title known: the href is all we have
                    out.write(f"  - {have.href}\n")
    out.write(f"requests={result.requests}\n")


async def _run(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.cycle import run_cycle
    from calsync.lock import RunLock
    from calsync.store import Store
    from calsync.wiring import accounts_in_use, build_adapters, build_engine, state_paths

    config = _load(args)
    unknown = [r for r in args.rules or [] if r not in {rule.name for rule in config.rules}]
    if unknown:
        out.write(f"unknown rule(s): {', '.join(unknown)}\n")
        return EXIT_CONFIG
    db_path, lock_path = state_paths(_state_dir(args))
    with RunLock(lock_path):
        adapters = await build_adapters(config, accounts_in_use(config, args.rules))
        try:
            store = Store(db_path)
            try:
                result = await run_cycle(
                    build_engine(config, adapters, store),
                    store,
                    config,
                    adapters,
                    args.rules or None,
                    dry_run=args.dry_run,
                    allow_deletes=args.allow_deletes,
                )
            finally:
                store.close()
        finally:
            await adapters.aclose()
    _print_cycle(result, out, verbose=args.dry_run)
    return exit_code_for_results(result.rules.values())


def _configure_daemon_logging() -> None:
    """INFO lines to stderr. httpx and httpcore log every request URL at INFO and DEBUG (with
    calendar ids and sync tokens): only their warnings are let through, also with
    ``CALSYNC_DEBUG=1``, which lets calsync's own loggers through at DEBUG."""
    import logging

    from calsync import debug

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if debug.enabled():
        logging.getLogger("calsync").setLevel(logging.DEBUG)
        logging.getLogger(__name__).warning(
            "%s=1: debug logging is on; the log may contain event titles and URLs", debug.ENV
        )


def _web_listen(args: argparse.Namespace, config: "Config") -> tuple[str, int] | None:
    """The web UI's address: ``--listen``, else ``[web] listen``; None for ``--no-web`` or
    ``listen = "off"``."""
    if getattr(args, "no_web", False):
        return None
    if args.listen is not None:
        return args.listen
    return (config.web.host, config.web.port) if config.web.enabled else None


async def _daemon(args: argparse.Namespace, out: TextIO) -> int:
    """Run the daemon until SIGTERM or SIGINT; SIGUSR1 requests a cycle. Logs go to stderr."""
    import logging
    import signal

    from calsync.daemon import Daemon

    config = _load(args)
    sync = not args.no_sync
    if sync and not config.rules:
        out.write("no rules configured: nothing to sync\n")
        return EXIT_CONFIG
    _configure_daemon_logging()
    logger = logging.getLogger(__name__)
    listen = _web_listen(args, config)
    if listen is None:
        logger.info("web UI off")
    daemon = Daemon(config, _state_dir(args), sync=sync, listen=listen)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    handlers = {signal.SIGTERM: stop.set, signal.SIGINT: stop.set, signal.SIGUSR1: daemon.request_cycle}
    for signum, handler in handlers.items():
        loop.add_signal_handler(signum, handler)
    try:
        logger.info("daemon started (pid %d, sync %s)", daemon.snapshot().pid, "on" if sync else "off")
        await daemon.run(stop)
    finally:
        for signum in handlers:
            loop.remove_signal_handler(signum)
    logger.info("daemon stopped")
    return EXIT_OK


def _root_os_error(exc: BaseException) -> OSError | None:
    """The ``OSError`` that httpx wrapped (it chains it as the cause or the context)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, OSError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


async def _trigger(args: argparse.Namespace, out: TextIO) -> int:
    """Ask the daemon, over its socket, for a cycle now."""
    import httpx

    from calsync.wiring import socket_path

    transport = httpx.AsyncHTTPTransport(uds=str(socket_path(_state_dir(args))))
    body = {"rule": args.rule} if args.rule else {}
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://calsync", timeout=10) as client:
            response = await client.post("/api/trigger", json=body)
    except httpx.ConnectError as exc:
        cause = _root_os_error(exc)
        if cause is None or isinstance(cause, FileNotFoundError | ConnectionRefusedError):
            # No socket file, or one nobody listens on (left by a daemon that died).
            out.write("the daemon is not running; use calsync run\n")
        else:  # a path too long for a socket, no permission, …
            out.write(f"error: cannot connect to the daemon socket ({type(cause).__name__})\n")
        return EXIT_FAILED
    if response.status_code == 202:
        out.write("ok: cycle requested\n")
        return EXIT_OK
    if response.status_code == 409:
        out.write("the daemon runs with --no-sync\n")
        return EXIT_FAILED
    if response.status_code == 404:
        out.write(f"unknown rule(s): {args.rule}\n")
        return EXIT_CONFIG
    out.write(f"error: the daemon answered HTTP {response.status_code}\n")
    return EXIT_FAILED


WEB_OFF = 'the web UI is off ([web] listen = "off"); set [web] listen to HOST:PORT to use it\n'
PAIR_WARNING = (
    "Anyone with this link can see your calendar sync status. Reset it with `calsync web reset-key`.\n"
)


def cmd_web(args: argparse.Namespace, out: TextIO) -> int:
    """`calsync web pair` and `calsync web reset-key`: the pair links for the daemon's address."""
    from calsync.web import auth

    config = _load(args)
    listen = _web_listen(args, config)
    state_dir = _state_dir(args)
    if args.web_command == "reset-key":
        key = auth.reset_key(state_dir)  # also with the web UI off: a leaked key can be replaced
        out.write("ok: the key was replaced; every paired browser has to pair again\n")
    elif listen is None:  # nothing to pair with: no key made
        out.write(WEB_OFF)
        return EXIT_CONFIG
    else:
        key = auth.load_or_create_key(state_dir)
    if listen is None:
        out.write(WEB_OFF)
        return EXIT_OK
    host, port = listen
    for url in auth.pair_urls(key, host, port, config.web.allowed_hosts):
        out.write(url + "\n")
    out.write(PAIR_WARNING)
    return EXIT_OK


def _now() -> datetime:
    return datetime.now(UTC)


ViewBuilder = Callable[["Config", "StateReader"], "StatusView | UpcomingView | RunsView | ChangesView"]


def _show(args: argparse.Namespace, out: TextIO, build: ViewBuilder, *, check_rule: bool = True) -> int:
    """Build a read-only view from the state database and print it (as JSON with ``--json``)."""
    import sqlite3

    from calsync.views import NO_STATE_MESSAGE, StateReader, StateUnavailable
    from calsync.wiring import state_paths

    config = _load(args)
    rule = getattr(args, "rule", None)
    # History outlives the config: `runs` and `changes` also show rules removed since.
    if check_rule and rule is not None and rule not in {r.name for r in config.rules}:
        out.write(f"unknown rule(s): {rule}\n")
        return EXIT_CONFIG
    db_path, _ = state_paths(_state_dir(args))
    try:
        reader = StateReader(db_path)
        with reader:
            view = build(config, reader)
    except StateUnavailable:
        out.write(NO_STATE_MESSAGE + "\n")
        return EXIT_FAILED
    except sqlite3.DatabaseError as exc:  # not a database, corrupt, or locked
        out.write(f"error: the state database could not be read: {type(exc).__name__}\n")
        return EXIT_FAILED
    if args.json:
        out.write(json.dumps(view.to_json(), ensure_ascii=False, indent=2) + "\n")
    else:
        _print_view(view, out)
    return EXIT_OK


def _print_view(view: "StatusView | UpcomingView | RunsView | ChangesView", out: TextIO) -> None:
    from calsync.views import ChangesView, RunsView, StatusView, UpcomingView

    if isinstance(view, StatusView):
        for status in view.rules:
            last = status.last_ok_at.isoformat() if status.last_ok_at else "never"
            out.write(
                f"rule={status.rule.name} health={status.health} last_sync={last} copies={status.copies}\n"
            )
            for item in view.attention:
                if item.rule == status.rule.name:
                    hint = f" Run: {item.command}" if item.command else ""
                    out.write(f"  {item.kind}: {item.message}{hint}\n")
    elif isinstance(view, UpcomingView):
        if not view.events:
            out.write(f"no events in the next {view.hours} hours\n")
        for event in view.events:
            start = event.instance.start
            when = "all day" if start.all_day else start.utc().astimezone().strftime("%H:%M")
            alarms = ",".join(str(m) for m in event.alarms_minutes_before) or "none"
            out.write(
                f"{when}  {_flat(event.instance.title)}  [{event.target_calendar}]  alarms={alarms}  "
                f"{event.delivery}\n"
            )
    elif isinstance(view, RunsView):
        for run in view.runs:
            line = (
                f"{run.id} {run.started_at.isoformat()} rule={run.rule} status={run.status} "
                f"created={run.created} updated={run.updated} deleted={run.deleted} failed={run.failed} "
                f"duration_ms={run.duration_ms}"
            )
            for name in ("reverted", "protected", "out_of_window"):
                if getattr(run, name):
                    line += f" {name}={getattr(run, name)}"
            if run.error_kind:
                line += f" error_kind={run.error_kind}"
            if run.error:
                line += f" error={run.error!r}"
            out.write(line + "\n")
    elif isinstance(view, ChangesView):
        for change in view.changes:
            title = _flat(change.title) if change.title else "(untitled)"
            out.write(
                f"{change.at.isoformat()} rule={change.rule} {change.action} {title} object={change.object_id}\n"
            )


def cmd_status(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.views import build_status

    return _show(args, out, lambda config, reader: build_status(config, reader, _now()))


def cmd_upcoming(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.views import build_upcoming

    return _show(
        args,
        out,
        lambda config, reader: build_upcoming(config, reader, _now(), hours=args.hours, rule=args.rule),
    )


def cmd_runs(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.views import build_runs

    return _show(
        args,
        out,
        lambda config, reader: build_runs(config, reader, rule=args.rule, limit=args.limit),
        check_rule=False,
    )


def cmd_changes(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.views import build_changes

    return _show(
        args,
        out,
        lambda config, reader: build_changes(
            config, reader, rule=args.rule, since=args.since, limit=args.limit
        ),
        check_rule=False,
    )


def main(argv: Sequence[str] | None = None, *, out: TextIO = sys.stdout, stdin: TextIO = sys.stdin) -> int:
    args = _parser().parse_args(argv)
    if args.version:
        out.write(f"calsync {__version__}\n")
        return EXIT_OK
    from calsync.errors import CalsyncError

    try:
        if args.command == "config":
            return cmd_config_check(args, out)
        if args.command == "calendars":
            return asyncio.run(_calendars(args, out))
        if args.command == "auth":
            return asyncio.run(_auth(args, out, stdin))
        if args.command == "run":
            return asyncio.run(_run(args, out))
        if args.command == "daemon":
            return asyncio.run(_daemon(args, out))
        if args.command == "trigger":
            return asyncio.run(_trigger(args, out))
        if args.command == "web":
            return cmd_web(args, out)
        if args.command == "status":
            return cmd_status(args, out)
        if args.command == "upcoming":
            return cmd_upcoming(args, out)
        if args.command == "runs":
            return cmd_runs(args, out)
        if args.command == "changes":
            return cmd_changes(args, out)
    except CalsyncError as exc:
        out.write(_describe(exc))
        return exit_code_for(exc)
    except Exception as exc:
        # The network, the state database or the filesystem failing outside an adapter:
        # a message, not a traceback. Imported here so a normal start stays fast.
        import sqlite3

        import httpx

        if not isinstance(exc, httpx.HTTPError | sqlite3.Error | OSError):
            raise
        out.write(_describe(exc))
        return exit_code_for(exc)
    _parser().print_help(out)
    return EXIT_CONFIG
