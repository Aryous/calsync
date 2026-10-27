"""Build adapters and the engine from configuration. The only place that
turns account definitions into live clients."""

import contextlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from calsync.config import Account, Config
from calsync.credentials import read_secret
from calsync.engine import Engine
from calsync.errors import CalsyncError, ConfigError
from calsync.http import RequestLog, make_client
from calsync.sources.base import Source
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import RefreshTokenProvider, load_client_secret, parse_refresh_token
from calsync.store import Store
from calsync.targets.base import Target
from calsync.targets.caldav import CalDavTarget


@dataclass
class Adapters:
    log: RequestLog
    sources: dict[str, Source] = field(default_factory=dict)
    targets: dict[str, Target] = field(default_factory=dict)
    clients: list[httpx.AsyncClient] = field(default_factory=list)
    # Accounts whose setup failed (e.g. unreadable secret); they have no adapter.
    errors: dict[str, CalsyncError] = field(default_factory=dict)

    def adapter(self, name: str) -> Source | Target:
        """The account's adapter, or its setup error raised."""
        if name in self.errors:
            raise self.errors[name]
        return self.sources.get(name) or self.targets[name]

    async def aclose(self) -> None:
        """Close every client, even if one close fails; re-raise the first failure."""
        first: Exception | None = None
        for client in self.clients:
            try:
                await client.aclose()
            except Exception as exc:
                first = first or exc
        if first is not None:
            raise first


async def _discard(adapters: Adapters) -> None:
    """Close a half-built set of adapters without masking the error that stopped it."""
    with contextlib.suppress(Exception):
        await adapters.aclose()


def _add_account(config: Config, account: Account, secret: str, adapters: Adapters) -> None:
    """Build one account's adapter from an explicit secret; never reads stored credentials."""
    if account.provider == "google":
        if config.google_client_secret_file is None:
            raise ConfigError(["google.client_secret_file: required for google accounts"])
        client_id, client_secret = load_client_secret(config.google_client_secret_file)
        refresh = parse_refresh_token(secret)
        client = make_client(adapters.log)
        adapters.clients.append(client)
        token = RefreshTokenProvider(
            client, client_id=client_id, client_secret=client_secret, refresh_token=refresh
        )
        adapters.sources[account.name] = GoogleSource(client, token)
        return
    assert account.server_url is not None and account.username is not None
    client = make_client(adapters.log, auth=httpx.BasicAuth(account.username, secret))
    adapters.clients.append(client)
    adapters.targets[account.name] = CalDavTarget(
        client, account.server_url, max_concurrency=account.max_concurrency
    )


async def build_adapters(
    config: Config,
    names: set[str],
    *,
    environ: Mapping[str, str] = os.environ,
    log: RequestLog | None = None,
) -> Adapters:
    """Adapters for ``names``. An account whose setup fails is recorded in ``errors`` and
    skipped, so one unreadable secret cannot stop the other accounts. ``log``: record requests
    there instead of in a new log (the daemon keeps one log across rebuilds)."""
    adapters = Adapters(log if log is not None else RequestLog())
    try:
        for name in sorted(names):
            account = config.accounts[name]
            try:
                _add_account(config, account, read_secret(account.credential, environ=environ), adapters)
            except CalsyncError as exc:
                adapters.errors[name] = exc
    except BaseException:
        await _discard(adapters)
        raise
    return adapters


async def adapters_with_secret(config: Config, name: str, secret: str) -> Adapters:
    """Adapters for one account built from a candidate secret (not yet stored); raises on failure."""
    adapters = Adapters(RequestLog())
    try:
        _add_account(config, config.accounts[name], secret, adapters)
    except BaseException:
        await _discard(adapters)
        raise
    return adapters


def accounts_in_use(config: Config, rule_names: list[str] | None = None) -> set[str]:
    rules = [r for r in config.rules if not rule_names or r.name in rule_names]
    return {r.source.account for r in rules} | {r.target.account for r in rules}


def build_engine(
    config: Config, adapters: Adapters, store: Store, *, clock: Callable[[], datetime] | None = None
) -> Engine:
    """The engine over ``adapters``' dicts (by reference: replacing an entry swaps an adapter)."""
    if clock is None:
        return Engine(config, store, adapters.sources, adapters.targets, request_log=adapters.log)
    return Engine(config, store, adapters.sources, adapters.targets, request_log=adapters.log, clock=clock)


def state_paths(state_dir: Path) -> tuple[Path, Path]:
    return state_dir / "state.db", state_dir / "run.lock"


def socket_path(state_dir: Path) -> Path:
    """The daemon's API socket."""
    return state_dir / "calsync.sock"
