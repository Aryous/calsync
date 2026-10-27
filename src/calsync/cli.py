"""Command-line entry point. Heavy modules are imported inside each command
so ``calsync --version`` and ``config check`` start instantly."""

import argparse
import asyncio
import contextlib
import getpass
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TextIO

from calsync import __version__

if TYPE_CHECKING:  # type-only: importing these at runtime would load httpx/icalendar
    import httpx

    from calsync.config import Config
    from calsync.engine import CycleResult, RuleResult
    from calsync.events_view import EventDetail, EventsView
    from calsync.model import When
    from calsync.ops import OpResult
    from calsync.store import Store
    from calsync.views import BlockedView, ChangesView, RunsView, StateReader, StatusView, UpcomingView

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
    get = config_sub.add_parser("get", help="print the parsed configuration as JSON")
    get.add_argument("--reveal", action="store_true", help="show URLs, usernames and credential refs in full")
    get.add_argument("--json", action="store_true", help="print JSON (the default for this command)")
    history = config_sub.add_parser("history", help="recorded config changes, newest first")
    history.add_argument("--limit", type=_positive_int, default=50, help="at most this many (default: 50)")
    history.add_argument("--json", action="store_true", help="print JSON")
    setting = _writer(config_sub, "set", "set one setting (an empty VALUE removes it)")
    setting.add_argument(
        "key", help="e.g. runtime.interval, notify.bark_url, web.allowed_hosts, google.client_secret_file"
    )
    setting.add_argument("value", help='e.g. 2m, https://..., "a.local,b.local"')
    _writer(config_sub, "undo", "undo the newest config change not yet undone")

    rule_cmd = sub.add_parser("rule", help="add, change or remove a rule")
    rule_sub = rule_cmd.add_subparsers(dest="rule_command", required=True)
    add = _writer(rule_sub, "add", "add a rule")
    add.add_argument("name")
    add.add_argument("--source", required=True, metavar="ACCOUNT:CALENDAR", help="the Google calendar")
    add.add_argument("--target", required=True, metavar="ACCOUNT:CALENDAR", help="the CalDAV calendar")
    add.add_argument("--create-target", action="store_true", help="create the target calendar if missing")
    add.add_argument("--window", metavar="all|PAST:FUTURE", help="e.g. 30d:365d (default: all of time)")
    add.add_argument(
        "--alarms",
        metavar="copy|strip|-10m,0m",
        help="alerts on the copies (default: copy); offsets as --alarms=-10m,0m",
    )
    add.add_argument("--content", choices=("full", "busy"), help="busy: copies show only Busy")
    add.add_argument("--attendees", choices=("none", "notes"), help="attendees into the notes or dropped")
    add.add_argument("--conference", choices=("copy", "none"), help="the meeting link")
    add.add_argument(
        "--preview", action="store_true", help="show what the rule would sync and stop (writes nothing)"
    )
    change = _writer(rule_sub, "set", "change one setting of a rule")
    change.add_argument("name")
    change.add_argument(
        "key", help="window, alarms, paused, retire, fields.content, delete_guard.max_count, ..."
    )
    change.add_argument(
        "value", help="window all|PAST:FUTURE; alarms copy|strip|-10m,0m; true|false; numbers"
    )
    remove = _writer(rule_sub, "remove", "remove a rule (its copies stay unless --delete-copies)")
    remove.add_argument("name")
    remove.add_argument(
        "--delete-copies", action="store_true", help="delete its copies first, then remove the rule"
    )
    for name, text in (("pause", "pause a rule: no cycle syncs it"), ("resume", "resume a paused rule")):
        _writer(sub, name, text).add_argument("rule")

    blocked = sub.add_parser("blocked", help="deletes held back by the mass-delete guard (read-only)")
    blocked.add_argument("--rule", help="only this rule")
    blocked.add_argument("--json", action="store_true", help="print JSON")
    approve = _writer(sub, "approve", "let a rule's blocked deletes through: exactly the listed set")
    approve.add_argument("rule")
    approve.add_argument(
        "--fingerprint", help="the set to approve, as `calsync blocked` shows it (default: the current one)"
    )
    approve.add_argument(
        "--yes", action="store_true", help="approve without asking (needed without a terminal)"
    )
    approve.add_argument(
        "--wait", action="store_true", help="through the daemon: wait for the cycle that deletes them"
    )

    account = sub.add_parser("account", help="add or remove an account")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    add_account = _writer(account_sub, "add", "add an account; then a person runs calsync auth NAME")
    add_account.add_argument("name")
    add_account.add_argument("--provider", required=True, choices=("google", "caldav"))
    add_account.add_argument("--server-url", help='caldav: "icloud" or the server URL')
    add_account.add_argument("--username", help="caldav: the login name")
    remove_account = _writer(account_sub, "remove", "remove an account no rule uses")
    remove_account.add_argument("name")
    remove_account.add_argument(
        "--delete-credential", action="store_true", help="also delete its keychain entry"
    )

    calendars = sub.add_parser("calendars", help="list calendars of an account (from the cache)")
    calendars.add_argument("account")
    calendars.add_argument(
        "--refresh", action="store_true", help="fetch the list now (through the daemon if it runs)"
    )
    calendars.add_argument("--json", action="store_true", help="print JSON")
    calendars.set_defaults(op_command=True)  # it may run calendars.refresh: a busy lock reads as such

    auth = sub.add_parser("auth", help="store credentials for an account")
    auth.add_argument("account")
    auth.add_argument(
        "--refresh-token-stdin", action="store_true", help="google: read an existing refresh token from stdin"
    )

    notify_cmd = sub.add_parser("notify", help="push notifications (Bark, webhook)")
    notify_sub = notify_cmd.add_subparsers(dest="notify_command", required=True)
    notify_test = notify_sub.add_parser("test", help="send a test push to every configured channel")
    notify_test.add_argument("--json", action="store_true", help="print JSON")
    notify_test.set_defaults(op_command=True)  # through the daemon if it runs: a busy lock reads as such

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

    trigger = sub.add_parser(
        "trigger", help="sync now: ask the running daemon for a cycle, or run one here without a daemon"
    )
    trigger.add_argument("--rule", help="only this rule")
    trigger.add_argument("--wait", action="store_true", help="wait for that cycle and show its result")
    trigger.add_argument(
        "--timeout",
        type=_trigger_timeout,
        default=120,
        help="with --wait: seconds to wait for the daemon's cycle (default: 120, at most 600)",
    )
    trigger.add_argument("--json", action="store_true", help="print JSON")
    trigger.add_argument(
        "--dry-run", action="store_true", help="refused: trigger has no dry run (use calsync run --dry-run)"
    )

    status = sub.add_parser("status", help="sync health per rule (read-only)")
    status.add_argument("--json", action="store_true", help="print JSON")

    upcoming = sub.add_parser("upcoming", help="events due soon and whether they reached the target")
    upcoming.add_argument("--hours", type=_positive_int, default=24, help="how far ahead (default: 24)")
    upcoming.add_argument("--rule", help="only this rule")
    upcoming.add_argument("--json", action="store_true", help="print JSON")

    events = sub.add_parser("events", help="events by date, each with its delivery per rule (read-only)")
    events.add_argument("--from", dest="start", type=_instant, help="ISO date or time (default: today)")
    events.add_argument(
        "--to", dest="end", type=_instant, help="ISO date or time, exclusive (default: --from + 7 days)"
    )
    events.add_argument("--rule", help="only this rule's deliveries")
    events.add_argument("--target", metavar="ACCOUNT:CALENDAR", help="only deliveries into this calendar")
    events.add_argument("--query", help="only events whose title or location contains this text")
    events.add_argument("--json", action="store_true", help="print JSON")
    event = sub.add_parser("event", help="one event: the source and each copy")
    event_sub = event.add_subparsers(dest="event_command", required=True)
    show = event_sub.add_parser("show", help="the event, its original and its copies")
    show.add_argument("id", help="an id from `calsync events --json`")
    show.add_argument("--json", action="store_true", help="print JSON")

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


