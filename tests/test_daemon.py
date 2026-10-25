"""`calsync daemon`: schedule, requests, backoff, auth rebuilds and --no-sync.

FakeGoogle and FakeTarget stand in for the accounts, built by an injected adapters factory.
Time is fake: ``World.sleep`` records each wait and advances ``World.clock`` by it, so a
schedule of many minutes runs instantly and every timestamp is exact.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from calsync import wiring
from calsync.config import Config, RuntimeConfig
from calsync.daemon import Daemon
from calsync.http import RequestLog, make_client
from calsync.lock import AlreadyRunning, RunLock
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import StaticToken
from calsync.views import StateReader
from calsync.wiring import Adapters
from tests.factories import google_event
from tests.fakes.google import BASE_URL, FakeGoogle
from tests.fakes.target import FakeTarget
from tests.harness import GCAL, NOW, config_for, rule

pytestmark = pytest.mark.anyio

WORK = "work@group.calendar.google.com"


def two_rules(*, interval: float = 30, jitter: float = 0.0, work_source: str = "Work") -> Config:
    """``family`` and ``work`` from the same Google account; a missing ``work_source`` fails."""
    config = config_for([rule("family"), rule("work", source=work_source, target="Work")])
    return replace(config, runtime=RuntimeConfig(timedelta(seconds=interval), jitter))


class World:
    """Fake time plus the fake accounts every adapters build hands out."""

    def __init__(self, *, stop_after: int | None = None, block: bool = False) -> None:
        self.now = NOW
        self.fake = FakeGoogle()
        self.fake.add_calendar(GCAL, "Family")
        self.fake.add_calendar(WORK, "Work")
        self.fake.put_event(
            GCAL, google_event("lunch", "Lunch", "2026-10-25T12:00:00+08:00", "2026-10-25T13:00:00+08:00")
        )
        self.fake.put_event(
            WORK, google_event("standup", "Standup", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00")
        )
        self.target = FakeTarget()
        self.stop = asyncio.Event()
        self.stop_after = stop_after  # set ``stop`` when the n-th wait starts
        self.block = block  # waits never time out: only a request or ``stop`` ends them
        self.delays: list[float] = []
        self.builds: list[set[str]] = []
        self.logs: list[RequestLog] = []
        self.clients: list[httpx.AsyncClient] = []
        self.deny: set[str] = set()  # google accounts built with a revoked credential, once
        self.on_google: Callable[[], None] | None = None  # called on the next Google request, once
        self.before_sleep: Callable[[], None] | None = None  # called as each wait starts
        self.after_sleep: Callable[[], None] | None = None  # called as each timed-out wait ends

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, delay: float) -> None:
        if self.before_sleep is not None:
            self.before_sleep()
        self.delays.append(delay)
        if self.stop_after is not None and len(self.delays) >= self.stop_after:
            self.stop.set()
        if self.block:
            await asyncio.Event().wait()  # cancelled by the daemon when a request or stop wins
        self.now += timedelta(seconds=delay)
        if self.after_sleep is not None:
            self.after_sleep()

    def _google(self, request: httpx.Request) -> httpx.Response:
        hook, self.on_google = self.on_google, None
        if hook is not None:
            hook()
        return self.fake.handler(request)

    async def factory(self, config: Config, names: set[str], *, log: RequestLog | None = None) -> Adapters:
        self.builds.append(set(names))
        adapters = Adapters(log or RequestLog())
        self.logs.append(adapters.log)
        for name in sorted(names):
            if config.accounts[name].provider == "caldav":
                adapters.targets[name] = self.target  # one server: its state outlives rebuilds
                continue
            if name in self.deny:
                self.deny.discard(name)
                transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "revoked"}))
            else:
                transport = httpx.MockTransport(self._google)
            client = make_client(adapters.log, transport=transport)
            adapters.clients.append(client)
            self.clients.append(client)
            adapters.sources[name] = GoogleSource(client, StaticToken("t"), base_url=BASE_URL)
        return adapters

    def daemon(self, config: Config, state_dir: Path, **kwargs: Any) -> Daemon:
        options: dict[str, Any] = {
            "clock": self.clock,
            "sleep": self.sleep,
            "rng": lambda: 0.5,
            "adapters_factory": self.factory,
            "serve": False,  # the socket tests below turn it on, in a short directory
        }
        return Daemon(config, state_dir, **{**options, **kwargs})


def runs_by_time(state_dir: Path) -> list[tuple[int, str]]:
    """(seconds after NOW, rule) of every recorded run, in the order recorded."""
    with StateReader(state_dir / "state.db") as reader:
        runs = reader.runs()  # newest first
    return [(int((r.started_at - NOW).total_seconds()), r.rule) for r in reversed(runs)]


async def eventually(condition: Callable[[], bool]) -> None:
    """Like ``until``, for conditions that need real time (threads, sockets)."""
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


def uds_client(state_dir: Path) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=str(state_dir / "calsync.sock"))
    return httpx.AsyncClient(transport=transport, base_url="http://calsync")


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(10_000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


# ----- schedule ---------------------------------------------------------------


async def test_first_cycle_runs_immediately(tmp_path: Path) -> None:
    world = World(stop_after=1)
    daemon = world.daemon(two_rules(), tmp_path)
    seen: list[int] = []
    world.before_sleep = lambda: seen.append(daemon.snapshot().cycles)
    await daemon.run(world.stop)
    assert seen == [1]  # the first wait starts only after a cycle
    assert runs_by_time(tmp_path) == [(0, "family"), (0, "work")]
    info = daemon.snapshot()
    assert info.cycles == 1 and info.last_cycle_at == NOW and info.started_at == NOW
    assert world.target.objects  # the cycle wrote the copies


@pytest.mark.parametrize(("rng", "expected"), [(0.0, 54.0), (1.0, 66.0), (0.5, 60.0)])
async def test_wait_is_the_interval_with_jitter(tmp_path: Path, rng: float, expected: float) -> None:
    world = World(stop_after=1)
    config = config_for([rule()])  # the default runtime: 60 s, jitter 0.1
    daemon = world.daemon(config, tmp_path, rng=lambda: rng)
    next_at: list[datetime | None] = []
    world.before_sleep = lambda: next_at.append(daemon.snapshot().next_cycle_at)
    await daemon.run(world.stop)
    (delay,) = world.delays
    assert 54 <= delay <= 66 and delay == pytest.approx(expected)
    assert next_at == [NOW + timedelta(seconds=delay)]


async def test_cycles_repeat_after_each_wait(tmp_path: Path) -> None:
    world = World(stop_after=3)
    daemon = world.daemon(two_rules(interval=30), tmp_path)
    await daemon.run(world.stop)
    assert world.delays == [30, 30, 30]
    assert [t for t, r in runs_by_time(tmp_path) if r == "family"] == [0, 30, 60]
    assert daemon.snapshot().cycles == 3


async def test_request_during_the_wait_starts_a_cycle_at_once(tmp_path: Path) -> None:
    world = World(block=True)
    daemon = world.daemon(two_rules(), tmp_path)
    task = asyncio.create_task(daemon.run(world.stop))
    await until(lambda: len(world.delays) == 1)
    assert daemon.snapshot().cycles == 1 and daemon.snapshot().next_cycle_at == NOW + timedelta(seconds=30)
    daemon.request_cycle()
    await until(lambda: len(world.delays) == 2)
    assert daemon.snapshot().cycles == 2 and world.now == NOW  # no time passed: the wait was cut short
    world.stop.set()  # also ends a wait
    await asyncio.wait_for(task, 5)
    assert daemon.snapshot().cycles == 2 and not daemon.snapshot().running


@pytest.mark.parametrize(
    ("requests", "follow_up"),
    [((None, None), ["family", "work"]), (("work", "work"), ["work"]), (("work", None), ["family", "work"])],
)
async def test_requests_during_a_cycle_collapse_into_one_follow_up(
    tmp_path: Path, requests: tuple[str | None, str | None], follow_up: list[str]
) -> None:
    world = World(stop_after=1)
    daemon = world.daemon(two_rules(), tmp_path)

    def during_cycle() -> None:
        for name in requests:
            daemon.request_cycle(name)

    world.on_google = during_cycle
    await daemon.run(world.stop)
    assert daemon.snapshot().cycles == 2 and len(world.delays) == 1  # one follow-up, then the wait
    assert runs_by_time(tmp_path) == [(0, "family"), (0, "work"), *((0, r) for r in follow_up)]


async def test_request_for_an_unknown_rule_is_refused(tmp_path: Path) -> None:
    daemon = World().daemon(two_rules(), tmp_path)
    with pytest.raises(ValueError, match="unknown rule"):
        daemon.request_cycle("nope")


# ----- backoff ----------------------------------------------------------------


async def test_failing_rule_backs_off_while_the_other_keeps_running(tmp_path: Path) -> None:
    world = World(stop_after=7)  # cycles at 0, 30, ..., 180 s
    daemon = world.daemon(two_rules(work_source="Missing"), tmp_path)
    await daemon.run(world.stop)
    runs = runs_by_time(tmp_path)
    assert [t for t, r in runs if r == "family"] == [0, 30, 60, 90, 120, 150, 180]
    # Failed at 0: skipped until 0 + 1 min. Failed again at 60: skipped until 60 + 2 min.
    assert [t for t, r in runs if r == "work"] == [0, 60, 180]
    with StateReader(tmp_path / "state.db") as reader:
        assert {r.status for r in reader.runs("work")} == {"failed"}


async def test_backoff_resets_after_an_ok_run(tmp_path: Path) -> None:
    world = World(stop_after=6)
    daemon = world.daemon(two_rules(work_source="Later"), tmp_path)

    def appear_at_60() -> None:  # the calendar appears just before the third cycle
        if world.now == NOW + timedelta(seconds=60):
            world.fake.add_calendar("later@group.calendar.google.com", "Later")

    world.after_sleep = appear_at_60
    await daemon.run(world.stop)
    assert [t for t, r in runs_by_time(tmp_path) if r == "work"] == [0, 60, 90, 120, 150]


async def test_explicit_request_ignores_the_backoff(tmp_path: Path) -> None:
    world = World(block=True)
    daemon = world.daemon(two_rules(work_source="Missing"), tmp_path)
    task = asyncio.create_task(daemon.run(world.stop))
    await until(lambda: len(world.delays) == 1)
    daemon.request_cycle("work")  # backed off until 60 s, but asked for by name
    await until(lambda: len(world.delays) == 2)
    assert runs_by_time(tmp_path) == [(0, "family"), (0, "work"), (0, "work")]
    daemon.request_cycle()  # all rules: also ignores the backoff
    await until(lambda: len(world.delays) == 3)
    assert runs_by_time(tmp_path)[3:] == [(0, "family"), (0, "work")]
    world.stop.set()
    await asyncio.wait_for(task, 5)


async def test_blocked_rule_is_not_backed_off(tmp_path: Path) -> None:
    world = World(stop_after=3)
    config = replace(config_for([rule()]), runtime=RuntimeConfig(timedelta(seconds=30), 0.0))
    for i in range(3):
        world.fake.put_event(
            GCAL,
            google_event(f"e{i}", f"Standup {i}", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00"),
        )

    def cancel_all() -> None:  # after the first cycle every event goes: the guard blocks
        if not world.delays:
            for event in ("lunch", "e0", "e1", "e2"):
                world.fake.cancel_event(GCAL, event)

    world.before_sleep = cancel_all
    daemon = world.daemon(config, tmp_path)
    await daemon.run(world.stop)
    with StateReader(tmp_path / "state.db") as reader:
        statuses = [(int((r.started_at - NOW).total_seconds()), r.status) for r in reader.runs()]
    assert sorted(statuses) == [(0, "ok"), (30, "blocked"), (60, "blocked")]


# ----- auth -------------------------------------------------------------------


async def test_auth_failure_rebuilds_that_rules_accounts(tmp_path: Path) -> None:
    other = {**rule("other", target="Other"), "source": {"account": "broken", "calendar": "Family"}}
    config = replace(
        config_for([rule("family"), other], extra_google=True),
        runtime=RuntimeConfig(timedelta(seconds=30), 0.0),
    )
    world = World(stop_after=4)
    world.deny.add("broken")  # revoked at start; `calsync auth` fixes it meanwhile
    daemon = world.daemon(config, tmp_path)
    await daemon.run(world.stop)
    # Built once per account at start; rebuilt (source and target of the rule) before the
    # rule's next attempt at 60 s, after its 1 minute backoff.
    assert world.builds == [{"broken"}, {"dav"}, {"me"}, {"broken"}, {"dav"}]
    with StateReader(tmp_path / "state.db") as reader:
        other_runs = sorted(reader.runs("other"), key=lambda r: r.started_at)
        family_runs = reader.runs("family")
    assert [(int((r.started_at - NOW).total_seconds()), r.status, r.error_kind) for r in other_runs] == [
        (0, "failed", "auth"),
        (60, "ok", None),
        (90, "ok", None),
    ]
    assert len(family_runs) == 4 and {r.status for r in family_runs} == {"ok"}
    assert all(c.is_closed for c in world.clients)  # replaced and final clients all closed


async def test_unreadable_account_is_recorded_failed_every_cycle(tmp_path: Path) -> None:
    async def factory(config: Config, names: set[str], *, log: RequestLog | None = None) -> Adapters:
        adapters = await world.factory(config, names, log=log)
        if "broken" in names:
            from calsync.errors import AuthError

            adapters.sources.pop("broken")
            adapters.errors["broken"] = AuthError("credential file not found: /tmp/example")
        return adapters

    other = {**rule("other", target="Other"), "source": {"account": "broken", "calendar": "Family"}}
    config = replace(
        config_for([rule("family"), other], extra_google=True),
        runtime=RuntimeConfig(timedelta(seconds=30), 0.0),
    )
    world = World(stop_after=3)
    daemon = world.daemon(config, tmp_path, adapters_factory=factory)
    await daemon.run(world.stop)
    with StateReader(tmp_path / "state.db") as reader:
        other_runs = reader.runs("other")
    assert sorted(int((r.started_at - NOW).total_seconds()) for r in other_runs) == [0, 60]
    assert {(r.status, r.error_kind) for r in other_runs} == {("failed", "auth")}
    assert other_runs[0].error is not None and "credential file not found" in other_runs[0].error


# ----- request log, logging ---------------------------------------------------


async def test_request_log_is_cleared_every_cycle(tmp_path: Path) -> None:
    world = World(stop_after=4)
    daemon = world.daemon(two_rules(), tmp_path)
    await daemon.run(world.stop)
    with StateReader(tmp_path / "state.db") as reader:
        family = sorted(reader.runs("family"), key=lambda r: r.started_at)
    idle = family[-1].requests
    assert idle > 0 and [r.requests for r in family[1:]] == [idle] * 3  # counts still per cycle
    shared = {id(log) for log in world.logs}
    assert len(shared) == 1  # every build logs to the daemon's one log
    assert len(world.logs[0].entries) == idle  # only the last cycle's requests are kept


async def test_one_info_line_per_cycle_without_titles(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="calsync.daemon")
    world = World(stop_after=2)
    daemon = world.daemon(two_rules(work_source="Missing"), tmp_path)
    await daemon.run(world.stop)
    lines = [
        r.getMessage() for r in caplog.records if r.name == "calsync.daemon" and r.levelno == logging.INFO
    ]
    assert len(lines) == 2
    assert lines[0].startswith("rule=family status=ok created=1 updated=0 deleted=0 failed=0; ")
    assert (
        "rule=work status=failed created=0 updated=0 deleted=0 failed=0 error_kind=config; requests="
        in lines[0]
    )
    assert lines[1].startswith("rule=family status=ok created=0 ") and "rule=work" not in lines[1]
    assert not any(t in line for line in lines for t in ("Lunch", "Standup", "Missing", "google.test"))


# ----- stop, lock, snapshot -----------------------------------------------------


async def test_stop_during_a_cycle_finishes_it_then_exits(tmp_path: Path) -> None:
    world = World()
    daemon = world.daemon(two_rules(), tmp_path)
    world.on_google = world.stop.set
    await asyncio.wait_for(daemon.run(world.stop), 5)
    assert world.delays == []  # no wait after the stop
    assert runs_by_time(tmp_path) == [(0, "family"), (0, "work")]  # the cycle completed
    assert len(world.target.objects) == 2
    assert all(c.is_closed for c in world.clients)
    with RunLock(tmp_path / "run.lock"):  # released
        pass
    assert not daemon.snapshot().running


async def test_stop_before_start_runs_no_cycle(tmp_path: Path) -> None:
    world = World()
    world.stop.set()
    daemon = world.daemon(two_rules(), tmp_path)
    await daemon.run(world.stop)
    assert daemon.snapshot().cycles == 0 and world.delays == []


async def test_lock_is_held_for_the_daemon_lifetime(tmp_path: Path) -> None:
    world = World(block=True)
    daemon = world.daemon(two_rules(), tmp_path)
    task = asyncio.create_task(daemon.run(world.stop))
    await until(lambda: len(world.delays) == 1)
    with pytest.raises(AlreadyRunning) as info, RunLock(tmp_path / "run.lock"):
        pass
    assert info.value.pid is not None
    world.stop.set()
    await asyncio.wait_for(task, 5)


async def test_held_lock_refuses_to_start(tmp_path: Path) -> None:
    world = World(stop_after=1)
    daemon = world.daemon(two_rules(), tmp_path)
    with RunLock(tmp_path / "run.lock"), pytest.raises(AlreadyRunning):
        await daemon.run(world.stop)
    assert world.builds == [] and not (tmp_path / "state.db").exists()


async def test_snapshot_json(tmp_path: Path) -> None:
    world = World(block=True)
    daemon = world.daemon(two_rules(), tmp_path)
    before = daemon.snapshot().to_json()
    assert before["running"] is False and before["cycles"] == 0 and before["last_cycle_at"] is None
    task = asyncio.create_task(daemon.run(world.stop))
    await until(lambda: len(world.delays) == 1)
    info = daemon.snapshot()
    assert info.to_json() == {
        "running": True,
        "sync": True,
        "pid": info.pid,
        "started_at": NOW.isoformat(),
        "last_cycle_at": NOW.isoformat(),
        "next_cycle_at": (NOW + timedelta(seconds=30)).isoformat(),
        "cycles": 1,
    }
    world.stop.set()
    await asyncio.wait_for(task, 5)


# ----- --no-sync ----------------------------------------------------------------


async def test_no_sync_builds_no_adapters_and_takes_no_lock(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*args: Any, **kwargs: Any) -> Adapters:
        raise AssertionError("build_adapters called")

    monkeypatch.setattr(wiring, "build_adapters", refuse)
    stop = asyncio.Event()
    state_dir = short_dir / "state"
    daemon = Daemon(two_rules(), state_dir, sync=False)
    task = asyncio.create_task(daemon.run(stop))
    await until(lambda: daemon.snapshot().running)
    assert daemon.request_cycle() is False  # nothing runs cycles
    info = daemon.snapshot()
    assert (info.sync, info.next_cycle_at, info.cycles) == (False, None, 0)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert not (state_dir / "run.lock").exists() and not (state_dir / "state.db").exists()
    assert not (state_dir / "calsync.sock").exists()


async def test_no_sync_runs_beside_a_running_sync_daemon(short_dir: Path) -> None:
    world = World(block=True)  # syncs without serving, as `calsync run` would
    task = asyncio.create_task(world.daemon(two_rules(), short_dir).run(world.stop))
    await until(lambda: len(world.delays) == 1)
    stop = asyncio.Event()
    viewer = Daemon(two_rules(), short_dir, sync=False)
    viewer_task = asyncio.create_task(viewer.run(stop))
    await eventually(lambda: (short_dir / "calsync.sock").is_socket())
    stop.set()
    world.stop.set()
    await asyncio.wait_for(asyncio.gather(task, viewer_task), 5)


async def test_a_failing_service_raises_its_error_and_releases_everything(tmp_path: Path) -> None:
    world = World()

    async def broken_sleep(delay: float) -> None:
        raise RuntimeError("clock broke")

    daemon = world.daemon(two_rules(), tmp_path, sleep=broken_sleep)
    with pytest.raises(RuntimeError, match="clock broke"):  # not an ExceptionGroup: main reports it
        await asyncio.wait_for(daemon.run(world.stop), 5)
    assert all(c.is_closed for c in world.clients)
    with RunLock(tmp_path / "run.lock"):
        pass


# ----- review fixes: failures, deadline, blocked streak, request result ------------


async def test_an_error_escaping_the_cycle_is_logged_and_the_schedule_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import sqlite3

    from calsync import daemon as daemon_module

    real = daemon_module.run_cycle
    calls: list[int] = []

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls.append(int((world.now - NOW).total_seconds()))
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return await real(*args, **kwargs)

    monkeypatch.setattr(daemon_module, "run_cycle", flaky)
    caplog.set_level(logging.INFO, logger="calsync.daemon")
    world = World(stop_after=2)
    daemon = world.daemon(two_rules(interval=30), tmp_path)
    await asyncio.wait_for(daemon.run(world.stop), 5)
    errors = [r for r in caplog.records if r.name == "calsync.daemon" and r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errors] == ["sync cycle failed"]
    assert world.delays == [30, 30]  # a full wait after the failure: no tight loop
    assert calls == [0, 30] and runs_by_time(tmp_path) == [(30, "family"), (30, "work")]
    assert daemon.snapshot().cycles == 1  # the failed cycle is not counted


async def test_a_rule_request_keeps_the_timed_deadline(tmp_path: Path) -> None:
    world = World(block=True)
    daemon = world.daemon(two_rules(interval=30), tmp_path)
    task = asyncio.create_task(daemon.run(world.stop))
    await until(lambda: len(world.delays) == 1)
    deadline = NOW + timedelta(seconds=30)
    world.now = NOW + timedelta(seconds=10)
    assert daemon.request_cycle("work") is True
    await until(lambda: len(world.delays) == 2)
    assert world.delays[1] == 20  # the rest of the original wait, not a fresh interval
    assert daemon.snapshot().next_cycle_at == deadline
    # Past the deadline, a request for `work` must not starve `family`: the timed cycle follows.
    world.now = NOW + timedelta(seconds=40)
    daemon.request_cycle("work")
    await until(lambda: len(world.delays) == 3)
    assert runs_by_time(tmp_path)[2:] == [(10, "work"), (40, "work"), (40, "family"), (40, "work")]
    assert world.delays[2] == 30  # the timed cycle set a fresh deadline
    world.stop.set()
    await asyncio.wait_for(task, 5)


async def test_a_request_for_every_rule_restarts_the_timer(tmp_path: Path) -> None:
    world = World(block=True)
    daemon = world.daemon(two_rules(interval=30), tmp_path)
    task = asyncio.create_task(daemon.run(world.stop))
    await until(lambda: len(world.delays) == 1)
    world.now = NOW + timedelta(seconds=10)
    daemon.request_cycle()
    await until(lambda: len(world.delays) == 2)
    assert world.delays[1] == 30 and daemon.snapshot().next_cycle_at == NOW + timedelta(seconds=40)
    world.stop.set()
    await asyncio.wait_for(task, 5)


async def test_a_blocked_result_leaves_the_failure_streak_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from calsync import daemon as daemon_module
    from calsync.engine import CycleResult, RuleResult

    script = {0: "ok", 90: "blocked"}  # every other attempt fails
    calls: list[int] = []

    async def scripted(engine: Any, store: Any, config: Any, adapters: Any, rules: Any, **kw: Any) -> Any:
        at = int((world.now - NOW).total_seconds())
        calls.append(at)
        status = script.get(at, "failed")
        return CycleResult(world.now, world.now, {r: RuleResult(r, status=status) for r in rules})

    monkeypatch.setattr(daemon_module, "run_cycle", scripted)
    world = World(stop_after=9)  # ticks at 0, 30, ..., 240 s
    config = replace(config_for([rule()]), runtime=RuntimeConfig(timedelta(seconds=30), 0.0))
    await world.daemon(config, tmp_path).run(world.stop)
    # Failed at 30 (1 in a row: skipped until 90), blocked at 90 (still 1 in a row), failed at
    # 120: 2 in a row, skipped for 2 minutes until 240. A reset at 90 would have run it at 180.
    assert calls == [0, 30, 90, 120, 240]


async def test_request_cycle_reports_whether_it_was_accepted(tmp_path: Path) -> None:
    syncing = World().daemon(two_rules(), tmp_path)
    assert syncing.request_cycle() is True and syncing.request_cycle("work") is True
    viewer = Daemon(two_rules(), tmp_path, sync=False)
    assert viewer.request_cycle() is False and viewer.request_cycle("work") is False
    with pytest.raises(ValueError, match="unknown rule"):
        viewer.request_cycle("nope")


# ----- the API socket ---------------------------------------------------------------


async def run_viewer(state_dir: Path) -> tuple[Daemon, asyncio.Event, "asyncio.Task[None]"]:
    """A ``--no-sync`` daemon serving the socket in ``state_dir``, once it is listening."""
    stop = asyncio.Event()
    viewer = Daemon(two_rules(), state_dir, sync=False, clock=lambda: NOW)
    task = asyncio.create_task(viewer.run(stop))
    await eventually(lambda: (state_dir / "calsync.sock").is_socket() or task.done())
    if task.done():
        task.result()
    return viewer, stop, task


async def test_serves_the_api_on_a_private_socket(short_dir: Path) -> None:
    import signal
    import stat

    world = World(stop_after=1)  # one cycle, for a state to show
    await world.daemon(two_rules(), short_dir).run(world.stop)
    before = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    viewer, stop, task = await run_viewer(short_dir)
    sock = short_dir / "calsync.sock"
    assert stat.S_IMODE(sock.stat().st_mode) == 0o600
    async with uds_client(short_dir) as client:
        status = await client.get("/api/status")
        runs = await client.get("/api/runs")
        trigger = await client.post("/api/trigger")
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before  # uvicorn left them
    assert status.status_code == 200
    body = status.json()
    assert body["daemon"] == viewer.snapshot().to_json() and body["daemon"]["sync"] is False
    assert [r["name"] for r in body["rules"]] == ["family", "work"]
    assert len(runs.json()["runs"]) == 2
    assert trigger.status_code == 409 and trigger.json()["error"]["kind"] == "read_only"
    stop.set()
    await asyncio.wait_for(task, 5)
    assert not sock.exists()  # removed on exit
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before


async def test_a_stale_socket_file_is_replaced(short_dir: Path) -> None:
    import socket

    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(short_dir / "calsync.sock"))  # bound, never listened on, closed: nobody answers
    dead.close()
    assert (short_dir / "calsync.sock").is_socket()
    _, stop, task = await run_viewer(short_dir)
    async with uds_client(short_dir) as client:
        response = await client.get("/api/status")
    assert response.status_code == 503 and response.json()["error"]["kind"] == "no_state"
    stop.set()
    await asyncio.wait_for(task, 5)


async def test_a_live_socket_means_another_daemon(short_dir: Path) -> None:
    _, stop, task = await run_viewer(short_dir)
    second = Daemon(two_rules(), short_dir, sync=False)
    with pytest.raises(AlreadyRunning) as info:
        await asyncio.wait_for(second.run(asyncio.Event()), 5)
    assert info.value.pid is None
    async with uds_client(short_dir) as client:  # the first one still serves
        assert (await client.post("/api/trigger")).status_code == 409
    stop.set()
    await asyncio.wait_for(task, 5)
    assert not (short_dir / "calsync.sock").exists()


async def test_trigger_over_the_socket_runs_a_cycle(short_dir: Path) -> None:
    world = World(block=True)
    daemon = world.daemon(two_rules(), short_dir, serve=True)
    task = asyncio.create_task(daemon.run(world.stop))
    await until(lambda: len(world.delays) == 1)
    async with uds_client(short_dir) as client:
        response = await client.post("/api/trigger", json={"rule": "work"})
        unknown = await client.post("/api/trigger", json={"rule": "nope"})
        await eventually(lambda: len(world.delays) == 2)
        status = (await client.get("/api/status")).json()
    assert response.status_code == 202 and response.json() == {"accepted": True, "rule": "work"}
    assert unknown.status_code == 404
    assert runs_by_time(short_dir) == [(0, "family"), (0, "work"), (0, "work")]
    assert status["daemon"]["cycles"] == 2 and status["daemon"]["sync"] is True
    world.stop.set()
    await asyncio.wait_for(task, 5)
    assert not (short_dir / "calsync.sock").exists()


async def test_a_failing_api_server_lets_the_running_cycle_finish(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from calsync import api
    from calsync import daemon as daemon_module

    world = World()
    cycle_started = asyncio.Event()
    real = daemon_module.run_cycle

    async def slow_cycle(*args: Any, **kwargs: Any) -> Any:
        cycle_started.set()
        await world.stop.wait()  # the server fails meanwhile: the failure sets stop
        return await real(*args, **kwargs)

    async def broken_server(*args: Any) -> None:
        await cycle_started.wait()
        raise RuntimeError("server broke")

    monkeypatch.setattr(daemon_module, "run_cycle", slow_cycle)
    monkeypatch.setattr(api, "serve", broken_server)
    caplog.set_level(logging.INFO, logger="calsync.daemon")
    daemon = world.daemon(two_rules(), short_dir, serve=True)
    with pytest.raises(RuntimeError, match="server broke"):
        await asyncio.wait_for(daemon.run(world.stop), 5)
    assert runs_by_time(short_dir) == [(0, "family"), (0, "work")]  # the cycle completed
    assert daemon.snapshot().cycles == 1 and world.delays == []
    assert "the api server failed (RuntimeError); stopping" in caplog.messages
    assert all(c.is_closed for c in world.clients)
    assert not (short_dir / "calsync.sock").exists()
    with RunLock(short_dir / "run.lock"):
        pass


async def test_a_socket_that_cannot_be_bound_stops_the_start(tmp_path: Path) -> None:
    world = World(stop_after=1)
    state_dir = tmp_path / ("d" * 110)  # longer than a unix socket path may be
    daemon = world.daemon(two_rules(), state_dir, serve=True)
    with pytest.raises(OSError):
        await asyncio.wait_for(daemon.run(world.stop), 5)
    assert world.builds == [] and not (state_dir / "state.db").exists()
    with RunLock(state_dir / "run.lock"):
        pass
