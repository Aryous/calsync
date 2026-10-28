"""Approving blocked deletes: the fingerprint, ``calsync blocked``, the ``approve`` operation in
the daemon (through its socket too) and in-process, and the CLI's confirmation prompt.

The engine tests use FakeGoogle and FakeTarget (``build_fake``); the daemon tests use the fake
time and accounts of ``test_daemon.World``. Twelve copies over the default guard (max_count 10).
"""

import asyncio
import io
import json
import logging
from pathlib import Path
from typing import Any

import anyio.to_thread
import pytest

from calsync import api, configfile, opsclient, wiring
from calsync.cli import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, main
from calsync.config import Config
from calsync.daemon import Daemon
from calsync.engine import Approval, delete_fingerprint
from calsync.errors import ConfigError
from calsync.ops import OpResult
from calsync.store import ChangeRecord, RunRecord, Store
from calsync.views import StateReader, build_blocked
from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, NOW, build_fake
from tests.test_daemon import World, eventually, until
from tests.test_daemon_reload import ACCOUNTS, FAMILY, TEXT

pytestmark = pytest.mark.anyio

COUNT = 12  # copies of rule "family" in the tests below (the guard's max_count is 10)


def seed(fake: FakeGoogle, n: int = COUNT) -> list[str]:
    """``n`` events in the family calendar, titled out of id order; their ids."""
    ids = [f"e{i:02d}" for i in range(n)]
    for i, event_id in enumerate(ids):
        fake.put_event(
            GCAL,
            google_event(
                event_id, f"Event {n - 1 - i:02d}", "2026-10-27T09:00:00+08:00", "2026-10-27T09:30:00+08:00"
            ),
        )
    return ids


def family_config(tmp_path: Path) -> Config:
    return configfile.validate_text(ACCOUNTS + FAMILY, tmp_path)


def blocked_view(state_dir: Path, config: Config, rule: str | None = None) -> Any:
    with StateReader(state_dir / "state.db") as reader:
        return build_blocked(config, reader, rule)


# ----- the engine and the view -------------------------------------------------------------


async def blocked_engine(tmp_path: Path, cancel: int = COUNT) -> tuple[Any, Any, FakeGoogle, list[str]]:
    """An engine whose rule is blocked: ``COUNT`` copies, ``cancel`` of their events cancelled."""
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    ids = seed(fake)
    h = build_fake(tmp_path, fake, family_config(tmp_path))
    await h.engine.cycle()
    for event_id in ids[:cancel]:
        fake.cancel_event(GCAL, event_id)
    result = (await h.engine.cycle()).rules["family"]
    assert result.status == "blocked"
    return h, result, fake, ids


async def test_the_engine_and_the_view_agree_on_the_fingerprint(tmp_path: Path) -> None:
    h, result, _, _ = await blocked_engine(tmp_path)
    ids = [a.object_id for a in result.plan.delete]
    assert len(ids) == COUNT
    assert result.blocked_fingerprint == delete_fingerprint(ids) == delete_fingerprint(reversed(ids))
    assert len(result.blocked_fingerprint) == 16
    [entry] = blocked_view(tmp_path, family_config(tmp_path)).rules
    assert (entry.rule, entry.count, entry.fingerprint) == ("family", COUNT, result.blocked_fingerprint)
    h.store.close()


async def test_blocked_json_shape(tmp_path: Path) -> None:
    h, result, _, _ = await blocked_engine(tmp_path)
    h.store.close()
    body = blocked_view(tmp_path, family_config(tmp_path)).to_json()
    assert list(body) == ["blocked"]
    [entry] = body["blocked"]
    assert list(entry) == [
        "rule",
        "since",
        "count",
        "copies",
        "ratio",
        "limit",
        "reason",
        "fingerprint",
        "items",
    ]
    assert entry["since"] == NOW.isoformat()
    assert (entry["count"], entry["copies"], entry["ratio"]) == (COUNT, COUNT, 1.0)
    assert entry["limit"] == {"max_count": 10, "max_ratio": 0.5}
    assert entry["reason"] == "plan deletes 12 events, more than max_count=10"
    assert entry["fingerprint"] == result.blocked_fingerprint
    assert [i["title"] for i in entry["items"]] == [f"Event {i:02d}" for i in range(COUNT)]  # title order
    assert sorted(i["object_id"] for i in entry["items"]) == sorted(a.object_id for a in result.plan.delete)
    assert blocked_view(tmp_path, family_config(tmp_path), "nope").to_json() == {"blocked": []}


