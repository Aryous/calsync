"""``trigger``: a cycle now, optionally waiting for its result, through the daemon (its waiters,
the operation, the socket and the CLI) and in-process without a daemon.

The daemon tests use the fake time and accounts of ``test_daemon.World`` with a config file
(``test_daemon_reload``): rules ``family`` and ``work``.
"""

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import anyio.to_thread
import httpx
import pytest

from calsync import api, configfile, wiring
from calsync.cli import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, main
from calsync.daemon import Daemon
from calsync.engine import CycleResult
from calsync.errors import ConfigError, RulePaused
from calsync.ops import DaemonStopping, OpResult
from calsync.views import StateReader
from tests.test_daemon import World, eventually, until
from tests.test_daemon_reload import ACCOUNTS, FAMILY, TEXT, WORK, write

pytestmark = pytest.mark.anyio

RESULT_KEYS = ["status", "created", "updated", "deleted", "failed", "error_kind"]


class Running:
    """A daemon with a config file, fake time and waits that only a request or stop ends."""

    def __init__(self, root: Path, text: str = TEXT, **kwargs: Any) -> None:
        self.path = root / "config.toml"
        self.path.write_text(text)
        self.world = World(block=True)
        config = configfile.validate_text(text, root)
        self.daemon: Daemon = self.world.daemon(config, root, config_path=self.path, **kwargs)
        self.root = root
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.daemon.run(self.world.stop))
        await until(lambda: len(self.world.delays) == 1)  # the first cycle ran

    async def stop(self) -> None:
        self.world.stop.set()
        assert self.task is not None
        await asyncio.wait_for(self.task, 5)

    def cycles(self) -> int:
        return self.daemon.snapshot().cycles

    async def trigger(self, **args: Any) -> OpResult:
        return await asyncio.wait_for(self.daemon.run_op("trigger", args, False, source="cli"), 5)

    def runs(self, rule: str) -> int:
        with StateReader(self.root / "state.db") as reader:
            return len(reader.runs(rule))


# ----- the daemon's waiters ------------------------------------------------------------------


async def test_a_request_during_a_cycle_waits_for_the_follow_up(tmp_path: Path) -> None:
    d = Running(tmp_path)
    await d.start()
    futures: list[asyncio.Future[CycleResult | None]] = []
    # During the next cycle (every rule), a request for "work": that cycle started before it.
    d.world.on_google = lambda: futures.append(d.daemon.request_cycle("work", wait=True))
    d.daemon.request_cycle()
    await until(lambda: bool(futures) and futures[0].done())
    result = futures[0].result()
    assert result is not None and list(result.rules) == ["work"]  # the follow-up ran only "work"
    assert d.cycles() == 3  # the first, the one it interrupted, and the follow-up
    await d.stop()


async def test_a_request_between_cycles_is_answered_by_the_next_cycle(tmp_path: Path) -> None:
    d = Running(tmp_path)
    await d.start()
    future = d.daemon.request_cycle("family", wait=True)
    result = await asyncio.wait_for(future, 5)
    assert result is not None and list(result.rules) == ["family"] and d.cycles() == 2
    assert d.daemon._waiters == []  # pyright: ignore[reportPrivateUsage]
    await d.stop()


async def test_every_rule_waits_for_a_cycle_that_ran_every_unpaused_rule(tmp_path: Path) -> None:
    d = Running(tmp_path, ACCOUNTS + FAMILY + WORK + WORK.replace('"work"', '"held"') + "paused = true\n")
    await d.start()
    daemon = d.daemon
    async with daemon._cycle_lock:  # pyright: ignore[reportPrivateUsage]  # no cycle meanwhile
        daemon.request_cycle("family")  # the next cycle runs "family" only
        every = daemon._wait_for_cycle(None)  # pyright: ignore[reportPrivateUsage]
    await until(lambda: d.cycles() == 2)
    assert not every.done()  # "work" did not run
    daemon.request_cycle()
    result = await asyncio.wait_for(every, 5)
    assert result is not None and list(result.rules) == ["family", "work"]  # "held" is paused
    await d.stop()


