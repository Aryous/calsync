"""Command-line entry point. Heavy modules are imported inside each command
so ``calsync --version`` and ``config check`` start instantly."""

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from calsync import __version__

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
    return parser


def _load(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from calsync.config import default_config_path, load_config

    return load_config(args.config or default_config_path())


def _state_dir(args: argparse.Namespace) -> Path:
    from calsync.wiring import default_state_dir

    return args.state_dir or default_state_dir()


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
    adapters = build_adapters(config, {args.account})
    try:
        if args.account in adapters.sources:
            for cal in await adapters.sources[args.account].list_calendars():
                out.write(f"{cal.id}\t{cal.name}\n")
        else:
            for tcal in await adapters.targets[args.account].list_calendars():
                out.write(f"{tcal.href}\t{tcal.name}\t{tcal.mode}\n")
    finally:
        await adapters.aclose()
    return EXIT_OK


def _read_stdin_secret(prompt: str, stdin: TextIO) -> str:
    if stdin.isatty():
        return getpass.getpass(prompt)
    return stdin.read().strip()


async def _auth(args: argparse.Namespace, out: TextIO, stdin: TextIO) -> int:
    import json

    from calsync.credentials import write_secret
    from calsync.wiring import build_adapters

    config = _load(args)
    account = config.accounts.get(args.account)
    if account is None:
        out.write(f"unknown account {args.account!r}\n")
        return EXIT_CONFIG
    if account.provider == "google":
        if not args.refresh_token_stdin:
            out.write("google browser sign-in is not available yet; pass --refresh-token-stdin\n")
            return EXIT_CONFIG
        secret = json.dumps({"refresh_token": _read_stdin_secret("Refresh token: ", stdin)})
    else:
        secret = _read_stdin_secret("App-specific password: ", stdin)
    write_secret(account.credential, secret)
    adapters = build_adapters(config, {args.account})
    try:
        adapter = adapters.sources.get(args.account) or adapters.targets[args.account]
        found = await adapter.list_calendars()
    finally:
        await adapters.aclose()
    out.write(f"ok: credentials stored, {len(found)} calendars visible\n")
    return EXIT_OK


def _print_cycle(result, out: TextIO, verbose: bool) -> None:  # type: ignore[no-untyped-def]
    for name, r in result.rules.items():
        out.write(
            f"rule={name} status={r.status} created={r.created} updated={r.updated} "
            f"deleted={r.deleted} failed={r.failed}" + (f" error={r.error!r}" if r.error else "") + "\n"
        )
        if verbose and r.plan is not None:
            for want in r.plan.create:
                out.write(f"  + {want.series.title} @ {want.series.start.value.isoformat()}\n")
            for want, _ in r.plan.update:
                out.write(f"  ~ {want.series.title} @ {want.series.start.value.isoformat()}\n")
            for have in r.plan.delete:
                out.write(f"  - {have.href}\n")
    out.write(f"requests={result.requests}\n")


async def _run(args: argparse.Namespace, out: TextIO) -> int:
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
        adapters = build_adapters(config, accounts_in_use(config, args.rules))
        store = Store(db_path)
        try:
            engine = build_engine(config, adapters, store)
            result = await engine.cycle(
                rules=args.rules, dry_run=args.dry_run, allow_deletes=args.allow_deletes
            )
        finally:
            store.close()
            await adapters.aclose()
    _print_cycle(result, out, verbose=args.dry_run)
    if any(r.error_kind == "auth" for r in result.rules.values()):
        return EXIT_AUTH
    return EXIT_OK if result.ok else EXIT_FAILED


def main(argv: Sequence[str] | None = None, *, out: TextIO = sys.stdout, stdin: TextIO = sys.stdin) -> int:
    args = _parser().parse_args(argv)
    if args.version:
        out.write(f"calsync {__version__}\n")
        return EXIT_OK
    from calsync.errors import AuthError, ConfigError
    from calsync.lock import AlreadyRunning

    try:
        if args.command == "config":
            return cmd_config_check(args, out)
        if args.command == "calendars":
            return asyncio.run(_calendars(args, out))
        if args.command == "auth":
            return asyncio.run(_auth(args, out, stdin))
        if args.command == "run":
            return asyncio.run(_run(args, out))
    except ConfigError as exc:
        out.write("configuration error:\n" + "".join(f"  - {p}\n" for p in exc.problems))
        return EXIT_CONFIG
    except AuthError as exc:
        out.write(f"authentication error: {exc}\n")
        return EXIT_AUTH
    except AlreadyRunning as exc:
        out.write(f"{exc}\n")
        return EXIT_RUNNING
    _parser().print_help(out)
    return EXIT_CONFIG