async def test_an_approval_releases_exactly_the_blocked_set(tmp_path: Path) -> None:
    h, result, _, _ = await blocked_engine(tmp_path)
    other = (await h.engine.cycle(approvals={"family": Approval("0" * 16, "cli")})).rules["family"]
    assert (other.status, other.deleted, other.approved) == ("blocked", 0, False)
    assert len(h.target.objects) == COUNT
    done = (await h.engine.cycle(approvals={"family": Approval(result.blocked_fingerprint, "cli")})).rules[
        "family"
    ]
    assert (done.status, done.deleted, done.approved, done.blocked_fingerprint) == ("ok", COUNT, True, None)
    assert h.target.objects == {}
    with StateReader(tmp_path / "state.db") as reader:
        changes = list(reader.changes("family"))
        deleted = [c for c in changes if c.action == "deleted"]
        approved = [c for c in changes if c.action == "approved"]
    assert len(deleted) == COUNT  # recorded as change rows, as any delete
    # Who let the set through, with the cycle that released it, before its deletes.
    [row] = approved
    assert (row.object_id, row.title) == (result.blocked_fingerprint, "12 deletes approved (cli)")
    assert row.run_id == deleted[0].run_id and row.id < min(c.id for c in deleted)
    h.store.close()


async def test_an_approval_that_releases_nothing_is_not_recorded(tmp_path: Path) -> None:
    h, _, _, _ = await blocked_engine(tmp_path)
    await h.engine.cycle(approvals={"family": Approval("0" * 16, "ui")})
    with StateReader(tmp_path / "state.db") as reader:
        assert [c for c in reader.changes("family") if c.action == "approved"] == []
    h.store.close()


async def test_a_grown_set_blocks_again_with_a_new_fingerprint(tmp_path: Path) -> None:
    h, first, fake, ids = await blocked_engine(tmp_path, cancel=COUNT - 1)
    fake.cancel_event(GCAL, ids[-1])  # one more delete since the approval was given
    again = (await h.engine.cycle(approvals={"family": Approval(first.blocked_fingerprint, "cli")})).rules[
        "family"
    ]
    assert (again.status, again.deleted, again.approved) == ("blocked", 0, False)
    assert again.blocked_fingerprint not in (None, first.blocked_fingerprint)
    assert len(h.target.objects) == COUNT
    [entry] = blocked_view(tmp_path, family_config(tmp_path)).rules
    assert (entry.count, entry.fingerprint) == (COUNT, again.blocked_fingerprint)
    h.store.close()


async def test_allow_deletes_keeps_working(tmp_path: Path) -> None:
    h, result, _, _ = await blocked_engine(tmp_path)
    done = (await h.engine.cycle(allow_deletes=True)).rules["family"]
    assert (done.status, done.deleted, done.approved_by) == ("ok", COUNT, "run")
    # A set the guard would have blocked, let through: recorded like an approval, origin "run".
    with StateReader(tmp_path / "state.db") as reader:
        [row] = [c for c in reader.changes("family") if c.action == "approved"]
    assert (row.object_id, row.title) == (result.blocked_fingerprint, "12 deletes approved (run)")
    h.store.close()


async def test_allow_deletes_the_guard_would_not_block_records_nothing(tmp_path: Path) -> None:
    h, _, fake, ids = await blocked_engine(tmp_path)
    dry = (await h.engine.cycle(dry_run=True, allow_deletes=True)).rules["family"]
    assert (dry.status, dry.approved_by) == ("ok", None)  # a dry run releases nothing
    seed(fake)  # all back, then two gone: under the guard
    for event_id in ids[:2]:
        fake.cancel_event(GCAL, event_id)
    done = (await h.engine.cycle(allow_deletes=True)).rules["family"]
    assert (done.status, done.deleted, done.approved_by) == ("ok", 2, None)
    with StateReader(tmp_path / "state.db") as reader:
        assert [c for c in reader.changes("family") if c.action == "approved"] == []
    h.store.close()