async def test_a_failed_cycle_answers_the_waiter_with_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from calsync import daemon as daemon_module

    d = Running(tmp_path)
    await d.start()

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the state database is gone")

    monkeypatch.setattr(daemon_module, "run_cycle", broken)
    assert await asyncio.wait_for(d.daemon.request_cycle("work", wait=True), 5) is None
    result = await d.trigger(rule="work", wait=True)
    assert result.data == {"ran": False, "reason": "cycle_failed"} and not result.changed
    assert result.summary == "the sync cycle failed; see the daemon log"
    await d.stop()


async def test_stopping_refuses_the_waiters_and_later_requests(tmp_path: Path) -> None:
    d = Running(tmp_path)
    await d.start()
    async with d.daemon._cycle_lock:  # pyright: ignore[reportPrivateUsage]  # the cycle never starts
        future = d.daemon.request_cycle("work", wait=True)
        d.world.stop.set()
        await asyncio.sleep(0.05)
    await d.stop()
    with pytest.raises(DaemonStopping, match=r"^The daemon is stopping\.$"):
        future.result()
    with pytest.raises(DaemonStopping):
        d.daemon.request_cycle("work", wait=True)
    with pytest.raises(DaemonStopping):
        d.daemon.request_cycle()  # also without waiting: nothing would run it


async def test_a_rule_paused_or_removed_while_waited_for_refuses_the_waiter(tmp_path: Path) -> None:
    d = Running(tmp_path)
    await d.start()
    daemon = d.daemon
    async with daemon._cycle_lock:  # pyright: ignore[reportPrivateUsage]
        paused = daemon.request_cycle("work", wait=True)
        removed = daemon.request_cycle("family", wait=True)
        every = daemon.request_cycle(wait=True)
        write(d.path, ACCOUNTS + WORK.replace('name = "work"', 'name = "work"\npaused = true'))
        await daemon._check_config()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(paused.exception(), RulePaused)
    assert isinstance(removed.exception(), ConfigError)
    assert str(removed.exception()) == 'rule "family" was removed before its cycle ran'
    assert not every.done()  # every rule means the unpaused ones: none now; stopping answers it
    await d.stop()
    assert isinstance(every.exception(), DaemonStopping)


async def test_request_cycle_with_wait_without_sync_is_unavailable(tmp_path: Path) -> None:
    from calsync.ops import OpsUnavailable
    from tests.test_daemon import two_rules

    viewer = Daemon(two_rules(), tmp_path, sync=False)
    with pytest.raises(OpsUnavailable, match="--no-sync"):
        viewer.request_cycle(wait=True)


# ----- the trigger operation in the daemon -----------------------------------------------------


async def test_trigger_waits_for_the_cycle_and_reports_it(tmp_path: Path) -> None:
    d = Running(tmp_path)
    await d.start()
    d.world.fake.put_event(
        "work@group.calendar.google.com",
        {
            "id": "retro",
            "status": "confirmed",
            "summary": "Retro",
            "start": {"dateTime": "2026-10-27T09:00:00+08:00"},
            "end": {"dateTime": "2026-10-27T10:00:00+08:00"},
            "updated": "2026-10-24T00:00:00Z",
        },
    )
    result = await d.trigger(rule="work", wait=True)
    assert result.op == "trigger" and result.changed
    assert result.data["ran"] is True and list(result.data) == ["ran", "rules", "requests"]
    assert list(result.data["rules"]) == ["work"]
    work = result.data["rules"]["work"]
    assert list(work) == RESULT_KEYS
    assert (work["status"], work["created"], work["deleted"], work["error_kind"]) == ("ok", 1, 0, None)
    assert isinstance(result.data["requests"], int) and result.data["requests"] > 0
    assert result.summary == "the cycle ran: work ok"
    json.dumps(result.to_json())  # JSON as it is
    await d.stop()