def _writer(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]", name: str, text: str
) -> argparse.ArgumentParser:
    """A state-changing command: ``--dry-run`` and ``--json``."""
    command = sub.add_parser(name, help=text)
    command.add_argument("--dry-run", action="store_true", help="validate and show the diff; write nothing")
    command.add_argument("--json", action="store_true", help="print JSON")
    command.set_defaults(op_command=True)
    return command


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


def _trigger_timeout(text: str) -> int:
    value = _positive_int(text)
    if value > 600:
        raise argparse.ArgumentTypeError("at most 600 seconds")
    return value


def _instant(text: str) -> datetime:
    """An ISO 8601 time; one without an offset is local time."""
    try:
        value = datetime.fromisoformat(text)
        # A local time near the ends of the calendar may have no instant (ValueError, OverflowError).
        return value if value.tzinfo is not None else value.astimezone()
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError(
            f"expected an ISO 8601 time such as 2026-10-24T08:00, got {text!r}"
        ) from None


def _config_path(args: argparse.Namespace) -> Path:
    from calsync.config import default_config_path

    return args.config or default_config_path()


def _load(args: argparse.Namespace) -> "Config":
    from calsync.config import load_config

    return load_config(_config_path(args))


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


def _describe(exc: BaseException, args: argparse.Namespace | None = None) -> str:
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
        if getattr(args, "op_command", False):  # the daemon would have run it: not the daemon
            return f"{exc}; try again once it has finished\n"
        command = getattr(args, "command", None)
        if command == "trigger":  # `calsync run`, or a daemon that has not bound its socket yet
            return f"{exc}; if a daemon is starting, try again in a moment\n"
        if command == "daemon":
            return f"{exc}; wait for it to finish, or stop the other daemon\n"
        if command == "run" and getattr(args, "allow_deletes", False):
            rules = getattr(args, "rules", None) or []
            rule = rules[0] if len(rules) == 1 else "RULE"
            return (
                f"{exc}. If it is the daemon, approve the blocked deletes through it: check "
                f"`calsync blocked --rule {rule}`, then `calsync approve {rule}`\n"
            )
        return f"{exc}; if it is the daemon, use `calsync trigger`\n"
    if isinstance(exc, CalsyncError):  # network and other expected failures
        return f"error: {exc}\n"
    return f"error: {type(exc).__name__}: {exc}\n"


def cmd_config_check(args: argparse.Namespace, out: TextIO) -> int:
    config = _load(args)
    out.write(f"ok: {len(config.accounts)} accounts, {len(config.rules)} rules\n")
    return EXIT_OK


def _dump(body: object, out: TextIO) -> None:
    out.write(json.dumps(body, ensure_ascii=False, indent=2) + "\n")