async def test_an_approval_names_its_origin_on_the_result(tmp_path: Path) -> None:
    h, result, _, _ = await blocked_engine(tmp_path)
    done = (await h.engine.cycle(approvals={"family": Approval(result.blocked_fingerprint, "ui")})).rules[
        "family"
    ]
    assert (done.approved, done.approved_by) == (True, "ui")
    h.store.close()


# ----- the approve operation in the daemon --------------------------------------------------


class Running:
    """A daemon (fake time, waits that only a request or stop ends) whose rule ``family`` is
    blocked: ``COUNT`` copies, ``cancel`` of their events cancelled."""

    def __init__(self, tmp_path: Path, **kwargs: Any) -> None:
        self.path = tmp_path / "config.toml"
        self.path.write_text(TEXT)
        self.world = World(block=True)
        self.world.fake.cancel_event(GCAL, "lunch")  # only the seeded events
        self.ids = seed(self.world.fake)
        config = configfile.validate_text(TEXT, tmp_path)
        self.daemon: Daemon = self.world.daemon(config, tmp_path, config_path=self.path, **kwargs)
        self.state_dir = tmp_path
        self.task: asyncio.Task[None] | None = None

    async def start(self, cancel: int = COUNT) -> str:
        self.task = asyncio.create_task(self.daemon.run(self.world.stop))
        await until(lambda: len(self.world.delays) == 1)  # the first cycle made the copies
        for event_id in self.ids[:cancel]:
            self.world.fake.cancel_event(GCAL, event_id)
        await self.cycle()
        [entry] = blocked_view(self.state_dir, self.daemon.config).rules
        return entry.fingerprint

    async def cycle(self, rule: str = "family") -> None:
        """Request a cycle for ``rule`` and wait until it is recorded."""
        before = self.runs(rule)
        self.daemon.request_cycle(rule)
        await eventually(lambda: self.runs(rule) > before)

    def runs(self, rule: str = "family") -> int:
        with StateReader(self.state_dir / "state.db") as reader:
            return len(reader.runs(rule))

    async def approve(self, **args: Any) -> OpResult:
        return await asyncio.wait_for(self.daemon.run_op("approve", args, False, source="cli"), 5)

    async def stop(self) -> None:
        self.world.stop.set()
        assert self.task is not None
        await asyncio.wait_for(self.task, 5)

    def family_copies(self) -> int:
        return len([h for h in self.world.target.objects if "/family/" in h])

    def latest(self, rule: str = "family") -> Any:
        with StateReader(self.state_dir / "state.db") as reader:
            return reader.runs(rule, limit=1)[0]


async def test_approve_through_the_daemon_waits_for_the_cycle_without_holding_the_lock(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="calsync.daemon")
    d = Running(tmp_path)
    fingerprint = await d.start()
    # Held while waiting, the lock would keep the cycle out: this would time out.
    result = await d.approve(rule="family", wait=True)
    assert result.data == {
        "rule": "family",
        "status": "approved",
        "fingerprint": fingerprint,
        "count": COUNT,
        "deleted": COUNT,
    }
    assert result.changed and result.summary == 'rule "family": 12 copies deleted'
    assert d.family_copies() == 0
    assert "rule family: 12 deletes approved (cli)" in caplog.messages
    with StateReader(tmp_path / "state.db") as reader:
        [row] = [c for c in reader.changes("family") if c.action == "approved"]
    assert (row.object_id, row.title) == (fingerprint, "12 deletes approved (cli)")
    await d.stop()


async def test_an_approval_is_consumed_by_one_cycle(tmp_path: Path) -> None:
    d = Running(tmp_path)
    fingerprint = await d.start()
    assert (await d.approve(rule="family", wait=True)).data["status"] == "approved"
    assert d.daemon._approvals == {}  # pyright: ignore[reportPrivateUsage]
    seed(d.world.fake)  # the same events again: the same copies, the same set to delete
    await d.cycle()
    assert d.family_copies() == COUNT
    for event_id in d.ids:
        d.world.fake.cancel_event(GCAL, event_id)
    await d.cycle()
    assert d.latest().status == "blocked" and d.family_copies() == COUNT  # approved once only
    [entry] = blocked_view(tmp_path, d.daemon.config).rules
    assert entry.fingerprint == fingerprint
    await d.stop()


