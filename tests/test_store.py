import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from calsync import store as store_mod
from calsync.mirror import apply_pull
from calsync.sources.base import CalendarPull
from calsync.store import RunRecord, Store
from calsync.targets.base import CachedObject, TargetCache, TargetCalendar

T0 = datetime(2026, 10, 24, tzinfo=UTC)
T1 = datetime(2026, 10, 25, tzinfo=UTC)


def test_apply_pull() -> None:
    mirror = {"old": {"id": "old"}}
    apply_pull(mirror, CalendarPull("c", full=False, upserts=[{"id": "a"}], deletes=["old"]))
    assert mirror == {"a": {"id": "a"}}
    apply_pull(mirror, CalendarPull("c", full=True, upserts=[{"id": "b"}]))
    assert mirror == {"b": {"id": "b"}}


def test_source_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "state" / "state.db"
    with Store(db) as store:
        store.save_source(
            "g",
            {"calendars_token": "t1"},
            [
                CalendarPull("cal", full=True, upserts=[{"id": "a", "summary": "日程"}, {"id": "b"}]),
            ],
        )
        store.save_source(
            "g",
            {"calendars_token": "t2"},
            [
                CalendarPull("cal", full=False, upserts=[{"id": "c"}], deletes=["a"]),
            ],
        )
    with Store(db) as store:
        state, mirrors = store.load_source("g")
        assert state == {"calendars_token": "t2"}
        assert sorted(mirrors["cal"]) == ["b", "c"]
        assert store.load_source("other") == ({}, {})


def test_target_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cal = TargetCalendar("/u/cal/", "Family", "sync")
    cache = TargetCache(
        cursor="tok1",
        objects={
            "/u/cal/x.ics": CachedObject('"e1"', "tag", "oid", "hash", (T0, T1)),
            "/u/cal/y.ics": CachedObject(None, "tag", "oid2", None, None),
        },
    )
    with Store(db) as store:
        assert store.load_target_calendar("d", "Family") is None
        store.save_target_calendar("d", "Family", cal)
        store.save_target_cache("d", cal.href, cache)
    with Store(db) as store:
        assert store.load_target_calendar("d", "Family") == cal
        loaded = store.load_target_cache("d", cal.href)
        assert loaded.cursor == "tok1" and loaded.objects == cache.objects
        store.save_target_calendar("d", "Family", TargetCalendar("/u/other/", "Family", "sync"))
        assert store.load_target_cache("d", "/u/other/").cursor is None  # moved: cursor reset
        store.forget_target_calendar("d", "Family")
        assert store.load_target_calendar("d", "Family") is None


def test_runs(tmp_path: Path) -> None:
    with Store(tmp_path / "s.db") as store:
        store.record_runs([RunRecord("a", T0, T0, "failed", 0, 0, 0, 0, 3, "boom")])
        store.record_runs(
            [
                RunRecord("a", T1, T1, "ok", 1, 0, 0, 0, 4, None),
                RunRecord("b", T1, T1, "ok", 0, 0, 0, 0, 4, None),
            ]
        )
        latest = store.latest_runs()
    assert [(r.rule, r.status, r.requests) for r in latest] == [("a", "ok", 4), ("b", "ok", 4)]


def test_schema_version_mismatch_rebuilds(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "s.db"
    with Store(db) as store:
        store.save_source("g", {"x": 1}, [])
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", store_mod.SCHEMA_VERSION + 1)
    with Store(db) as store:
        assert store.load_source("g") == ({}, {})
    version = sqlite3.connect(db).execute("SELECT value FROM meta").fetchone()[0]
    assert int(version) == store_mod.SCHEMA_VERSION