def _error_kind(exc: BaseException) -> str:
    from calsync.configfile import ConfigConflict
    from calsync.errors import ConfigError
    from calsync.lock import AlreadyRunning
    from calsync.opsclient import DaemonLost

    if isinstance(exc, ConfigConflict):
        return "conflict"
    if isinstance(exc, ConfigError):
        return "config"
    if isinstance(exc, AlreadyRunning):
        return "locked"
    if isinstance(exc, DaemonLost):
        return "daemon_lost"
    return "error"


# Operations that report an account they could not reach in their result (``error_kind`` in
# ``data``) instead of raising; the CLI exits by that kind. Named here, never inferred from a
# ``data`` key another operation might also carry.
EXITS_BY_UNREACHED_ACCOUNT = frozenset({"rule.preview"})


def _ops_command(args: argparse.Namespace, out: TextIO, name: str, op_args: dict[str, object]) -> int:
    """Run one operation (through the daemon if it runs, else here with the lock) and print it:
    the summary, the diff with ``--dry-run``, and the command a person runs next, if any."""
    from calsync import opsclient
    from calsync.errors import CalsyncError, ConfigError

    try:
        result = opsclient.call(
            name, op_args, dry_run=args.dry_run, config_path=_config_path(args), state_dir=_state_dir(args)
        )
    except CalsyncError as exc:
        if not args.json:
            raise
        error: dict[str, object] = {"kind": _error_kind(exc), "message": str(exc)}
        if isinstance(exc, ConfigError):
            error["problems"] = exc.problems
        _dump({"error": error}, out)
        return exit_code_for(exc)
    code = EXIT_OK
    if result.op in EXITS_BY_UNREACHED_ACCOUNT and (kind := result.data.get("error_kind")):
        code = {"auth": EXIT_AUTH, "config": EXIT_CONFIG}.get(kind, EXIT_FAILED)
    if args.json:
        _dump(result.to_json(), out)
        return code
    out.write(result.summary + "\n")
    if args.dry_run and result.diff:
        out.write(result.diff)
    if result.needs_human:
        out.write(f"next: {result.needs_human['command']}  ({result.needs_human['reason']})\n")
    return code


