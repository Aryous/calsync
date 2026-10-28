"""The daemon's hot reload, paused rules and rules that retire after deleting their copies.

Same fakes as ``test_daemon``: fake time (each wait is cut into polls of ``WATCH_INTERVAL``
seconds while a config file is watched), FakeGoogle and FakeTarget built by the injected
adapters factory. The config file lives in ``tmp_path``; edits to it are made from the fake
sleep's hooks, so they land between two polls.
"""

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from calsync import configfile
from calsync.config import Config
from calsync.daemon import WATCH_INTERVAL, RulePaused
from calsync.views import StateReader
from tests.factories import google_event
from tests.harness import GCAL, NOW
from tests.test_daemon import World, runs_by_time, until

pytestmark = pytest.mark.anyio

ACCOUNTS = """\
[google]
client_secret_file = "unused.json"

[runtime]
interval = "30s"
jitter = 0.0

[accounts.me]
provider = "google"
credential = { backend = "env", ref = "UNUSED" }

[accounts.dav]
provider = "caldav"
server_url = "http://unused/"
username = "u"
credential = { backend = "env", ref = "UNUSED" }
"""

FAMILY = """
[[rules]]
name = "family"  # the family calendar
source = { account = "me", calendar = "Family" }
target = { account = "dav", calendar = "Family", create_if_missing = true }
"""

WORK = """
[[rules]]
name = "work"
source = { account = "me", calendar = "Work" }
target = { account = "dav", calendar = "Work", create_if_missing = true }
"""

TEXT = ACCOUNTS + FAMILY + WORK


def write(path: Path, text: str) -> None:
    """Write ``text`` and move the modification time on, so the cheap stat check sees it even
    when the size happens to match."""
    before = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(text)
    os.utime(path, ns=(before + 1_000_000_000, before + 1_000_000_000))


def setup(tmp_path: Path, text: str = TEXT) -> tuple[Path, Config]:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path, configfile.validate_text(text, tmp_path)


def on_wait(world: World, n: int, action: Callable[[], None]) -> None:
    """Run ``action`` once, as the ``n``-th wait (poll) ends."""
    count = 0
    previous = world.after_sleep

    def hook() -> None:
        nonlocal count
        count += 1
        if previous is not None:
            previous()
        if count == n:
            action()

    world.after_sleep = hook


def history(state_dir: Path) -> list[tuple[str, str]]:
    with StateReader(state_dir / "state.db") as reader:
        return [(c.source, c.summary) for c in reversed(reader.config_changes())]


def rules_of(config: Config) -> list[str]:
    return [r.name for r in config.rules]


# ----- external edits ---------------------------------------------------------------------


async def test_an_external_edit_is_applied_within_one_poll(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="calsync.daemon")
    path, config = setup(tmp_path)
    world = World(stop_after=20)
    daemon = world.daemon(config, tmp_path, config_path=path)
    seen: list[list[str]] = []
    world.before_sleep = lambda: seen.append(rules_of(daemon.config))
    on_wait(world, 1, lambda: write(path, ACCOUNTS + FAMILY))  # "work" removed by hand
    await daemon.run(world.stop)
    assert world.delays[:2] == [WATCH_INTERVAL, WATCH_INTERVAL]
    assert seen[0] == ["family", "work"] and seen[1] == ["family"]  # applied at the first poll
    assert history(tmp_path) == [("external", "edited outside calsync")]
    with StateReader(tmp_path / "state.db") as reader:
        [change] = reader.config_changes()
    assert change.before == TEXT
    assert change.after_fingerprint == configfile.read_snapshot(path).fingerprint
    # The cycle at 30 s runs the new rule set only.
    assert runs_by_time(tmp_path) == [(0, "family"), (0, "work"), (30, "family")]
    assert "config reloaded (edited outside calsync)" in caplog.messages


