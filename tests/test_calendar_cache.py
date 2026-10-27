"""The calendar-list cache (``account_calendar``): refreshed by the daemon at start and every 6 h
and by the ``calendars.refresh`` operation; read by ``calsync calendars`` (and the GUI's map)."""

import asyncio
import io
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from calsync import ops, wiring
from calsync.cli import EXIT_AUTH, EXIT_CONFIG, EXIT_OK, main
from calsync.config import Config, RuntimeConfig
from calsync.errors import AuthError, CalsyncError, TransientError
from calsync.http import RequestLog, make_client
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import StaticToken
from calsync.store import Store
from calsync.views import StateReader
from tests.fakes.google import BASE_URL, FakeGoogle
from tests.fakes.target import FakeTarget
from tests.harness import GCAL, NOW, config_for, rule
from tests.test_daemon import World, eventually

pytestmark = pytest.mark.anyio


class Accounts:
    """A FakeGoogle account ``me`` and a FakeTarget account ``dav``, handed out by ``build``."""

    def __init__(self) -> None:
        self.google = FakeGoogle()
        self.google.add_calendar(GCAL, "Family")
        self.google.add_calendar("shared@group.calendar.google.com", "Shared", access_role="reader")
        self.target = FakeTarget()
        self.target.add_calendar("Home")
        self.builds: list[set[str]] = []
        self.setup_errors: dict[str, CalsyncError] = {}

    async def build(self, config: Config, names: set[str], **_: Any) -> wiring.Adapters:
        self.builds.append(set(names))
        adapters = wiring.Adapters(RequestLog())
        for name in sorted(names):
            if name in self.setup_errors:
                adapters.errors[name] = self.setup_errors[name]
            elif config.accounts[name].provider == "caldav":
                adapters.targets[name] = self.target
            else:
                client = make_client(adapters.log, transport=self.google.transport())
                adapters.clients.append(client)
                adapters.sources[name] = GoogleSource(client, StaticToken("t"), base_url=BASE_URL)
        return adapters


def cached(state_dir: Path, account: str) -> list[tuple[str, str, str | None]]:
    with StateReader(state_dir / "state.db") as reader:
        return [(c.id, c.name, c.role) for c in reader.account_calendars(account)]


# ----- the adapters --------------------------------------------------------------------------


async def test_google_calendar_list_carries_the_access_role() -> None:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.add_calendar("shared@group.calendar.google.com", "Shared", access_role="reader")
    async with make_client(RequestLog(), transport=fake.transport()) as client:
        found = await GoogleSource(client, StaticToken("t"), base_url=BASE_URL).list_calendars()
    assert {(c.id, c.role) for c in found} == {
        (GCAL, "owner"),
        ("shared@group.calendar.google.com", "reader"),
    }


async def test_the_calendar_list_request_never_touches_the_sync_token_params() -> None:
    """``accessRole`` is asked for only in the plain listing: the cycle's calendarList keeps the
    parameters its stored sync token was issued for."""
    from calsync.sources import google

    assert "accessRole" not in google.CALENDAR_LIST_PARAMS["fields"]
    assert "accessRole" in google.CALENDAR_LIST_ALL_PARAMS["fields"]


# ----- refresh_calendars ------------------------------------------------------------------


async def test_refresh_writes_rows_and_keeps_them_on_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    accounts = Accounts()
    config = config_for([rule()])
    adapters = await accounts.build(config, {"me", "dav"})
    with Store(tmp_path / "state.db") as store:
        done = await ops.refresh_calendars(store, adapters, ["dav", "me"], NOW)
        assert (done.counts, done.errors) == ({"dav": 1, "me": 2}, {})
        accounts.target.fail_discovery = TransientError("PROPFIND https://dav.example.com/home/ timed out")
        later = NOW + timedelta(hours=6)
        with caplog.at_level(logging.WARNING):
            done = await ops.refresh_calendars(store, adapters, ["dav", "me"], later)
    await adapters.aclose()
    assert set(done.errors) == {"dav"} and done.counts == {"me": 2}
    assert cached(tmp_path, "me") == [
        (GCAL, "Family", "owner"),
        ("shared@group.calendar.google.com", "Shared", "reader"),
    ]
    assert cached(tmp_path, "dav") == [("/dav/home/home/", "Home", None)]  # the old rows
    with StateReader(tmp_path / "state.db") as reader:
        assert {c.fetched_at for c in reader.account_calendars("dav")} == {NOW}
        assert {c.fetched_at for c in reader.account_calendars("me")} == {later}
    messages = [r.getMessage() for r in caplog.records]
    assert any("dav" in m and "TransientError" in m for m in messages)
    assert all("https://" not in m for m in messages)


