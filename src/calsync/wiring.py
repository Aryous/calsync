"""Build adapters and the engine from configuration. The only place that
turns account definitions into live clients."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from calsync.config import Account, Config
from calsync.credentials import read_secret
from calsync.engine import Engine
from calsync.errors import ConfigError
from calsync.http import RequestLog, make_client
from calsync.sources.base import Source
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import RefreshTokenProvider, load_client_secret, parse_refresh_token
from calsync.store import Store
from calsync.targets.base import Target
from calsync.targets.caldav import CalDavTarget


def default_state_dir(environ: Mapping[str, str] = os.environ) -> Path:
    explicit = environ.get("CALSYNC_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".local" / "state" / "calsync"


@dataclass
class Adapters:
    log: RequestLog
    sources: dict[str, Source] = field(default_factory=dict)
    targets: dict[str, Target] = field(default_factory=dict)
    clients: list[httpx.AsyncClient] = field(default_factory=list)

    async def aclose(self) -> None:
        for client in self.clients:
            await client.aclose()


def _google_source(
    config: Config, account: Account, client: httpx.AsyncClient, environ: Mapping[str, str]
) -> GoogleSource:
    if config.google_client_secret_file is None:
        raise ConfigError(["google.client_secret_file: required for google accounts"])
    client_id, client_secret = load_client_secret(config.google_client_secret_file)
    refresh = parse_refresh_token(read_secret(account.credential, environ=environ))
    token = RefreshTokenProvider(
        client, client_id=client_id, client_secret=client_secret, refresh_token=refresh
    )
    return GoogleSource(client, token)


def build_adapters(config: Config, names: set[str], *, environ: Mapping[str, str] = os.environ) -> Adapters:
    adapters = Adapters(RequestLog())
    google_client: httpx.AsyncClient | None = None
    for name in sorted(names):
        account = config.accounts[name]
        if account.provider == "google":
            if google_client is None:
                google_client = make_client(adapters.log)
                adapters.clients.append(google_client)
            adapters.sources[name] = _google_source(config, account, google_client, environ)
        else:
            assert account.server_url is not None and account.username is not None
            password = read_secret(account.credential, environ=environ)
            client = make_client(adapters.log, auth=httpx.BasicAuth(account.username, password))
            adapters.clients.append(client)
            adapters.targets[name] = CalDavTarget(
                client, account.server_url, max_concurrency=account.max_concurrency
            )
    return adapters


def accounts_in_use(config: Config, rule_names: list[str] | None = None) -> set[str]:
    rules = [r for r in config.rules if not rule_names or r.name in rule_names]
    return {r.source.account for r in rules} | {r.target.account for r in rules}


def build_engine(config: Config, adapters: Adapters, store: Store) -> Engine:
    return Engine(config, store, adapters.sources, adapters.targets, request_log=adapters.log)


def state_paths(state_dir: Path) -> tuple[Path, Path]:
    return state_dir / "state.db", state_dir / "run.lock"