async def test_an_invalid_edit_keeps_the_running_config_and_is_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="calsync.daemon")
    path, config = setup(tmp_path)
    world = World(stop_after=20)
    daemon = world.daemon(config, tmp_path, config_path=path)
    broken = TEXT.replace('interval = "30s"', 'interval = "soon"').replace(
        "jitter = 0.0", 'jitter = "nonsense"'
    )
    with pytest.raises(configfile.ConfigError) as problems:
        configfile.validate_text(broken, tmp_path)
    assert len(problems.value.problems) == 2
    on_wait(world, 1, lambda: write(path, broken))
    on_wait(world, 3, lambda: write(path, broken))  # touched again, same text
    await daemon.run(world.stop)
    assert rules_of(daemon.config) == ["family", "work"]
    failed = [m for m in caplog.messages if m.startswith("config reload failed")]
    assert failed == ["config reload failed (2 problems)"]  # counts only, once per text
    assert all("soon" not in m and "nonsense" not in m for m in caplog.messages)
    assert history(tmp_path) == []  # nothing recorded
    assert [r for _, r in runs_by_time(tmp_path)] == ["family", "work"] * 2  # still syncing both


async def test_a_fixed_edit_after_an_invalid_one_is_applied(tmp_path: Path) -> None:
    path, config = setup(tmp_path)
    world = World(stop_after=6)
    daemon = world.daemon(config, tmp_path, config_path=path)
    on_wait(world, 1, lambda: write(path, TEXT + "[[rules]]\n"))
    on_wait(world, 2, lambda: write(path, ACCOUNTS + WORK))
    await daemon.run(world.stop)
    assert rules_of(daemon.config) == ["work"]
    assert history(tmp_path) == [("external", "edited outside calsync")]


async def test_an_empty_config_file_keeps_the_running_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An editor that truncates before writing leaves an empty file for a moment."""
    caplog.set_level(logging.INFO, logger="calsync.daemon")
    path, config = setup(tmp_path)
    world = World(stop_after=6)
    daemon = world.daemon(config, tmp_path, config_path=path)
    on_wait(world, 1, lambda: write(path, ""))
    on_wait(world, 2, lambda: write(path, TEXT))  # the write completes
    await daemon.run(world.stop)
    assert rules_of(daemon.config) == ["family", "work"]
    assert [m for m in caplog.messages if m.startswith("config reload failed")] == [
        "config reload failed (1 problems)"
    ]
    assert world.builds == [{"dav"}, {"me"}]  # nothing closed or rebuilt
    assert history(tmp_path) == []


async def test_a_deleted_config_file_keeps_the_running_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="calsync.daemon")
    path, config = setup(tmp_path)
    world = World(stop_after=6)
    daemon = world.daemon(config, tmp_path, config_path=path)
    on_wait(world, 1, path.unlink)
    await daemon.run(world.stop)
    assert rules_of(daemon.config) == ["family", "work"]
    assert [m for m in caplog.messages if m.startswith("config reload failed")] == [
        "config reload failed (1 problems)"
    ]
    assert history(tmp_path) == []


async def test_the_config_is_checked_right_before_a_cycle(tmp_path: Path) -> None:
    """A request cuts the wait short before any poll: the cycle still runs the edited file."""
    path, config = setup(tmp_path)
    world = World(block=True)
    daemon = world.daemon(config, tmp_path, config_path=path)
    task = asyncio.create_task(daemon.run(world.stop))
    await until(lambda: len(world.delays) == 1)
    write(path, ACCOUNTS + FAMILY)
    daemon.request_cycle()
    await until(lambda: len(world.delays) == 2)
    world.stop.set()
    await asyncio.wait_for(task, 5)
    assert runs_by_time(tmp_path) == [(0, "family"), (0, "work"), (0, "family")]
    assert history(tmp_path) == [("external", "edited outside calsync")]


async def test_an_edit_during_a_cycle_waits_for_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing swaps the config under a running cycle, and the engine is kept, never rebuilt."""
    from calsync import daemon as daemon_module

    path, config = setup(tmp_path)
    world = World(stop_after=16)
    daemon = world.daemon(config, tmp_path, config_path=path)
    real = daemon_module.run_cycle
    during: list[tuple[list[str], list[str]]] = []
    engines: list[Any] = []

    async def cycle(engine: Any, store: Any, config: Config, *args: Any, **kwargs: Any) -> Any:
        engines.append(engine)
        if len(engines) == 1:
            write(path, ACCOUNTS + FAMILY)  # edited while the first cycle runs
        result = await real(engine, store, config, *args, **kwargs)
        during.append((rules_of(daemon.config), rules_of(engine._config)))  # pyright: ignore[reportPrivateUsage]
        return result

    monkeypatch.setattr(daemon_module, "run_cycle", cycle)
    await daemon.run(world.stop)
    # Unchanged until the cycle ended; then applied to the same engine, never a fresh one.
    assert during == [(["family", "work"], ["family", "work"]), (["family"], ["family"])]
    assert len(engines) == 2 and engines[1] is engines[0]


