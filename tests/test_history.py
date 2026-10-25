"""Run and change history recorded by the engine (FakeGoogle + FakeTarget)."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from calsync.naming import object_id
from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.fakes.target import FakeTarget
from tests.harness import GCAL, FakeHarness, build_fake, config_for, rule

pytestmark = pytest.mark.anyio

Row = tuple[str, str, str | None]  # (object id, action, title)


def oid(source_id: str) -> str:
    return object_id("family", GCAL, source_id)


def standup_and_lunch() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(
        GCAL, google_event("standup", "Standup", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00")
    )
    fake.put_event(
        GCAL, google_event("lunch", "Lunch", "2026-10-26T12:00:00+08:00", "2026-10-26T13:00:00+08:00")
    )
    return fake


def changes(tmp_path: Path, after: int = 0) -> list[Row]:
    """Change rows with id > ``after``, sorted (writes within a phase run concurrently)."""
    with closing(sqlite3.connect(tmp_path / "state.db")) as conn:
        rows = conn.execute("SELECT object_id, action, title FROM change WHERE id > ?", (after,)).fetchall()
    return sorted(rows, key=lambda r: (r[0], r[1]))


def last_id(tmp_path: Path) -> int:
    with closing(sqlite3.connect(tmp_path / "state.db")) as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM change").fetchone()[0]


def change_run_ids_match(tmp_path: Path) -> bool:
    """Every change points at a run of its own rule."""
    with closing(sqlite3.connect(tmp_path / "state.db")) as conn:
        bad = conn.execute(
            "SELECT COUNT(*) FROM change c LEFT JOIN run r ON r.id = c.run_id WHERE r.rule IS NOT c.rule"
        ).fetchone()[0]
    return bad == 0


async def test_creates_and_updates_are_recorded_with_their_titles(tmp_path: Path) -> None:
    fake = standup_and_lunch()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    assert changes(tmp_path) == sorted(
        [(oid("lunch"), "created", "Lunch"), (oid("standup"), "created", "Standup")]
    )
    mark = last_id(tmp_path)
    fake.put_event(
        GCAL,
        google_event("standup", "Weekly review", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00"),
    )
    await h.engine.cycle()
    assert changes(tmp_path, mark) == [(oid("standup"), "updated", "Weekly review")]
    assert change_run_ids_match(tmp_path)


async def test_idle_cycle_records_no_change(tmp_path: Path) -> None:
    h = build_fake(tmp_path, standup_and_lunch(), config_for([rule()]))
    await h.engine.cycle()
    mark = last_id(tmp_path)
    await h.engine.cycle()
    assert changes(tmp_path, mark) == []


async def test_dry_run_records_no_change(tmp_path: Path) -> None:
    fake = standup_and_lunch()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    mark = last_id(tmp_path)
    fake.put_event(
        GCAL,
        google_event("standup", "Weekly review", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00"),
    )
    res = (await h.engine.cycle(dry_run=True)).rules["family"]
    assert res.plan is not None and len(res.plan.update) == 1  # planned, not written
    assert changes(tmp_path, mark) == []
    await h.engine.cycle()
    assert changes(tmp_path, mark) == [(oid("standup"), "updated", "Weekly review")]


async def test_target_edit_written_back_is_recorded_as_reverted(tmp_path: Path) -> None:
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, standup_and_lunch(), config_for([rule()]), target=target)
    await h.engine.cycle()
    await h.engine.cycle()
    mark = last_id(tmp_path)
    href = next(
        h for h, data in target.managed(target.calendars["Family"]).items() if b"SUMMARY:Lunch" in data
    )
    target.put(href, target.objects[href][1].replace(b"SUMMARY:Lunch", b"SUMMARY:Lunch MOVED"))
    res = (await h.engine.cycle()).rules["family"]
    assert res.reverted == 1
    assert changes(tmp_path, mark) == [(oid("lunch"), "reverted", "Lunch")]
    (run,) = h.store.latest_runs()
    assert run.reverted == 1 and run.updated == 1


async def test_delete_carries_the_last_known_title(tmp_path: Path) -> None:
    fake = standup_and_lunch()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    fake.put_event(
        GCAL,
        google_event("standup", "Weekly review", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00"),
    )
    await h.engine.cycle()
    mark = last_id(tmp_path)
    fake.cancel_event(GCAL, "standup")
    res = (await h.engine.cycle()).rules["family"]
    assert res.deleted == 1
    assert changes(tmp_path, mark) == [(oid("standup"), "deleted", "Weekly review")]


async def test_protected_series_is_recorded_once(tmp_path: Path) -> None:
    fake = standup_and_lunch()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    mark = last_id(tmp_path)
    broken = google_event("lunch", "Lunch", "2026-10-26T12:00:00+08:00", "2026-10-26T13:00:00+08:00")
    del broken["start"]
    fake.put_event(GCAL, broken)
    assert (await h.engine.cycle(dry_run=True)).rules["family"].protected == 1  # a dry run records nothing...
    assert changes(tmp_path, mark) == []
    for _ in range(3):
        assert (await h.engine.cycle()).rules["family"].protected == 1
    # ...and does not use up the first record. Unparsed: no title is known.
    assert changes(tmp_path, mark) == [(oid("lunch"), "protected", None)]
    (run,) = h.store.latest_runs()
    assert run.protected == 1


def four() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for i in range(4):
        fake.put_event(
            GCAL,
            google_event(f"e{i}", f"Standup {i}", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00"),
        )
    return fake


async def test_blocked_deletes_are_recorded_once(tmp_path: Path) -> None:
    fake = four()
    h: FakeHarness = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    mark = last_id(tmp_path)
    for i in range(4):
        fake.cancel_event(GCAL, f"e{i}")
    assert (await h.engine.cycle(dry_run=True)).rules["family"].status == "blocked"
    for _ in range(2):
        assert (await h.engine.cycle()).rules["family"].status == "blocked"
    expected = sorted((oid(f"e{i}"), "blocked", f"Standup {i}") for i in range(4))
    assert changes(tmp_path, mark) == expected
    (run,) = h.store.latest_runs()
    assert run.status == "blocked" and run.error_kind == "guard"
    mark = last_id(tmp_path)
    res = (await h.engine.cycle(allow_deletes=True)).rules["family"]
    assert res.deleted == 4
    assert changes(tmp_path, mark) == sorted((oid(f"e{i}"), "deleted", f"Standup {i}") for i in range(4))
    assert change_run_ids_match(tmp_path)


async def test_blocked_delete_with_an_unreadable_body_is_recorded_untitled(tmp_path: Path) -> None:
    fake = four()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    mark = last_id(tmp_path)
    garbled = sorted(h.target.managed(h.target.calendars["Family"]))[0]
    lost = next(i for i in range(4) if f"SUMMARY:Standup {i}".encode() in h.target.objects[garbled][1])
    h.target.put(garbled, b"not icalendar")
    for i in range(4):
        fake.cancel_event(GCAL, f"e{i}")
    res = (await h.engine.cycle()).rules["family"]
    assert res.status == "blocked" and not res.blocked_titles_complete
    # Each row carries the summary as read; only the unreadable body has no title.
    assert changes(tmp_path, mark) == sorted(
        (oid(f"e{i}"), "blocked", None if i == lost else f"Standup {i}") for i in range(4)
    )


async def test_empty_titles_are_recorded_as_none(tmp_path: Path) -> None:
    fake = four()
    fake.put_event(GCAL, google_event("e0", "", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00"))
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    created = {r[0]: r[2] for r in changes(tmp_path) if r[1] == "created"}
    assert created[oid("e0")] is None  # neither "" nor a placeholder
    mark = last_id(tmp_path)
    for i in range(4):
        fake.cancel_event(GCAL, f"e{i}")
    res = (await h.engine.cycle()).rules["family"]
    assert res.status == "blocked" and "(untitled)" in res.blocked_titles  # the alert keeps its placeholder
    assert changes(tmp_path, mark) == sorted(
        (oid(f"e{i}"), "blocked", None if i == 0 else f"Standup {i}") for i in range(4)
    )


async def test_only_newly_protected_ids_are_recorded(tmp_path: Path) -> None:
    fake = standup_and_lunch()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    mark = last_id(tmp_path)
    for source_id, title, start, end in (
        ("lunch", "Lunch", "2026-10-26T12:00:00+08:00", "2026-10-26T13:00:00+08:00"),
        ("standup", "Standup", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00"),
    ):
        broken = google_event(source_id, title, start, end)
        del broken["start"]
        fake.put_event(GCAL, broken)
        await h.engine.cycle()
        await h.engine.cycle()
    # The set grew from {lunch} to {lunch, standup}: standup is new, lunch is not recorded again.
    assert changes(tmp_path, mark) == [(oid("lunch"), "protected", None), (oid("standup"), "protected", None)]
    assert (await h.engine.cycle()).rules["family"].protected == 2


async def test_a_failed_history_write_records_the_rows_next_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = four()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    mark = last_id(tmp_path)
    runs = h.store.latest_runs()
    broken = google_event("e0", "Standup 0", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00")
    del broken["start"]
    fake.put_event(GCAL, broken)
    for i in range(1, 4):
        fake.cancel_event(GCAL, f"e{i}")  # 3 of 3 deletable copies: the guard blocks

    real = h.store.record_runs
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("disk I/O error")
        real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(h.store, "record_runs", fail_once)
    with pytest.raises(sqlite3.OperationalError):
        await h.engine.cycle()
    assert changes(tmp_path, mark) == [] and h.store.latest_runs() == runs
    assert (await h.engine.cycle()).rules["family"].status == "blocked"
    assert changes(tmp_path, mark) == sorted(
        [(oid("e0"), "protected", None)] + [(oid(f"e{i}"), "blocked", f"Standup {i}") for i in range(1, 4)]
    )


async def test_out_of_window_count_is_recorded(tmp_path: Path) -> None:
    fake = standup_and_lunch()
    fake.put_event(
        GCAL, google_event("old", "Lunch", "2025-01-06T12:00:00+08:00", "2025-01-06T13:00:00+08:00")
    )
    h = build_fake(tmp_path, fake, config_for([rule(window={"past": "30d", "future": "30d"})]))
    await h.engine.cycle()
    (run,) = h.store.latest_runs()
    assert run.out_of_window == 1 and run.created == 2