async def test_trigger_without_wait_is_accepted_at_once(tmp_path: Path) -> None:
    d = Running(tmp_path)
    await d.start()
    async with d.daemon._cycle_lock:  # pyright: ignore[reportPrivateUsage]  # a cycle "runs"
        result = await d.trigger(rule="work")  # never waits for the lock
    assert result.data == {"accepted": True, "rule": "work"} and result.summary == "cycle requested"
    await eventually(lambda: d.runs("work") == 2)
    await d.stop()


async def test_trigger_times_out_without_holding_the_lock(tmp_path: Path) -> None:
    d = Running(tmp_path)
    await d.start()
    lock = d.daemon._cycle_lock  # pyright: ignore[reportPrivateUsage]
    async with lock:  # the requested cycle cannot start meanwhile
        result = await d.trigger(rule="work", wait=True, timeout=0.05)
    assert result.data == {"ran": False, "reason": "timeout"} and not result.changed
    assert result.summary == "no cycle finished within 0.05 s; the requested cycle still runs"
    await eventually(lambda: d.runs("work") == 2)  # the request stood
    assert d.daemon._waiters == []  # pyright: ignore[reportPrivateUsage]  # given up, then forgotten
    await d.stop()


async def test_a_client_gone_cancels_only_the_waiting(tmp_path: Path) -> None:
    d = Running(tmp_path)
    await d.start()
    lock = d.daemon._cycle_lock  # pyright: ignore[reportPrivateUsage]
    await lock.acquire()
    waiting = asyncio.create_task(
        d.daemon.run_op("trigger", {"rule": "work", "wait": True}, False, source="ui")
    )
    await until(lambda: bool(d.daemon._waiters))  # pyright: ignore[reportPrivateUsage]
    waiting.cancel()  # what a disconnect does to the request's task
    await asyncio.gather(waiting, return_exceptions=True)
    lock.release()
    await eventually(lambda: d.runs("work") == 2)  # the cycle ran all the same
    assert d.daemon._waiters == []  # pyright: ignore[reportPrivateUsage]
    await d.stop()


@pytest.mark.parametrize(
    ("args", "error", "match"),
    [
        ({"rule": "nope"}, ConfigError, 'unknown rule "nope"'),
        ({"timeout": 601}, ConfigError, "at most 600"),
        ({"timeout": 0}, ConfigError, "above 0"),
        ({"timeout": "soon"}, ConfigError, "a number of seconds"),
        ({"wait": "yes"}, ConfigError, '"wait" must be true or false'),
        ({"extra": 1}, ConfigError, 'unknown argument "extra"'),
    ],
)
async def test_trigger_refuses_bad_arguments(
    tmp_path: Path, args: dict[str, Any], error: type, match: str
) -> None:
    d = Running(tmp_path)
    await d.start()
    with pytest.raises(error, match=match):
        await d.trigger(**args)
    await d.stop()


async def test_trigger_for_a_paused_rule_is_refused(tmp_path: Path) -> None:
    d = Running(tmp_path, ACCOUNTS + FAMILY + WORK.replace('name = "work"', 'name = "work"\npaused = true'))
    await d.start()
    with pytest.raises(RulePaused, match='rule "work" is paused'):
        await d.trigger(rule="work", wait=True)
    await d.stop()


# ----- over the socket ---------------------------------------------------------------------------


def uds(root: Path) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=str(root / "calsync.sock"))
    return httpx.AsyncClient(transport=transport, base_url="http://calsync", timeout=10)