async def test_an_account_change_rebuilds_only_that_account(tmp_path: Path) -> None:
    path, config = setup(tmp_path)
    world = World(stop_after=4)
    daemon = world.daemon(config, tmp_path, config_path=path)
    edited = TEXT.replace('username = "u"', 'username = "v"')
    extra = '\n[accounts.spare]\nprovider = "google"\ncredential = { backend = "env", ref = "UNUSED" }\n'
    on_wait(world, 1, lambda: write(path, edited))
    on_wait(world, 2, lambda: write(path, edited.replace(FAMILY, "") + extra))  # an unused account
    await daemon.run(world.stop)
    assert world.builds == [{"dav"}, {"me"}, {"dav"}]
    assert history(tmp_path) == [("external", "edited outside calsync")] * 2


async def test_a_changed_web_address_needs_a_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="calsync.daemon")
    path, config = setup(tmp_path)
    world = World(stop_after=4)
    daemon = world.daemon(config, tmp_path, config_path=path, listen=("127.0.0.1", 8765))
    on_wait(world, 1, lambda: write(path, TEXT + '\n[web]\nlisten = "127.0.0.1:9100"\n'))
    on_wait(world, 2, lambda: write(path, TEXT + '\n[web]\nlisten = "off"\n'))
    await daemon.run(world.stop)
    assert "restart the daemon to listen on 127.0.0.1:9100" in caplog.messages
    assert "restart the daemon to turn the web UI off" in caplog.messages