# ----- the operation ------------------------------------------------------------------------


def op_context(tmp_path: Path, store: Store, config_path: Path) -> ops.OpContext:
    return ops.OpContext(
        config_path=config_path, state_dir=tmp_path, store=store, source="cli", clock=lambda: NOW
    )


CONFIG = """\
[google]
client_secret_file = "unused.json"

[accounts.me]
provider = "google"
credential = { backend = "env", ref = "CALSYNC_UNUSED" }

[accounts.dav]
provider = "caldav"
server_url = "https://dav.example.com/"
username = "u"
credential = { backend = "env", ref = "CALSYNC_UNUSED" }

[[rules]]
name = "family"
source = { account = "me", calendar = "Family" }
target = { account = "dav", calendar = "Home" }
"""


@pytest.fixture
def accounts(monkeypatch: pytest.MonkeyPatch) -> Accounts:
    fake = Accounts()
    monkeypatch.setattr(wiring, "build_adapters", fake.build)
    return fake


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return path


def test_refresh_op_in_process(tmp_path: Path, accounts: Accounts, config_file: Path) -> None:
    with Store(tmp_path / "state.db") as store:
        result = ops.run_op(op_context(tmp_path, store, config_file), "calendars.refresh", {}, False)
    assert result.changed and result.data["accounts"]["me"] == {
        "refreshed": True,
        "calendars": 2,
        "fetched_at": NOW.isoformat(),
        "error": None,
        "error_kind": None,
    }
    assert accounts.builds == [{"me", "dav"}]
    assert len(cached(tmp_path, "dav")) == 1
    assert config_file.read_text() == CONFIG  # nothing in the config, no history row
    with StateReader(tmp_path / "state.db") as reader:
        assert reader.config_changes() == []


def test_refresh_op_reports_a_failing_account_without_raising(
    tmp_path: Path, accounts: Accounts, config_file: Path
) -> None:
    accounts.setup_errors["me"] = AuthError("credential file not found: /secret/place")
    with Store(tmp_path / "state.db") as store:
        result = ops.run_op(
            op_context(tmp_path, store, config_file), "calendars.refresh", {"account": "me"}, False
        )
    assert not result.changed
    assert result.data["accounts"]["me"]["error"] == 'AuthError: account "me" has no usable credential'
    assert result.data["accounts"]["me"]["error_kind"] == "auth"
    assert result.needs_human == {"command": "calsync auth me", "reason": ops.AUTH_REASON}


def test_refresh_op_dry_run_writes_nothing(tmp_path: Path, accounts: Accounts, config_file: Path) -> None:
    with Store(tmp_path / "state.db") as store:
        result = ops.run_op(
            op_context(tmp_path, store, config_file), "calendars.refresh", {"account": "dav"}, True
        )
    assert not result.changed and result.data["would_change"] is True
    assert accounts.builds == [] and cached(tmp_path, "dav") == []


def test_refresh_op_refuses_an_unknown_account(tmp_path: Path, accounts: Accounts, config_file: Path) -> None:
    from calsync.errors import ConfigError

    with Store(tmp_path / "state.db") as store, pytest.raises(ConfigError):
        ops.run_op(op_context(tmp_path, store, config_file), "calendars.refresh", {"account": "nope"}, False)


# ----- the daemon --------------------------------------------------------------------------


async def test_daemon_refreshes_at_start_and_every_six_hours(tmp_path: Path) -> None:
    config = replace(config_for([rule()]), runtime=RuntimeConfig(timedelta(hours=1), 0.0))
    world = World(stop_after=1)
    world.target.add_calendar("Family")
    await world.daemon(config, tmp_path).run(world.stop)
    assert [cid for cid, _, _ in cached(tmp_path, "me")] == [GCAL, "work@group.calendar.google.com"]
    assert cached(tmp_path, "dav") == [("/dav/home/family/", "Family", None)]
    with StateReader(tmp_path / "state.db") as reader:
        assert {c.fetched_at for c in reader.account_calendars("me")} == {NOW}

    # Hourly cycles for 8 hours: the list is fetched once more, when 6 hours have passed.
    world = World(stop_after=8)
    world.target.add_calendar("Family")
    world.target.add_calendar("Kids")
    fetched: list[datetime] = []
    real = ops.refresh_calendars

    async def spy(store: Store, adapters: wiring.Adapters, names: list[str], at: datetime) -> ops.Refreshed:
        fetched.append(at)
        return await real(store, adapters, names, at)

    ops_refresh = pytest.MonkeyPatch()
    ops_refresh.setattr(ops, "refresh_calendars", spy)
    try:
        await world.daemon(config, tmp_path).run(world.stop)
    finally:
        ops_refresh.undo()
    assert fetched == [NOW, NOW + timedelta(hours=6)]
    assert [name for _, name, _ in cached(tmp_path, "dav")] == ["Family", "Kids"]