async def test_approve_without_wait_returns_pending_and_the_cycle_follows(tmp_path: Path) -> None:
    d = Running(tmp_path)
    fingerprint = await d.start()
    before = d.runs()
    result = await d.approve(rule="family")
    assert result.data == {"rule": "family", "status": "pending", "fingerprint": fingerprint, "count": COUNT}
    assert result.changed and d.runs() == before  # answered before the cycle ran
    await eventually(lambda: d.runs() > before)
    assert (d.latest().status, d.latest().deleted) == ("ok", COUNT)
    await d.stop()


async def test_a_set_that_grew_before_the_cycle_is_stale(tmp_path: Path) -> None:
    d = Running(tmp_path)
    first = await d.start(cancel=COUNT - 1)
    d.world.on_google = lambda: d.world.fake.cancel_event(GCAL, d.ids[-1])  # as the cycle reads
    result = await d.approve(rule="family", wait=True)
    assert result.data["status"] == "stale" and not result.changed
    assert result.data["fingerprint"] not in (None, first)
    assert (result.data["count"], result.data["approved_fingerprint"]) == (COUNT, first)
    assert d.family_copies() == COUNT and d.latest().status == "blocked"
    [entry] = blocked_view(tmp_path, d.daemon.config).rules
    assert entry.fingerprint == result.data["fingerprint"]
    await d.stop()


async def test_an_approval_cycle_the_guard_no_longer_blocks_answers_synced(tmp_path: Path) -> None:
    """The set shrank under the guard before the cycle: the rule syncs as usual (exit 0)."""
    d = Running(tmp_path)
    await d.start()

    def shrink() -> None:
        seed(d.world.fake)  # the events are back, but two
        for event_id in d.ids[:2]:
            d.world.fake.cancel_event(GCAL, event_id)

    d.world.on_google = shrink
    result = await d.approve(rule="family", wait=True)
    assert result.data == {"rule": "family", "status": "synced", "deleted": 2}
    assert (
        result.changed
        and result.summary == 'rule "family" was no longer blocked; it synced as usual (2 copies deleted)'
    )
    with StateReader(tmp_path / "state.db") as reader:
        assert [c for c in reader.changes("family") if c.action == "approved"] == []  # not needed
    await d.stop()


async def test_a_fingerprint_that_is_not_the_current_one_is_stale_at_once(tmp_path: Path) -> None:
    d = Running(tmp_path)
    fingerprint = await d.start()
    before = d.runs()
    result = await d.approve(rule="family", fingerprint="0" * 16, wait=True)
    assert result.data == {
        "rule": "family",
        "status": "stale",
        "fingerprint": fingerprint,
        "count": COUNT,
        "approved_fingerprint": "0" * 16,
    }
    assert d.daemon._approvals == {}  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0.05)
    assert d.runs() == before  # no cycle requested
    await d.stop()


async def test_a_rule_that_is_not_blocked_stores_nothing(tmp_path: Path) -> None:
    d = Running(tmp_path)
    await d.start()
    result = await d.approve(rule="work", wait=True)
    assert result.data == {"rule": "work", "status": "not_blocked"}
    assert not result.changed and result.summary == 'rule "work" is not blocked: nothing to approve'
    assert d.daemon._approvals == {}  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ConfigError, match='unknown rule "nope"'):
        await d.approve(rule="nope")
    await d.stop()


async def test_a_failed_approval_cycle_answers_instead_of_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from calsync import daemon as daemon_module

    d = Running(tmp_path)
    await d.start()

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the state database is gone")

    monkeypatch.setattr(daemon_module, "run_cycle", broken)
    result = await d.approve(rule="family", wait=True)
    assert result.data["status"] == "failed" and not result.changed
    assert d.daemon._approvals == {}  # pyright: ignore[reportPrivateUsage]  # cleared all the same
    assert d.family_copies() == COUNT
    await d.stop()


async def test_a_rule_paused_before_its_approval_cycle_refuses_the_waiter(tmp_path: Path) -> None:
    from tests.test_daemon_reload import write

    d = Running(tmp_path)
    fingerprint = await d.start()
    daemon = d.daemon
    async with daemon._cycle_lock:  # pyright: ignore[reportPrivateUsage]  # no cycle meanwhile
        waiter = daemon.approve("family", fingerprint, COUNT, "cli", wait=True)
        assert waiter is not None
        write(d.path, TEXT.replace('name = "family"', 'name = "family"\npaused = true', 1))
        await daemon._check_config()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(waiter.exception(), ConfigError)
    assert str(waiter.exception()) == 'rule "family" was paused before its approved deletes ran'
    assert daemon._approvals == {}  # pyright: ignore[reportPrivateUsage]
    await d.stop()
    assert d.family_copies() == COUNT