async def test_trigger_over_the_socket(short_dir: Path) -> None:
    d = Running(short_dir, serve=True)
    await d.start()
    await eventually(lambda: (short_dir / "calsync.sock").is_socket())
    async with uds(short_dir) as c:
        waited = await c.post("/api/ops/trigger", json={"args": {"rule": "family", "wait": True}})
        paused = d.path.read_text()
        write(d.path, paused.replace('name = "work"', 'name = "work"\npaused = true'))
        await c.post("/api/ops/trigger", json={"args": {"rule": "family", "wait": True}})  # applies the edit
        refused = await c.post("/api/ops/trigger", json={"args": {"rule": "work", "wait": True}})
    assert waited.status_code == 200, waited.text
    body = waited.json()
    assert (
        body["op"] == "trigger" and body["data"]["ran"] is True and list(body["data"]["rules"]) == ["family"]
    )
    assert refused.status_code == 409
    assert refused.json()["error"] == {"kind": "paused", "message": 'rule "work" is paused', "rule": "work"}
    await d.stop()


async def test_the_trigger_endpoint_after_the_scheduler_stopped_is_503(tmp_path: Path) -> None:
    d = Running(tmp_path)
    await d.start()
    await d.stop()
    app = api.create_app(config_provider=lambda: d.daemon.config, state_dir=tmp_path, daemon=d.daemon)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://calsync") as c:
        legacy = await c.post("/api/trigger", json={"rule": "work"})
    assert legacy.status_code == 503
    assert legacy.json() == {"error": {"kind": "unavailable", "message": "The daemon is stopping."}}


# ----- the CLI -----------------------------------------------------------------------------------


def cli(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, out=out, stdin=io.StringIO(""))
    return code, out.getvalue()


async def test_cli_trigger_wait_through_the_daemon(short_dir: Path) -> None:
    d = Running(short_dir, serve=True)
    await d.start()
    await eventually(lambda: (short_dir / "calsync.sock").is_socket())
    base = ["--config", str(d.path), "--state-dir", str(short_dir), "trigger"]
    code, out = await anyio.to_thread.run_sync(lambda: cli([*base, "--rule", "work", "--wait", "--json"]))
    assert code == EXIT_OK, out
    body = json.loads(out)
    assert body["data"]["ran"] is True and list(body["data"]["rules"]) == ["work"]
    code, out = await anyio.to_thread.run_sync(lambda: cli([*base, "--wait"]))
    assert code == EXIT_OK
    lines = out.splitlines()
    assert lines[0] == "the cycle ran: family ok, work ok"
    assert lines[1].startswith("rule=family status=ok created=0 updated=0 deleted=0 failed=0")
    assert lines[2].startswith("rule=work status=ok") and lines[3].startswith("requests=")
    # Without --wait: requested, as before.
    assert await anyio.to_thread.run_sync(lambda: cli(base)) == (EXIT_OK, "ok: cycle requested\n")
    code, out = await anyio.to_thread.run_sync(lambda: cli([*base, "--json"]))
    assert code == EXIT_OK and json.loads(out)["data"] == {"accepted": True, "rule": None}
    await d.stop()


async def test_cli_trigger_wait_times_out_with_exit_1(short_dir: Path) -> None:
    d = Running(short_dir, serve=True)
    await d.start()
    await eventually(lambda: (short_dir / "calsync.sock").is_socket())
    argv = ["--config", str(d.path), "--state-dir", str(short_dir), "trigger", "--wait", "--timeout", "1"]
    async with d.daemon._cycle_lock:  # pyright: ignore[reportPrivateUsage]  # no cycle can start
        code, out = await anyio.to_thread.run_sync(lambda: cli([*argv, "--json"]))
    assert code == EXIT_FAILED
    assert json.loads(out)["data"] == {"ran": False, "reason": "timeout"}
    await d.stop()


async def test_cli_trigger_for_a_paused_rule_through_the_daemon(short_dir: Path) -> None:
    d = Running(
        short_dir,
        ACCOUNTS + FAMILY + WORK.replace('name = "work"', 'name = "work"\npaused = true'),
        serve=True,
    )
    await d.start()
    await eventually(lambda: (short_dir / "calsync.sock").is_socket())
    base = ["--config", str(d.path), "--state-dir", str(short_dir), "trigger", "--rule", "work", "--wait"]
    assert await anyio.to_thread.run_sync(lambda: cli(base)) == (EXIT_FAILED, 'rule "work" is paused\n')
    code, out = await anyio.to_thread.run_sync(lambda: cli([*base, "--json"]))
    assert code == EXIT_FAILED and json.loads(out)["error"]["kind"] == "paused"
    code, out = await anyio.to_thread.run_sync(lambda: cli([*base[:-3], "--rule", "nope", "--wait"]))
    assert code == EXIT_CONFIG and 'unknown rule "nope"' in out
    await d.stop()