async def test_daemon_refresh_failure_keeps_rows_and_logs_the_type(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = config_for([rule()])
    with Store(tmp_path / "state.db") as store:
        from calsync.store import AccountCalendar

        store.save_account_calendars("dav", [AccountCalendar("dav", "/dav/home/old/", "Old", None, NOW)], NOW)
    world = World(stop_after=1)
    world.target.fail_discovery = TransientError("PROPFIND https://dav.example.com/ timed out")
    daemon = world.daemon(config, tmp_path)
    with caplog.at_level(logging.WARNING):
        await daemon.run(world.stop)
    assert cached(tmp_path, "dav") == [("/dav/home/old/", "Old", None)]
    assert any("dav" in r.getMessage() and "TransientError" in r.getMessage() for r in caplog.records)
    assert all("https://" not in r.getMessage() for r in caplog.records)


async def test_daemon_refresh_op_uses_its_adapters_and_builds_the_unused_ones(tmp_path: Path) -> None:
    """An account no rule uses (just added) gets a temporary adapter, closed again."""
    config = config_for([rule()], extra_google=True)  # "broken": no rule uses it
    world = World(block=True)
    config_path = tmp_path / "config.toml"
    config_path.write_text("")  # the watcher's baseline; unchanged, so never applied
    daemon = world.daemon(config, tmp_path, config_path=config_path)
    task = asyncio.create_task(daemon.run(world.stop))
    try:
        await eventually(lambda: daemon.snapshot().cycles >= 1)
        builds = len(world.builds)
        result = await daemon.run_op("calendars.refresh", {"account": "broken"}, False, source="cli")
        assert result.data["accounts"]["broken"]["refreshed"] is True
        assert world.builds[builds:] == [{"broken"}]
        assert world.clients[-1].is_closed
        result = await daemon.run_op("calendars.refresh", {"account": "me"}, False, source="cli")
        assert result.data["accounts"]["me"]["refreshed"] is True
        assert world.builds[builds + 1 :] == []  # the daemon's own adapter
    finally:
        world.stop.set()
        await task


# ----- calsync calendars ---------------------------------------------------------------------


def run(config_file: Path, state: Path, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(
        ["--config", str(config_file), "--state-dir", str(state), *argv], out=out, stdin=io.StringIO("")
    )
    return code, out.getvalue()


def test_calendars_refreshes_an_empty_cache_once_then_reads_it(
    tmp_path: Path, accounts: Accounts, config_file: Path
) -> None:
    state = tmp_path / "s"
    code, text = run(config_file, state, "calendars", "me")
    assert code == EXIT_OK
    lines = text.splitlines()
    assert lines[0].startswith("fetched ")
    assert lines[1:] == [f"{GCAL}\tFamily\towner", "shared@group.calendar.google.com\tShared\treader"]
    assert accounts.builds == [{"me"}]
    assert run(config_file, state, "calendars", "me")[0] == EXIT_OK
    assert accounts.builds == [{"me"}]  # from the cache
    run(config_file, state, "calendars", "me", "--refresh")
    assert accounts.builds == [{"me"}, {"me"}]


def test_calendars_json(tmp_path: Path, accounts: Accounts, config_file: Path) -> None:
    code, text = run(config_file, tmp_path / "s", "calendars", "dav", "--json")
    assert code == EXIT_OK
    body = json.loads(text)
    assert body == {
        "account": "dav",
        "fetched_at": body["fetched_at"],
        "calendars": [{"id": "/dav/home/home/", "name": "Home", "role": None}],
    }
    assert datetime.fromisoformat(body["fetched_at"]).tzinfo is not None


def test_calendars_auth_failure_exits_3_with_the_next_command(
    tmp_path: Path, accounts: Accounts, config_file: Path
) -> None:
    accounts.setup_errors["me"] = AuthError("credential file not found: /secret/place")
    code, text = run(config_file, tmp_path / "s", "calendars", "me")
    assert code == EXIT_AUTH
    assert 'AuthError: account "me" has no usable credential' in text and "calsync auth me" in text
    code, text = run(config_file, tmp_path / "s", "calendars", "me", "--json")
    assert code == EXIT_AUTH and json.loads(text)["error"]["kind"] == "auth"


def test_calendars_unknown_account(tmp_path: Path, accounts: Accounts, config_file: Path) -> None:
    assert run(config_file, tmp_path / "s", "calendars", "nope")[0] == EXIT_CONFIG