async def test_an_approval_after_the_scheduler_stopped_is_refused(tmp_path: Path) -> None:
    """No cycle would ever consume it: refused, nothing stored, nothing logged as approved."""
    from calsync.ops import OpsUnavailable

    d = Running(tmp_path)
    fingerprint = await d.start()
    await d.stop()
    with pytest.raises(OpsUnavailable, match=r"The daemon is stopping\."):
        d.daemon.approve("family", fingerprint, COUNT, "cli", wait=False)
    assert d.daemon._approvals == {}  # pyright: ignore[reportPrivateUsage]
    assert d.family_copies() == COUNT


async def test_a_dry_run_stores_nothing(tmp_path: Path) -> None:
    d = Running(tmp_path)
    fingerprint = await d.start()
    result = await asyncio.wait_for(d.daemon.run_op("approve", {"rule": "family"}, True, source="cli"), 5)
    assert result.data == {
        "rule": "family",
        "status": "would_approve",
        "fingerprint": fingerprint,
        "count": COUNT,
        "would_change": True,
    }
    assert not result.changed and d.daemon._approvals == {}  # pyright: ignore[reportPrivateUsage]
    await d.stop()


async def test_approve_over_the_socket_from_the_cli(short_dir: Path) -> None:
    d = Running(short_dir, serve=True)
    fingerprint = await d.start()
    await eventually(lambda: (short_dir / "calsync.sock").is_socket())
    argv = ["--config", str(d.path), "--state-dir", str(short_dir), "approve", "family", "--yes", "--wait"]
    code, out = await anyio.to_thread.run_sync(lambda: cli([*argv, "--json"]))
    assert code == EXIT_OK, out
    body = json.loads(out)
    assert body["data"]["status"] == "approved" and body["data"]["fingerprint"] == fingerprint
    assert d.family_copies() == 0
    await d.stop()


# ----- the approve operation in-process ---------------------------------------------------


def cli(argv: list[str], stdin: io.StringIO | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, out=out, stdin=stdin or io.StringIO(""))
    return code, out.getvalue()