async def test_no_restart_hint_for_what_the_daemon_already_does(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``service install --listen off``: a later ``[web] listen = "off"`` needs no restart, and
    an address the ``--listen`` override already serves neither."""
    caplog.set_level(logging.INFO, logger="calsync.daemon")
    path, config = setup(tmp_path)
    world = World(stop_after=4)
    daemon = world.daemon(config, tmp_path, config_path=path, listen=None)
    on_wait(world, 1, lambda: write(path, TEXT + '\n[web]\nlisten = "off"\n'))
    await daemon.run(world.stop)
    assert not [m for m in caplog.messages if m.startswith("restart the daemon")]
    caplog.clear()
    world = World(stop_after=4)
    write(path, TEXT)
    daemon = world.daemon(config, tmp_path, config_path=path, listen=("127.0.0.1", 9100))
    on_wait(world, 1, lambda: write(path, TEXT + '\n[web]\nlisten = "127.0.0.1:9100"\n'))
    await daemon.run(world.stop)
    assert not [m for m in caplog.messages if m.startswith("restart the daemon")]


# ----- the daemon's own writes ------------------------------------------------------------


async def test_an_own_write_is_applied_at_once_and_not_recorded_as_external(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="calsync.daemon")
    path, config = setup(tmp_path)
    world = World(stop_after=20)
    daemon = world.daemon(config, tmp_path, config_path=path)
    results: list[Any] = []
    applied: list[list[str]] = []

    async def pause() -> None:
        results.append(await daemon.run_op("rule.pause", {"name": "work"}, False, source="cli"))
        applied.append([r.name for r in daemon.config.rules if r.paused])

    tasks: list[asyncio.Task[None]] = []
    on_wait(world, 1, lambda: tasks.append(asyncio.ensure_future(pause())))
    await daemon.run(world.stop)
    await asyncio.gather(*tasks)
    assert results[0].changed and applied == [["work"]]  # applied when the op returned
    assert history(tmp_path) == [("cli", 'rule "work" paused')]  # one row: never seen as external
    assert "config reloaded (edited outside calsync)" not in caplog.messages
    assert runs_by_time(tmp_path) == [(0, "family"), (0, "work"), (30, "family")]


async def test_an_own_write_that_fails_to_apply_is_retried_by_the_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``_apply`` rolled back: the file no longer holds what runs, so the next poll sees it."""
    from calsync.daemon import Daemon

    caplog.set_level(logging.INFO, logger="calsync.daemon")
    path, config = setup(tmp_path)
    world = World(stop_after=20)
    daemon = world.daemon(config, tmp_path, config_path=path)
    real = Daemon._apply  # pyright: ignore[reportPrivateUsage]
    attempts: list[int] = []

    async def apply_once_broken(self: Daemon, config: Config) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("a bug")
        await real(self, config)

    monkeypatch.setattr(Daemon, "_apply", apply_once_broken)
    after_op: list[list[str]] = []

    async def pause() -> None:
        await daemon.run_op("rule.pause", {"name": "work"}, False, source="cli")
        after_op.append([r.name for r in daemon.config.rules if r.paused])

    tasks: list[asyncio.Task[None]] = []
    on_wait(world, 1, lambda: tasks.append(asyncio.ensure_future(pause())))
    await daemon.run(world.stop)
    await asyncio.gather(*tasks)
    assert after_op == [[]]  # the failed apply kept the running config
    assert caplog.messages.count("config reload failed (RuntimeError)") == 1
    assert len(attempts) == 2  # retried by the watcher at its next poll
    assert [r.name for r in daemon.config.rules if r.paused] == ["work"]


async def test_an_external_edit_is_recorded_before_an_op_that_follows_it(tmp_path: Path) -> None:
    path, config = setup(tmp_path)
    world = World(block=True)
    daemon = world.daemon(config, tmp_path, config_path=path)
    task = asyncio.create_task(daemon.run(world.stop))
    await until(lambda: len(world.delays) == 1)
    write(path, TEXT.replace('username = "u"', 'username = "v"'))
    await daemon.run_op("rule.pause", {"name": "work"}, False, source="cli")
    world.stop.set()
    await asyncio.wait_for(task, 5)
    assert history(tmp_path) == [("external", "edited outside calsync"), ("cli", 'rule "work" paused')]


# ----- paused rules -----------------------------------------------------------------------


async def test_a_paused_rule_is_never_selected_and_cannot_be_triggered(tmp_path: Path) -> None:
    path, config = setup(
        tmp_path, ACCOUNTS + FAMILY + WORK.replace('name = "work"', 'name = "work"\npaused = true')
    )
    world = World(block=True)
    daemon = world.daemon(config, tmp_path, config_path=path)
    task = asyncio.create_task(daemon.run(world.stop))
    await until(lambda: len(world.delays) == 1)
    with pytest.raises(RulePaused, match='rule "work" is paused'):
        daemon.request_cycle("work")
    assert isinstance(RulePaused("x"), ValueError)
    daemon.request_cycle()  # every rule: the unpaused ones
    await until(lambda: len(world.delays) == 2)
    world.stop.set()
    await asyncio.wait_for(task, 5)
    assert runs_by_time(tmp_path) == [(0, "family"), (0, "family")]  # no run rows while paused


async def test_resuming_delivers_what_changed_while_paused(tmp_path: Path) -> None:
    path, config = setup(
        tmp_path, ACCOUNTS + FAMILY.replace('name = "family"', 'name = "family"\npaused = true')
    )
    world = World(stop_after=16)
    daemon = world.daemon(config, tmp_path, config_path=path)
    on_wait(world, 1, lambda: write(path, ACCOUNTS + FAMILY))  # resumed by hand
    await daemon.run(world.stop)
    assert world.target.objects  # the timed cycle after the resume wrote the copy
    assert runs_by_time(tmp_path) == [(30, "family")]  # nothing ran (or was recorded) while paused


# ----- retiring rules ---------------------------------------------------------------------


def retiring(text: str, name: str = "family") -> str:
    return text.replace(f'name = "{name}"', f'name = "{name}"\nretire = true', 1)


async def test_a_retiring_rule_deletes_its_copies_then_leaves_the_file(tmp_path: Path) -> None:
    path, config = setup(tmp_path)
    world = World(stop_after=35)
    daemon = world.daemon(config, tmp_path, config_path=path)
    on_wait(world, 1, lambda: write(path, retiring(TEXT)))
    await daemon.run(world.stop)
    family = [h for h in world.target.objects if "/family/" in h]
    assert family == [] and world.target.objects  # the work copy stays
    text = path.read_text()
    assert 'name = "family"' not in text and 'name = "work"' in text
    assert "# the family calendar" not in text and "[accounts.dav]" in text
    assert history(tmp_path) == [
        ("external", "edited outside calsync"),
        ("daemon", 'rule "family" removed after its copies were deleted'),
    ]
    assert rules_of(daemon.config) == ["work"]
    with StateReader(tmp_path / "state.db") as reader:
        family_runs = [r for r in reader.runs("family")]
    assert [(r.status, r.deleted) for r in reversed(family_runs)] == [("ok", 0), ("ok", 1)]
    assert runs_by_time(tmp_path)[-2:] == [(30, "work"), (60, "work")]  # the removed rule runs no more


async def test_a_retiring_rule_over_the_guard_blocks_and_stays(tmp_path: Path) -> None:
    path, config = setup(tmp_path)
    world = World(stop_after=20)
    for i in range(3):
        world.fake.put_event(
            GCAL,
            google_event(f"x{i}", f"Extra {i}", "2026-10-27T12:00:00+08:00", "2026-10-27T13:00:00+08:00"),
        )
    daemon = world.daemon(config, tmp_path, config_path=path)
    on_wait(world, 1, lambda: write(path, retiring(TEXT)))
    await daemon.run(world.stop)
    assert len([h for h in world.target.objects if "/family/" in h]) == 4
    assert 'name = "family"' in path.read_text()
    assert history(tmp_path) == [("external", "edited outside calsync")]
    with StateReader(tmp_path / "state.db") as reader:
        assert reader.runs("family")[0].status == "blocked"


async def test_a_failed_removal_is_retried_after_the_next_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from calsync import ops

    caplog.set_level(logging.INFO, logger="calsync")
    path, config = setup(tmp_path, retiring(TEXT))
    world = World(stop_after=40)
    real = ops.run_op
    attempts: list[str] = []

    def flaky(ctx: Any, name: str, args: Any, dry_run: bool) -> Any:
        attempts.append(name)
        if len(attempts) == 1:
            raise configfile.ConfigConflict()
        return real(ctx, name, args, dry_run)

    monkeypatch.setattr(ops, "run_op", flaky)
    daemon = world.daemon(config, tmp_path, config_path=path)
    await daemon.run(world.stop)
    assert attempts == ["rule.remove", "rule.remove"]
    assert "rule family retired, but removing it from the config failed (ConfigConflict)" in caplog.messages
    assert 'name = "family"' not in path.read_text()
    assert history(tmp_path) == [("daemon", 'rule "family" removed after its copies were deleted')]


async def test_an_account_that_fails_to_set_up_fails_only_its_rules(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="calsync.daemon")
    path, config = setup(tmp_path)
    world = World(stop_after=16)
    real = world.factory

    async def factory(config: Config, names: set[str], **kwargs: Any) -> Any:
        if len(world.builds) >= 2 and names == {"dav"}:
            world.builds.append(set(names))
            raise RuntimeError("boom")
        return await real(config, names, **kwargs)

    daemon = world.daemon(config, tmp_path, config_path=path, adapters_factory=factory)
    on_wait(world, 1, lambda: write(path, TEXT.replace('username = "u"', 'username = "v"')))
    await daemon.run(world.stop)  # the daemon keeps running
    assert "setting up account dav failed (RuntimeError)" in caplog.messages
    with StateReader(tmp_path / "state.db") as reader:
        latest = {r.rule: r for r in reversed(reader.runs())}
    assert {r.status for r in latest.values()} == {"failed"}  # both rules write to dav
    assert history(tmp_path) == [("external", "edited outside calsync")]


async def test_a_retiring_rule_waits_while_a_changed_copy_could_not_be_read(tmp_path: Path) -> None:
    """Detection that held a changed copy back (unread) is no proof that no copy is left."""
    path, config = setup(tmp_path)
    world = World(stop_after=50)
    daemon = world.daemon(config, tmp_path, config_path=path)

    def retire_and_touch() -> None:
        write(path, retiring(TEXT))
        [href] = [h for h in world.target.objects if "/family/" in h]
        world.target.put(href, world.target.objects[href][1])  # changed on the target: a new etag
        world.target.hide_once.add(href)  # and left out of the next read

    on_wait(world, 1, retire_and_touch)
    await daemon.run(world.stop)
    assert [h for h in world.target.objects if "/family/" in h] == []
    with StateReader(tmp_path / "state.db") as reader:
        family_runs = [
            (int((r.started_at - NOW).total_seconds()), r.status, r.deleted) for r in reader.runs("family")
        ]
        removal = reader.config_changes()[0]
    # 30 s: the copy could not be read (nothing to delete, but not finished); 60 s: deleted.
    assert list(reversed(family_runs)) == [(0, "ok", 0), (30, "ok", 0), (60, "ok", 1)]
    assert (removal.source, int((removal.at - NOW).total_seconds())) == ("daemon", 60)


async def test_a_hand_edit_during_the_retiring_cycle_waits_for_the_next_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evidence is the cycle's: a rule whose target was edited meanwhile is judged again."""
    from calsync import daemon as daemon_module

    path, config = setup(tmp_path)
    world = World(stop_after=50)
    daemon = world.daemon(config, tmp_path, config_path=path)
    on_wait(world, 1, lambda: write(path, retiring(TEXT)))
    real = daemon_module.run_cycle
    calls = 0
    elsewhere = retiring(TEXT).replace(
        'calendar = "Family", create_if_missing', 'calendar = "Elsewhere", create_if_missing'
    )

    async def cycle(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        result = await real(*args, **kwargs)
        if calls == 2:  # the retiring cycle deleted the copy; meanwhile the target is edited
            write(path, elsewhere)
        return result

    monkeypatch.setattr(daemon_module, "run_cycle", cycle)
    await daemon.run(world.stop)
    with StateReader(tmp_path / "state.db") as reader:
        changes = [(c.source, int((c.at - NOW).total_seconds())) for c in reversed(reader.config_changes())]
    assert changes == [("external", 2), ("external", 30), ("daemon", 60)]
    assert "Elsewhere" not in world.target.calendars  # a retiring rule creates no calendar


async def test_a_retiring_rule_never_creates_its_target_calendar(tmp_path: Path) -> None:
    """A missing target calendar holds no copies: the rule retires without an MKCALENDAR."""
    path, config = setup(tmp_path, retiring(TEXT))
    world = World(stop_after=5)
    daemon = world.daemon(config, tmp_path, config_path=path)
    await daemon.run(world.stop)
    assert "Family" not in world.target.calendars and "Work" in world.target.calendars
    assert 'name = "family"' not in path.read_text()
    assert history(tmp_path) == [("daemon", 'rule "family" removed after its copies were deleted')]
    with StateReader(tmp_path / "state.db") as reader:
        assert [(r.status, r.deleted) for r in reader.runs("family")] == [("ok", 0)]


async def test_a_discovery_404_never_retires_a_rule_whose_copies_remain(tmp_path: Path) -> None:
    """Only a listing without the calendar means "no copies left": a 404 on the way to the
    listing (principal, calendar home) fails the rule, which stays in the file."""
    from calsync.errors import NotFoundError
    from calsync.naming import resource_name

    path, config = setup(tmp_path, retiring(TEXT))
    world = World(stop_after=5)
    cal = world.target.add_calendar("Family")
    copy = cal.href + resource_name("family", "c" * 24)
    world.target.put(copy, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    world.target.fail_discovery = NotFoundError("/dav/home/")
    daemon = world.daemon(config, tmp_path, config_path=path)
    await daemon.run(world.stop)
    assert 'name = "family"' in path.read_text()
    assert rules_of(daemon.config) == ["family", "work"]
    assert history(tmp_path) == []
    assert copy in world.target.objects
    with StateReader(tmp_path / "state.db") as reader:
        assert {r.status for r in reader.runs("family")} == {"failed"}


async def test_a_reload_that_fails_keeps_the_running_config_and_the_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from calsync.engine import Engine

    caplog.set_level(logging.INFO, logger="calsync.daemon")
    path, config = setup(tmp_path)
    world = World(stop_after=20)
    daemon = world.daemon(config, tmp_path, config_path=path)

    def boom(self: Engine, config: Config) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(Engine, "replace_config", boom)
    on_wait(world, 1, lambda: write(path, ACCOUNTS + FAMILY))
    await daemon.run(world.stop)  # no exception: the scheduler lives on
    assert "config reload failed (RuntimeError)" in caplog.messages
    assert caplog.messages.count("config reload failed (RuntimeError)") == 1
    assert rules_of(daemon.config) == ["family", "work"]
    assert runs_by_time(tmp_path) == [(0, "family"), (0, "work"), (30, "family"), (30, "work")]
    assert history(tmp_path) == []