# ----- in-process, without a daemon ---------------------------------------------------------------


class Here:
    """A config file and a state directory with no daemon; the accounts are ``World``'s fakes."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, text: str = TEXT) -> None:
        self.world = World()
        monkeypatch.setattr(wiring, "build_adapters", self.world.factory)
        self.path = root / "config.toml"
        self.path.write_text(text)
        self.state = root / "s"

    def cli(self, *argv: str) -> tuple[int, str]:
        return cli(["--config", str(self.path), "--state-dir", str(self.state), "trigger", *argv])

    def runs(self, rule: str) -> int:
        with StateReader(self.state / "state.db") as reader:
            return len(reader.runs(rule))


def test_trigger_without_a_daemon_runs_the_cycle_here(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = Here(short_dir, monkeypatch)
    code, out = here.cli("--rule", "work", "--wait", "--json")
    assert code == EXIT_OK, out
    body = json.loads(out)
    assert body["op"] == "trigger" and body["changed"] is True
    assert body["summary"] == "no daemon runs, so the cycle ran here: work ok"
    assert list(body["data"]) == ["ran", "rules", "requests"] and list(body["data"]["rules"]) == ["work"]
    assert list(body["data"]["rules"]["work"]) == RESULT_KEYS
    assert (here.runs("work"), here.runs("family")) == (1, 0)  # that rule only
    code, out = here.cli()  # without --wait: there is nothing to hand it to, so it runs here too
    assert code == EXIT_OK
    assert out.splitlines()[0] == "no daemon runs, so the cycle ran here: family ok, work ok"
    assert (here.runs("work"), here.runs("family")) == (2, 1)


def test_trigger_without_a_daemon_honours_paused_rules(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = Here(
        short_dir,
        monkeypatch,
        ACCOUNTS + FAMILY + WORK.replace('name = "work"', 'name = "work"\npaused = true'),
    )
    assert here.cli("--rule", "work") == (EXIT_FAILED, 'rule "work" is paused\n')
    code, out = here.cli("--json")
    assert code == EXIT_OK and list(json.loads(out)["data"]["rules"]) == ["family"]
    code, out = here.cli("--rule", "nope")
    assert code == EXIT_CONFIG and 'unknown rule "nope"' in out


def test_trigger_without_a_daemon_exits_by_the_rules(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = Here(
        short_dir,
        monkeypatch,
        ACCOUNTS + FAMILY + WORK.replace('calendar = "Work" }', 'calendar = "Missing" }', 1),
    )
    code, out = here.cli("--json")
    assert code == EXIT_CONFIG  # as `calsync run`: a missing calendar is a config error
    rules = json.loads(out)["data"]["rules"]
    assert (rules["family"]["status"], rules["work"]["status"]) == ("ok", "failed")
    assert rules["work"]["error_kind"] == "config"


def test_trigger_without_a_daemon_waits_for_the_lock_holder(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from calsync.cli import EXIT_RUNNING
    from calsync.lock import RunLock

    here = Here(short_dir, monkeypatch)
    here.state.mkdir()
    with RunLock(here.state / "run.lock"):  # `calsync run` holds it
        code, out = here.cli("--wait")
    assert code == EXIT_RUNNING and "holds the lock" in out


def test_trigger_timeout_bounds() -> None:
    with pytest.raises(SystemExit):
        cli(["trigger", "--timeout", "601"])
    with pytest.raises(SystemExit):
        cli(["trigger", "--timeout", "0"])