def test_approve_in_process_runs_one_cycle_for_that_rule(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World()
    world.fake.cancel_event(GCAL, "lunch")
    ids = seed(world.fake)
    monkeypatch.setattr(wiring, "build_adapters", world.factory)
    path = short_dir / "config.toml"
    path.write_text(TEXT)
    base = ["--config", str(path), "--state-dir", str(short_dir / "s")]
    assert cli([*base, "run"])[0] == EXIT_OK
    for event_id in ids:
        world.fake.cancel_event(GCAL, event_id)
    assert cli([*base, "run"])[0] == EXIT_FAILED  # blocked
    code, out = cli([*base, "approve", "family", "--yes"])
    assert (code, out) == (EXIT_OK, 'rule "family": 12 copies deleted\n')
    assert [h for h in world.target.objects if "/family/" in h] == []
    with StateReader(short_dir / "s" / "state.db") as reader:
        assert [r.status for r in reader.runs("family")] == ["ok", "blocked", "ok"]
        assert len(reader.runs("work")) == 2  # one cycle, for that rule only
    # Recorded with its origin, with no daemon and no push channel: `calsync changes` shows it.
    code, out = cli([*base, "changes", "--rule", "family", "--json"])
    rows = [c for c in json.loads(out)["changes"] if c["action"] == "approved"]
    assert code == EXIT_OK and [c["title"] for c in rows] == ["12 deletes approved (cli)"]
    code, out = cli([*base, "approve", "family", "--yes", "--json"])
    assert code == EXIT_CONFIG and json.loads(out)["data"]["status"] == "not_blocked"


# ----- the CLI: prompt, exit codes, `calsync blocked` ---------------------------------------


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class Files:
    """A config file and a state database whose latest run of ``family`` is blocked."""

    def __init__(self, root: Path) -> None:
        self.config = root / "config.toml"
        self.config.write_text(TEXT)
        self.state = root / "s"
        self.ids = [f"oid{i:02d}" for i in range(COUNT)]
        with Store(self.state / "state.db") as store:
            store.record_runs(
                [
                    RunRecord("work", NOW, NOW, "ok", 1, 0, 0, 0, 1, None),
                    RunRecord(
                        "family",
                        NOW,
                        NOW,
                        "blocked",
                        0,
                        0,
                        0,
                        0,
                        1,
                        "plan deletes 12 events, more than max_count=10",
                        error_kind="guard",
                    ),
                ],
                [
                    ChangeRecord("family", oid, "blocked", NOW, f"Event {i:02d}")
                    for i, oid in enumerate(self.ids)
                ],
            )
        self.fingerprint = delete_fingerprint(self.ids)

    def cli(self, *argv: str, stdin: io.StringIO | None = None) -> tuple[int, str]:
        return cli(["--config", str(self.config), "--state-dir", str(self.state), *argv], stdin)


@pytest.fixture
def files(tmp_path: Path) -> Files:
    return Files(tmp_path)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """``opsclient.call`` recorded; it answers ``approved`` unless a test sets ``answer``."""
    made: list[Any] = []

    def call(
        name: str, args: dict[str, Any], *, dry_run: bool, config_path: Path, state_dir: Path
    ) -> OpResult:
        made.append((name, args, dry_run))
        status = getattr(call, "answer", "approved")
        return OpResult(
            "approve", status in ("approved", "pending"), f"answer: {status}", data={"status": status}
        )

    monkeypatch.setattr(opsclient, "call", call)
    made.append(call)  # made[0]: the fake itself, to set ``answer``
    return made


def test_without_a_tty_approve_needs_yes(files: Files, calls: list[Any]) -> None:
    assert files.cli("approve", "family") == (EXIT_CONFIG, "pass --yes to approve without a prompt\n")
    assert calls[1:] == []


def test_json_needs_yes_even_on_a_tty(files: Files, calls: list[Any]) -> None:
    code, out = files.cli("approve", "family", "--json", stdin=Tty("family\n"))
    message = "pass --yes to approve without a prompt"
    assert code == EXIT_CONFIG
    assert json.loads(out) == {"error": {"kind": "config", "message": message, "problems": [message]}}
    assert calls[1:] == []


def test_on_a_tty_the_rule_name_confirms_the_shown_set(files: Files, calls: list[Any]) -> None:
    code, out = files.cli("approve", "family", stdin=Tty("family\n"))
    assert code == EXIT_OK
    lines = out.splitlines()
    assert lines[0] == "rule family: 12 copies would be deleted:"
    assert lines[1:11] == [f"  - Event {i:02d}" for i in range(10)]
    assert lines[11] == "  … and 2 more (calsync blocked --rule family)"
    assert lines[12].startswith("Type the rule name (family) to approve: ")
    assert lines[12].endswith("answer: approved")
    # The set the person saw, by fingerprint: a set that changed meanwhile comes back stale.
    assert calls[1:] == [("approve", {"rule": "family", "fingerprint": files.fingerprint}, False)]


def test_on_a_tty_anything_else_aborts(files: Files, calls: list[Any]) -> None:
    code, out = files.cli("approve", "family", stdin=Tty("yes\n"))
    assert code == EXIT_FAILED and out.endswith("not approved\n")
    assert calls[1:] == []


def test_on_a_tty_a_rule_that_is_not_blocked_is_answered_without_sending(
    files: Files, calls: list[Any]
) -> None:
    """Nothing was shown, so nothing is sent: never an approval of an unseen set."""
    code, out = files.cli("approve", "work", stdin=Tty("work\n"))
    assert (code, out) == (EXIT_CONFIG, 'rule "work" is not blocked: nothing to approve\n')
    assert calls[1:] == []


def test_on_a_tty_an_unrecorded_block_is_refused_without_sending(files: Files, calls: list[Any]) -> None:
    """Blocked again after an ok run, with the rows of the older block only: unknown set."""
    with Store(files.state / "state.db") as store:
        store.record_runs([RunRecord("family", NOW, NOW, "ok", 0, 0, 0, 0, 1, None)])
        store.record_runs(
            [RunRecord("family", NOW, NOW, "blocked", 0, 0, 0, 0, 1, "guard", error_kind="guard")]
        )
    code, out = files.cli("approve", "family", stdin=Tty("family\n"))
    assert code == EXIT_CONFIG and "its blocked set is no longer recorded" in out
    assert "would be deleted" not in out and calls[1:] == []


def test_on_a_tty_an_unknown_rule_is_refused_without_sending(files: Files, calls: list[Any]) -> None:
    code, out = files.cli("approve", "nope", stdin=Tty("nope\n"))
    assert code == EXIT_CONFIG and 'unknown rule "nope"' in out
    assert calls[1:] == []


def test_on_a_tty_a_paused_rule_is_refused_before_any_title(files: Files, calls: list[Any]) -> None:
    files.config.write_text(TEXT.replace('name = "family"', 'name = "family"\npaused = true', 1))
    stdin = Tty("family\n")
    code, out = files.cli("approve", "family", stdin=stdin)
    assert code == EXIT_CONFIG and 'rule "family" is paused; resume it first' in out
    assert "would be deleted" not in out and "Event" not in out and "Type the rule name" not in out
    assert stdin.tell() == 0  # nothing was read: no prompt
    assert calls[1:] == []


def test_yes_skips_the_prompt(files: Files, calls: list[Any]) -> None:
    assert files.cli("approve", "family", "--yes", "--wait") == (EXIT_OK, "answer: approved\n")
    assert files.cli("approve", "family", "--yes", "--fingerprint", "abc") == (EXIT_OK, "answer: approved\n")
    assert calls[1:] == [
        ("approve", {"rule": "family", "wait": True}, False),
        ("approve", {"rule": "family", "fingerprint": "abc"}, False),
    ]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("approved", EXIT_OK),
        ("pending", EXIT_OK),
        ("stale", EXIT_FAILED),
        ("failed", EXIT_FAILED),
        ("not_blocked", EXIT_CONFIG),
        ("synced", EXIT_OK),
    ],
)
def test_exit_codes(files: Files, calls: list[Any], status: str, code: int) -> None:
    calls[0].answer = status
    assert files.cli("approve", "family", "--yes")[0] == code
    assert files.cli("approve", "family", "--yes", "--json")[0] == code