def _op_for(args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    """The operation and its arguments for a write command."""
    if args.command == "config":
        if args.config_command == "set":
            value = args.value
            # A quoted ~ reaches us unexpanded; an empty value removes the key, as for any setting.
            if args.key == "google.client_secret_file" and value.strip():
                value = str(Path(value).expanduser())
            return "config.set", {"key": args.key, "value": value}
        return "config.undo", {}
    if args.command in ("pause", "resume"):
        return f"rule.{args.command}", {"name": args.rule}
    if args.command == "rule":
        if args.rule_command == "add":
            names = (
                "source",
                "target",
                "create_target",
                "window",
                "alarms",
                "content",
                "attendees",
                "conference",
            )
            given = {n: getattr(args, n) for n in names if getattr(args, n) not in (None, False)}
            return "rule.preview" if args.preview else "rule.add", {"name": args.name, **given}
        if args.rule_command == "set":
            return "rule.set", {"name": args.name, "key": args.key, "value": args.value}
        op_args: dict[str, object] = {"name": args.name}
        if args.delete_copies:
            op_args["delete_copies"] = True
        return "rule.remove", op_args
    assert args.command == "account"
    if args.account_command == "add":
        given = {n: getattr(args, n) for n in ("server_url", "username") if getattr(args, n) is not None}
        return "account.add", {"name": args.name, "provider": args.provider, **given}
    op_args = {"name": args.name}
    if args.delete_credential:
        op_args["delete_credential"] = True
    return "account.remove", op_args


YES_NEEDED = "pass --yes to approve without a prompt"
PROMPT_TITLES = 10
APPROVE_EXIT = {
    "approved": EXIT_OK,
    "pending": EXIT_OK,
    "would_approve": EXIT_OK,
    "stale": EXIT_FAILED,
    "failed": EXIT_FAILED,
    "not_blocked": EXIT_CONFIG,
}


def cmd_approve(args: argparse.Namespace, out: TextIO, stdin: TextIO) -> int:
    """`calsync approve RULE`: on a terminal without ``--yes``, show the set and let the person
    confirm it by typing the rule name; the set they saw (its fingerprint) is what is approved."""
    from calsync import opsclient
    from calsync.errors import CalsyncError, ConfigError

    fingerprint = args.fingerprint
    if not args.yes and not args.dry_run:
        if args.json or not stdin.isatty():  # no prompt in JSON mode or without a person
            if args.json:
                _dump({"error": {"kind": "config", "message": YES_NEEDED, "problems": [YES_NEEDED]}}, out)
            else:
                out.write(YES_NEEDED + "\n")
            return EXIT_CONFIG
        shown = _confirm_blocked(args, out, stdin)
        if shown is False:
            out.write("not approved\n")
            return EXIT_FAILED
        if not isinstance(shown, str):  # nothing shown: nothing a person could approve
            return _approve_answer(shown, args, out)
        if fingerprint is None:
            fingerprint = shown
    op_args: dict[str, object] = {"rule": args.rule}
    if fingerprint is not None:
        op_args["fingerprint"] = fingerprint
    if args.wait:
        op_args["wait"] = True
    try:
        result = opsclient.call(
            "approve",
            op_args,
            dry_run=args.dry_run,
            config_path=_config_path(args),
            state_dir=_state_dir(args),
        )
    except CalsyncError as exc:
        if not args.json:
            raise
        error: dict[str, object] = {"kind": _error_kind(exc), "message": str(exc)}
        if isinstance(exc, ConfigError):
            error["problems"] = exc.problems
        _dump({"error": error}, out)
        return exit_code_for(exc)
    return _approve_answer(result, args, out)


def _approve_answer(result: "OpResult", args: argparse.Namespace, out: TextIO) -> int:
    if args.json:
        _dump(result.to_json(), out)
    else:
        out.write(result.summary + "\n")
    return APPROVE_EXIT.get(str(result.data.get("status")), EXIT_FAILED)


def _confirm_blocked(
    args: argparse.Namespace, out: TextIO, stdin: TextIO
) -> "str | Literal[False] | OpResult":
    """Show the rule's blocked set and ask for the rule name. Its fingerprint once confirmed,
    False if not. Nothing is sent for a set the person was not shown: ``not_blocked`` (an
    ``OpResult``) when the rule is not blocked; ``ConfigError`` for an unknown rule or a blocked
    set that is no longer recorded (the operation's own answers)."""
    from calsync import ops
    from calsync.errors import ConfigError
    from calsync.views import StateReader, StateUnavailable, build_blocked, flat
    from calsync.wiring import state_paths

    config = _load(args)
    if all(r.name != args.rule for r in config.rules):
        raise ConfigError([f'unknown rule "{args.rule}"'])
    db_path, _ = state_paths(_state_dir(args))
    try:
        with StateReader(db_path) as reader:
            view = build_blocked(config, reader, args.rule)
    except StateUnavailable:
        return ops.not_blocked(args.rule)
    if not view.rules:
        return ops.not_blocked(args.rule)
    blocked = view.rules[0]
    if blocked.fingerprint is None:
        raise ops.unrecorded_block(args.rule)
    noun = "copy" if blocked.count == 1 else "copies"
    out.write(f"rule {blocked.rule}: {blocked.count} {noun} would be deleted:\n")
    for item in blocked.items[:PROMPT_TITLES]:
        out.write(f"  - {flat(item.title) if item.title else '(untitled)'}\n")
    if blocked.count > PROMPT_TITLES:
        out.write(f"  … and {blocked.count - PROMPT_TITLES} more (calsync blocked --rule {blocked.rule})\n")
    out.write(f"Type the rule name ({blocked.rule}) to approve: ")
    out.flush()
    if stdin.readline().strip() != blocked.rule:
        out.write("\n")
        return False
    return blocked.fingerprint


def cmd_notify_test(args: argparse.Namespace, out: TextIO) -> int:
    """`calsync notify test`: 0 when every configured channel took the push, 1 when one failed,
    2 when none is configured."""
    from calsync import opsclient
    from calsync.errors import CalsyncError, ConfigError

    try:
        result = opsclient.call(
            "notify.test", {}, dry_run=False, config_path=_config_path(args), state_dir=_state_dir(args)
        )
    except CalsyncError as exc:
        if not args.json:
            raise
        error: dict[str, object] = {"kind": _error_kind(exc), "message": str(exc)}
        if isinstance(exc, ConfigError):
            error["problems"] = exc.problems
        _dump({"error": error}, out)
        return exit_code_for(exc)
    channels: dict[str, str] = result.data.get("channels") or {}
    outcomes = list(channels.values())
    if all(o == "not configured" for o in outcomes):
        code = EXIT_CONFIG
    elif any(o.startswith("failed") for o in outcomes):
        code = EXIT_FAILED
    else:
        code = EXIT_OK
    if args.json:
        _dump(result.to_json(), out)
        return code
    for channel, outcome in channels.items():
        out.write(f"{channel}: {outcome}\n")
    if code == EXIT_CONFIG:
        out.write("no channel is configured\n")
    return code


def cmd_config_get(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.views import config_json

    _dump(config_json(_load(args), reveal=args.reveal), out)
    return EXIT_OK


def cmd_config_history(args: argparse.Namespace, out: TextIO) -> int:
    """Recorded config changes. Never their ``before`` text: a whole config, URLs included."""
    from calsync.views import StateReader, StateUnavailable, config_history_json
    from calsync.wiring import state_paths

    db_path, _ = state_paths(_state_dir(args))
    try:
        with StateReader(db_path) as reader:
            changes = reader.config_changes(args.limit)
    except StateUnavailable:
        changes = []
    if args.json:
        _dump(config_history_json(changes), out)
        return EXIT_OK
    if not changes:
        out.write("no config changes recorded\n")
    for c in changes:
        undoes = f" (undoes {c.undoes})" if c.undoes is not None else ""
        out.write(f"{c.id} {c.at.isoformat()} {c.source} {c.summary}{undoes}\n")
    return EXIT_OK


def cmd_config(args: argparse.Namespace, out: TextIO) -> int:
    if args.config_command == "get":
        return cmd_config_get(args, out)
    if args.config_command == "history":
        return cmd_config_history(args, out)
    if args.config_command in ("set", "undo"):
        return _ops_command(args, out, *_op_for(args))
    return cmd_config_check(args, out)


def _cached_calendars(state_dir: Path, account: str) -> list[tuple[str, str, str | None, datetime]]:
    """The account's cached calendar list: (id, name, role, fetched_at) rows (none cached: [])."""
    from calsync.views import StateReader, StateUnavailable
    from calsync.wiring import state_paths

    try:
        with StateReader(state_paths(state_dir)[0]) as reader:
            return [(c.id, c.name, c.role, c.fetched_at) for c in reader.account_calendars(account)]
    except StateUnavailable:
        return []


def cmd_calendars(args: argparse.Namespace, out: TextIO) -> int:
    """`calsync calendars ACCOUNT`: the calendar list from the cache, fetched first with
    ``--refresh`` or when nothing is cached (``calendars.refresh``: through the daemon if it
    runs, else here with the run lock)."""
    from calsync import opsclient
    from calsync.errors import CalsyncError

    config = _load(args)
    account = args.account
    if account not in config.accounts:
        message = f"unknown account {account!r}"
        if args.json:
            _dump({"error": {"kind": "config", "message": message, "problems": [message]}}, out)
        else:
            out.write(message + "\n")
        return EXIT_CONFIG
    state = _state_dir(args)
    rows = _cached_calendars(state, account)
    fetched: str | None = rows[0][3].isoformat() if rows else None
    if args.refresh or not rows:
        try:
            result = opsclient.call(
                "calendars.refresh",
                {"account": account},
                dry_run=False,
                config_path=_config_path(args),
                state_dir=state,
            )
        except CalsyncError as exc:
            if not args.json:
                raise
            _dump({"error": {"kind": _error_kind(exc), "message": str(exc)}}, out)
            return exit_code_for(exc)
        outcome = result.data.get("accounts", {}).get(account, {})
        if not outcome.get("refreshed"):
            kind = outcome.get("error_kind") or "error"
            message = str(outcome.get("error") or result.summary)
            human = result.needs_human
            if args.json:
                error: dict[str, object] = {"kind": kind, "message": message}
                if human:
                    error.update(command=human["command"], reason=human["reason"])
                _dump({"error": error}, out)
            else:
                out.write(f"error: {message}\n")
                if human:
                    out.write(f"next: {human['command']}  ({human['reason']})\n")
            return {"auth": EXIT_AUTH, "config": EXIT_CONFIG}.get(kind, EXIT_FAILED)
        rows = _cached_calendars(state, account)
        fetched = rows[0][3].isoformat() if rows else outcome.get("fetched_at")
    if args.json:
        calendars = [{"id": cid, "name": name, "role": role} for cid, name, role, _ in rows]
        _dump({"account": account, "fetched_at": fetched, "calendars": calendars}, out)
        return EXIT_OK
    when = datetime.fromisoformat(fetched).astimezone().isoformat(timespec="seconds") if fetched else "never"
    out.write(f"fetched {when}\n")
    for cid, name, role, _ in rows:
        out.write(f"{cid}\t{name}\t{role or '-'}\n")
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


def _print_cycle(result: "CycleResult", out: TextIO, verbose: bool) -> None:
    from calsync.cycle import counts_line
    from calsync.views import flat

    for name, r in result.rules.items():
        line = counts_line(name, r)
        if r.error:
            line += f" error={r.error!r}"
        out.write(line + "\n")
        if r.setup_detail:  # what to fix in the account's setup (a local path or mode)
            out.write(f"  {flat(r.setup_detail)}\n")
        # Titles are listed in plan.delete order only when every one was read.
        titled = r.status == "blocked" and r.blocked_titles_complete
        if r.status == "blocked":
            for title in r.blocked_titles:
                out.write(f"  - {flat(title)}\n")
            if not r.blocked_titles_complete:
                out.write("  (titles incomplete)\n")
            out.write("  re-run with --allow-deletes if this is intended\n")
        if verbose and r.plan is not None:
            for want in r.plan.create:
                out.write(f"  + {flat(want.series.title)} @ {want.series.start.value.isoformat()}\n")
            for want, _ in r.plan.update:
                out.write(f"  ~ {flat(want.series.title)} @ {want.series.start.value.isoformat()}\n")
            if not (titled and len(r.blocked_titles) == len(r.plan.delete)):
                for have in r.plan.delete:  # no title known: the href is all we have
                    out.write(f"  - {have.href}\n")
    out.write(f"requests={result.requests}\n")


async def _run(args: argparse.Namespace, out: TextIO) -> int:
    from calsync import notify
    from calsync.cycle import run_cycle
    from calsync.lock import RunLock
    from calsync.ops import send_contained
    from calsync.store import Store
    from calsync.wiring import accounts_in_use, build_adapters, build_engine, state_paths

    config = _load(args)
    unknown = [r for r in args.rules or [] if r not in {rule.name for rule in config.rules}]
    if unknown:
        out.write(f"unknown rule(s): {', '.join(unknown)}\n")
        return EXIT_CONFIG
    chosen = [r for r in config.rules if not args.rules or r.name in args.rules]
    for r in chosen:
        if r.paused:  # not run, and fine for the exit code
            out.write(f"rule {r.name}: paused\n")
    active = [r.name for r in chosen if not r.paused]
    if chosen and not active:  # every chosen rule is paused: nothing to run
        return EXIT_OK
    db_path, lock_path = state_paths(_state_dir(args))
    removed: list[str] = []
    alerts: list[notify.Alert] = []
    with RunLock(lock_path):
        adapters = await build_adapters(config, accounts_in_use(config, active))
        try:
            store = Store(db_path)
            try:
                result = await run_cycle(
                    build_engine(config, adapters, store),
                    store,
                    config,
                    adapters,
                    active,
                    dry_run=args.dry_run,
                    allow_deletes=args.allow_deletes,
                )
                if not args.dry_run:  # a dry run records nothing, so it alerts nothing
                    alerts = notify.evaluate(result.rules, store=store, config=config, now=_now())
                    removed = _remove_retired(args, config, result, store)
            finally:
                store.close()
        finally:
            await adapters.aclose()
    _print_cycle(result, out, verbose=args.dry_run)
    for summary in removed:
        out.write(summary + "\n")
    out.flush()  # the result first: a slow channel delays only the exit
    await send_contained(config, alerts)  # each push bounded; a failure only logged
    return exit_code_for_results(result.rules.values())


def _remove_retired(
    args: argparse.Namespace, config: "Config", result: "CycleResult", store: "Store"
) -> list[str]:
    """Remove the retiring rules whose copies are all gone, as the daemon would (this process
    holds the lock); the summaries of the removals that succeeded."""
    from calsync.cycle import remove_retired, retired
    from calsync.ops import OpContext

    ctx = OpContext(
        config_path=_config_path(args), state_dir=_state_dir(args), store=store, source="daemon", clock=_now
    )
    return [done.summary for name in retired(config, result, store) if (done := remove_retired(ctx, name))]


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
    daemon = Daemon(config, _state_dir(args), sync=sync, listen=listen, config_path=_config_path(args))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def cycle_now() -> None:
        from calsync.ops import OpsUnavailable

        with contextlib.suppress(OpsUnavailable):  # the scheduler has stopped: nothing to run it
            daemon.request_cycle()

    handlers = {signal.SIGTERM: stop.set, signal.SIGINT: stop.set, signal.SIGUSR1: cycle_now}
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


def cmd_trigger(args: argparse.Namespace, out: TextIO) -> int:
    """A cycle now. With a daemon: requested from it (``--wait``: the ``trigger`` operation over
    its socket, answered with that cycle's result). Without one: run here, holding the lock,
    as ``calsync run [--rule R]`` would, with the same result as ``--wait``."""
    from calsync import opsclient
    from calsync.errors import CalsyncError, ConfigError, RulePaused
    from calsync.wiring import socket_path

    state_dir = _state_dir(args)
    op_args: dict[str, object] = {"wait": True, "timeout": args.timeout} if args.wait else {}
    if args.rule:
        op_args["rule"] = args.rule
    try:
        if args.dry_run:  # it writes no config: refused, like the operation
            from calsync.ops import TRIGGER_NO_DRY_RUN

            raise ConfigError([TRIGGER_NO_DRY_RUN])
        listens = opsclient.daemon_listens(socket_path(state_dir))  # a socket it cannot reach raises
        if listens and not args.wait:
            return asyncio.run(_trigger(args, out))
        if listens:
            result = opsclient.call(
                "trigger",
                op_args,
                dry_run=False,
                config_path=_config_path(args),
                state_dir=state_dir,
                read_timeout=args.timeout + opsclient.TRIGGER_MARGIN,
            )
        else:
            _load(args)  # a config problem stops it before the state directory is touched
            result = opsclient.run_in_process(
                "trigger", op_args, dry_run=False, config_path=_config_path(args), state_dir=state_dir
            )
    except RulePaused as exc:
        return _refuse(args, out, "paused", str(exc), EXIT_FAILED)
    except CalsyncError as exc:
        if not args.json:
            raise
        error: dict[str, object] = {"kind": _error_kind(exc), "message": str(exc)}
        if isinstance(exc, ConfigError):
            error["problems"] = exc.problems
        _dump({"error": error}, out)
        return exit_code_for(exc)
    code = _trigger_exit(result.data)
    if args.json:
        _dump(result.to_json(), out)
        return code
    out.write(result.summary + "\n")
    rules: dict[str, dict[str, object]] = result.data.get("rules") or {}
    for name, r in rules.items():
        line = " ".join(f"{key}={r[key]}" for key in ("status", "created", "updated", "deleted", "failed"))
        out.write(
            f"rule={name} {line}" + (f" error_kind={r['error_kind']}" if r.get("error_kind") else "") + "\n"
        )
    if "requests" in result.data:
        out.write(f"requests={result.data['requests']}\n")
    return code


def _trigger_exit(data: dict[str, object]) -> int:
    """``trigger``'s exit code: requested 0; ran: as ``calsync run`` (auth 3 > config 2 > any
    rule not ok 1); every rule paused 0 (nothing to run); a timeout or a failed cycle 1."""
    if data.get("accepted"):
        return EXIT_OK
    if not data.get("ran"):
        return EXIT_OK if data.get("reason") == "paused" else EXIT_FAILED
    rules: Any = data.get("rules") or {}
    kinds = {r.get("error_kind") for r in rules.values()}
    if "auth" in kinds:
        return EXIT_AUTH
    if "config" in kinds:
        return EXIT_CONFIG
    return EXIT_OK if all(r.get("status") == "ok" for r in rules.values()) else EXIT_FAILED


async def _trigger(args: argparse.Namespace, out: TextIO) -> int:
    """Ask the daemon, over its socket, for a cycle now (``POST /api/trigger``)."""
    import httpx

    from calsync.opsclient import root_os_error
    from calsync.wiring import socket_path

    transport = httpx.AsyncHTTPTransport(uds=str(socket_path(_state_dir(args))))
    body = {"rule": args.rule} if args.rule else {}
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://calsync", timeout=10) as client:
            response = await client.post("/api/trigger", json=body)
    except httpx.ConnectError as exc:
        cause = root_os_error(exc)
        if cause is None or isinstance(cause, FileNotFoundError | ConnectionRefusedError):
            # No socket file, or one nobody listens on (left by a daemon that died).
            out.write("the daemon is not running; use calsync run\n")
        else:  # a path too long for a socket, no permission, …
            out.write(f"error: cannot connect to the daemon socket ({type(cause).__name__})\n")
        return EXIT_FAILED
    if response.status_code == 202:
        if args.json:
            from calsync.ops import OpResult

            _dump(
                OpResult(
                    "trigger", False, "cycle requested", data={"accepted": True, "rule": args.rule}
                ).to_json(),
                out,
            )
        else:
            out.write("ok: cycle requested\n")
        return EXIT_OK
    error = _error_body(response)
    if response.status_code == 409:
        if error.get("kind") == "paused":
            return _refuse(
                args, out, "paused", str(error.get("message") or "the rule is paused"), EXIT_FAILED
            )
        return _refuse(args, out, "read_only", "the daemon runs with --no-sync", EXIT_FAILED)
    if response.status_code == 404:
        return _refuse(args, out, "unknown_rule", f"unknown rule(s): {args.rule}", EXIT_CONFIG)
    if response.status_code == 503:
        return _refuse(args, out, "unavailable", "the daemon is stopping", EXIT_FAILED)
    return _refuse(args, out, "error", f"error: the daemon answered HTTP {response.status_code}", EXIT_FAILED)


def _error_body(response: "httpx.Response") -> dict[str, object]:
    """The API's ``{"error": {...}}`` object; empty when the body is anything else."""
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


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


# Evaluated lazily (the classes are imported for type checking only).
type View = StatusView | UpcomingView | RunsView | ChangesView | BlockedView | EventsView | EventDetail
ViewBuilder = Callable[["Config", "StateReader"], View]


def _show(
    args: argparse.Namespace,
    out: TextIO,
    build: ViewBuilder,
    *,
    check_rule: bool = True,
    extra: dict[str, object] | None = None,
) -> int:
    """Build a read-only view from the state database and print it (as JSON with ``--json``,
    with ``extra`` keys added)."""
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
        out.write(json.dumps({**view.to_json(), **(extra or {})}, ensure_ascii=False, indent=2) + "\n")
    else:
        _print_view(view, out)
    return EXIT_OK


def _span_text(start: "When", end: "When") -> str:
    """``2026-10-24 18:00–18:15`` in local time, ``2026-10-28 22:00 – 2026-10-29 01:00`` over
    midnight; ``2026-11-05 all day``, ``2026-11-05 – 2026-11-07 all day`` (the last day shown,
    not the exclusive end)."""
    if start.all_day:
        first = start.value
        last = end.value - timedelta(days=1) if isinstance(end.value, date) and end.all_day else first
        if last > first:
            return f"{first.isoformat()} – {last.isoformat()} all day"
        return f"{first.isoformat()} all day"
    lo, hi = start.utc().astimezone(), end.utc().astimezone()
    if hi.date() != lo.date():
        return f"{lo.strftime('%Y-%m-%d %H:%M')} – {hi.strftime('%Y-%m-%d %H:%M')}"
    return f"{lo.strftime('%Y-%m-%d %H:%M')}–{hi.strftime('%H:%M')}"


def _print_event(view: "EventDetail", out: TextIO) -> None:
    from calsync.views import flat

    event = view.event
    out.write(f"{flat(event.instance.title)}\n")
    out.write(f"  when: {_span_text(event.instance.start, event.instance.end)}\n")
    out.write(f"  source: {event.source['account']} / {event.source['calendar_name']}\n")
    for d in event.deliveries:
        at = f" at {d.delivered_at.isoformat()}" if d.delivered_at else ""
        out.write(f"  {d.rule} → {d.target['account']} / {d.target['calendar_name']}: {d.state}{at}\n")
        if d.differs:
            out.write(f"    differs: {', '.join(d.differs)}\n")


def _print_view(view: View, out: TextIO) -> None:
    from calsync.events_view import EventDetail, EventsView
    from calsync.views import BlockedView, ChangesView, RunsView, StatusView, UpcomingView, flat

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
                f"{when}  {flat(event.instance.title)}  [{event.target_calendar}]  alarms={alarms}  "
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
    elif isinstance(view, BlockedView):
        if not view.rules:
            out.write("no rule is blocked\n")
        for blocked in view.rules:
            fingerprint = blocked.fingerprint or "unknown"
            out.write(
                f"rule={blocked.rule} since={blocked.since.isoformat()} count={blocked.count} "
                f"fingerprint={fingerprint}\n"
            )
            for item in blocked.items:
                out.write(f"  - {flat(item.title) if item.title else '(untitled)'}\n")
            out.write(f"  approve with: calsync approve {blocked.rule}\n")
    elif isinstance(view, EventsView):
        if not view.events:
            out.write("no events\n")
        for event in view.events:
            deliveries = ", ".join(f"{d.target['calendar_name']}: {d.state}" for d in event.deliveries)
            out.write(
                f"{_span_text(event.instance.start, event.instance.end)}  {flat(event.instance.title)}  "
                f"[{event.source['calendar_name']} → {deliveries}]\n"
            )
        if view.events:
            out.write("(use --json for event ids)\n")
        if view.truncated:
            from calsync.model import EXPANSION_LIMIT

            out.write(
                f"note: a recurring event has more instances than shown (at most {EXPANSION_LIMIT} "
                "per event); narrow the range\n"
            )
    elif isinstance(view, EventDetail):
        _print_event(view, out)
    elif isinstance(view, ChangesView):
        for change in view.changes:
            title = flat(change.title) if change.title else "(untitled)"
            out.write(
                f"{change.at.isoformat()} rule={change.rule} {change.action} {title} object={change.object_id}\n"
            )


def cmd_status(args: argparse.Namespace, out: TextIO) -> int:
    """The status; with ``--json`` also the running daemon's block (asked over its socket;
    ``null`` when no daemon runs)."""
    from calsync.opsclient import daemon_status
    from calsync.views import build_status

    daemon = daemon_status(_state_dir(args))
    code = _show(
        args, out, lambda config, reader: build_status(config, reader, _now()), extra={"daemon": daemon}
    )
    if code == EXIT_OK and not args.json and daemon is not None:
        out.write(
            f"daemon: running, {daemon.get('cycles')} cycles, next at {daemon.get('next_cycle_at') or '-'}\n"
        )
    return code


def _refuse(args: argparse.Namespace, out: TextIO, kind: str, message: str, code: int) -> int:
    """A refused read: the message, or ``{"error": ...}`` with ``--json``."""
    if args.json:
        _dump({"error": {"kind": kind, "message": message}}, out)
    else:
        out.write(message + "\n")
    return code


def cmd_events(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.events_view import build_events, event_range, known_target, parse_target

    now = _now()
    try:
        start, end = event_range(args.start, args.end, now)
        target = parse_target(args.target) if args.target is not None else None
    except (ValueError, OverflowError) as exc:
        return _refuse(args, out, "bad_request", str(exc), EXIT_CONFIG)
    config = _load(args)
    if args.rule is not None and args.rule not in {r.name for r in config.rules}:
        return _refuse(args, out, "unknown_rule", f"unknown rule(s): {args.rule}", EXIT_CONFIG)
    if target is not None and not known_target(config, target):
        return _refuse(args, out, "unknown_target", f"no rule syncs into {args.target}", EXIT_CONFIG)
    return _show(
        args,
        out,
        lambda config, reader: build_events(
            config, reader, start, end, rule=args.rule, target=target, query=args.query or None, now=now
        ),
    )


def cmd_event(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.events_view import NotFound, build_event

    try:
        return _show(args, out, lambda config, reader: build_event(config, reader, args.id, _now()))
    except NotFound:
        return _refuse(args, out, "not_found", "no such event", EXIT_FAILED)


def cmd_upcoming(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.views import build_upcoming

    now = _now()
    try:
        # Only whether it is representable, in the local zone too (the view shows local times).
        _ = (now + timedelta(hours=args.hours)).astimezone()
    except OverflowError:
        return _refuse(args, out, "bad_request", "hours: too far ahead", EXIT_CONFIG)
    return _show(
        args,
        out,
        lambda config, reader: build_upcoming(config, reader, now, hours=args.hours, rule=args.rule),
    )


def cmd_runs(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.views import build_runs

    return _show(
        args,
        out,
        lambda config, reader: build_runs(config, reader, rule=args.rule, limit=args.limit),
        check_rule=False,
    )


def cmd_blocked(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.views import build_blocked

    return _show(args, out, lambda config, reader: build_blocked(config, reader, args.rule))


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
            return cmd_config(args, out)
        if args.command == "approve":
            return cmd_approve(args, out, stdin)
        if args.command == "blocked":
            return cmd_blocked(args, out)
        if args.command in ("rule", "pause", "resume", "account"):
            return _ops_command(args, out, *_op_for(args))
        if args.command == "calendars":
            return cmd_calendars(args, out)
        if args.command == "auth":
            return asyncio.run(_auth(args, out, stdin))
        if args.command == "notify":
            return cmd_notify_test(args, out)
        if args.command == "run":
            return asyncio.run(_run(args, out))
        if args.command == "daemon":
            return asyncio.run(_daemon(args, out))
        if args.command == "trigger":
            return cmd_trigger(args, out)
        if args.command == "web":
            return cmd_web(args, out)
        if args.command == "status":
            return cmd_status(args, out)
        if args.command == "upcoming":
            return cmd_upcoming(args, out)
        if args.command == "events":
            return cmd_events(args, out)
        if args.command == "event":
            return cmd_event(args, out)
        if args.command == "runs":
            return cmd_runs(args, out)
        if args.command == "changes":
            return cmd_changes(args, out)
    except CalsyncError as exc:
        out.write(_describe(exc, args))
        return exit_code_for(exc)
    except Exception as exc:
        # The network, the state database or the filesystem failing outside an adapter:
        # a message, not a traceback. Imported here so a normal start stays fast.
        import sqlite3

        import httpx

        if isinstance(exc, httpx.HTTPError | sqlite3.Error | OSError):
            out.write(_describe(exc, args))
            return exit_code_for(exc)
        from calsync import debug

        if args.command != "daemon" or debug.enabled():
            raise  # a bug: the traceback helps, except in the daemon's log
        # The daemon's output is its log (under launchd, a file): the type only, as for its other
        # failures, since the text can carry a URL. CALSYNC_DEBUG=1 lets the traceback through.
        out.write(f"error: {type(exc).__name__}\n")
        return EXIT_FAILED
    _parser().print_help(out)
    return EXIT_CONFIG
