"""Wire an Engine to FakeGoogle (in memory) and a real local Radicale server."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from calsync.config import Config, parse_config
from calsync.engine import Engine
from calsync.http import RequestLog, make_client
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import StaticToken
from calsync.store import Store
from calsync.targets.caldav import CalDavTarget
from tests.fakes.google import BASE_URL, FakeGoogle
from tests.fakes.target import FakeTarget

GCAL = "family@group.calendar.google.com"
NOW = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)


def config_for(rules: list[dict[str, Any]], *, extra_google: bool = False) -> Config:
    accounts: dict[str, Any] = {
        "me": {"provider": "google", "credential": {"backend": "env", "ref": "UNUSED"}},
        "dav": {
            "provider": "caldav",
            "server_url": "http://unused/",
            "username": "u",
            "credential": {"backend": "env", "ref": "UNUSED"},
        },
    }
    if extra_google:
        accounts["broken"] = {"provider": "google", "credential": {"backend": "env", "ref": "UNUSED"}}
    data = {"google": {"client_secret_file": "unused.json"}, "accounts": accounts, "rules": rules}
    return parse_config(data, base_dir=Path("."))


def rule(
    name: str = "family", *, source: str = "Family", target: str = "Family", **extra: Any
) -> dict[str, Any]:
    return {
        "name": name,
        "source": {"account": "me", "calendar": source},
        "target": {"account": "dav", "calendar": target, "create_if_missing": True},
        **extra,
    }


@dataclass
class Harness:
    fake: FakeGoogle
    log: RequestLog
    engine: Engine
    target: CalDavTarget
    store: Store


def build(
    tmp_path: Path,
    radicale_url: str,
    dav_user: str,
    fake: FakeGoogle,
    config: Config,
    *,
    broken_google: bool = False,
    clock: Any = lambda: NOW,
) -> Harness:
    log = RequestLog()
    google_client = make_client(log, transport=fake.transport())
    sources: dict[str, Any] = {"me": GoogleSource(google_client, StaticToken("t"), base_url=BASE_URL)}
    if broken_google:

        def deny(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "revoked"})

        broken = make_client(log, transport=httpx.MockTransport(deny))
        sources["broken"] = GoogleSource(broken, StaticToken("t"), base_url=BASE_URL)
    dav_client = make_client(log, auth=httpx.BasicAuth(dav_user, "pw"))
    target = CalDavTarget(dav_client, radicale_url, max_concurrency=2)
    store = Store(tmp_path / "state.db")
    engine = Engine(config, store, sources, {"dav": target}, request_log=log, clock=clock)
    return Harness(fake, log, engine, target, store)


@dataclass
class FakeHarness:
    fake: FakeGoogle
    log: RequestLog
    engine: Engine
    target: FakeTarget
    store: Store


def build_fake(
    tmp_path: Path,
    fake: FakeGoogle,
    config: Config,
    *,
    target: FakeTarget | None = None,
    clock: Any = lambda: NOW,
) -> FakeHarness:
    """Like ``build`` but with an in-memory FakeTarget instead of Radicale."""
    log = RequestLog()
    google_client = make_client(log, transport=fake.transport())
    sources: dict[str, Any] = {"me": GoogleSource(google_client, StaticToken("t"), base_url=BASE_URL)}
    target = target or FakeTarget()
    store = Store(tmp_path / "state.db")
    engine = Engine(config, store, sources, {"dav": target}, request_log=log, clock=clock)
    return FakeHarness(fake, log, engine, target, store)