def test_blocked_command(files: Files) -> None:
    code, out = files.cli("blocked")
    assert code == EXIT_OK
    lines = out.splitlines()
    assert lines[0] == f"rule=family since={NOW.isoformat()} count=12 fingerprint={files.fingerprint}"
    assert lines[1:13] == [f"  - Event {i:02d}" for i in range(COUNT)]
    assert lines[13] == "  approve with: calsync approve family"
    code, out = files.cli("blocked", "--json")
    [entry] = json.loads(out)["blocked"]
    assert (entry["rule"], entry["count"], entry["fingerprint"]) == ("family", COUNT, files.fingerprint)
    assert files.cli("blocked", "--rule", "work") == (EXIT_OK, "no rule is blocked\n")
    assert files.cli("blocked", "--rule", "nope") == (EXIT_CONFIG, "unknown rule(s): nope\n")


def test_status_points_to_blocked_and_approve(files: Files) -> None:
    _, out = files.cli("status")
    assert (
        "  blocked: 12 copies would be deleted — check `calsync blocked --rule family`, then approve if "
        "intended Run: calsync approve family\n"
    ) in out


# ----- the API ------------------------------------------------------------------------------


async def test_get_api_blocked(files: Files) -> None:
    import httpx

    config = configfile.validate_text(TEXT, files.config.parent)
    app = api.create_app(
        config_provider=lambda: config, state_dir=files.state, daemon=None, clock=lambda: NOW
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://calsync") as c:
        every = await c.get("/api/blocked")
        one = await c.get("/api/blocked", params={"rule": "work"})
        unknown = await c.get("/api/blocked", params={"rule": "nope"})
    assert every.status_code == 200 and every.json()["blocked"][0]["fingerprint"] == files.fingerprint
    assert one.json() == {"blocked": []}
    assert unknown.status_code == 404 and unknown.json()["error"]["kind"] == "unknown_rule"
    web_paths = [
        getattr(r, "path", "")
        for r in api.api_routes(config_provider=lambda: config, state_dir=files.state, daemon=None)
    ]
    assert "/api/blocked" in web_paths  # the web UI mounts the same GET routes
