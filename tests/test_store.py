import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from calsync import store as store_mod
from calsync.mirror import apply_pull
from calsync.sources.base import CalendarPull
from calsync.store import ChangeRecord, RunRecord, Store
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
            # read but not yet classified / classified as edited on the target, not yet written back
            "/u/cal/z.ics": CachedObject('"e3"', "tag", "oid3", "hash", None, verify_digest="d1"),
            "/u/cal/w.ics": CachedObject('"e4"', "tag", "oid4", None, (T0, T1), reverting=True),
            # rewrite-loop breaker: recent reverts and a hold
            "/u/cal/v.ics": CachedObject(
                '"e5"', "tag", "oid5", "hash", (T0, T1), revert_times=(T0, T1), hold_until=T1
            ),
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
    with closing(sqlite3.connect(db)) as conn:
        version = conn.execute("SELECT value FROM meta").fetchone()[0]
    assert int(version) == store_mod.SCHEMA_VERSION


def _count(db: Path, sql: str) -> int:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql).fetchone()[0]


def test_disk_full_error_propagates_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    with Store(db) as store:
        store.save_source("g", {"x": 1}, [CalendarPull("cal", full=True, upserts=[{"id": "a"}])])
        pages = store._db.execute("PRAGMA page_count").fetchone()[0]
        store._db.execute(f"PRAGMA max_page_count = {pages + 2}")
        big = [{"id": str(i), "blob": "x" * 2000} for i in range(500)]
        with pytest.raises(sqlite3.OperationalError, match="full"):
            store.save_source("g", {"x": 2}, [CalendarPull("cal", full=False, upserts=big)])
        store.save_source("g", {"x": 3}, [CalendarPull("cal", full=False, deletes=["a"])])
        state, mirrors = store.load_source("g")
        assert state == {"x": 3} and mirrors == {}


def test_save_source_is_atomic(tmp_path: Path) -> None:
    with Store(tmp_path / "s.db") as store:
        store.save_source("g", {"v": 1}, [CalendarPull("cal", full=True, upserts=[{"id": "a"}])])
        bad = CalendarPull("cal", full=True, upserts=[{"id": "b"}, {"nope": 1}])
        with pytest.raises(KeyError):
            store.save_source("g", {"v": 2}, [bad])
        state, mirrors = store.load_source("g")
        assert state == {"v": 1} and list(mirrors["cal"]) == ["a"]


def test_db_files_are_private(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        db = tmp_path / "s.db"
        with Store(db) as store:
            store.save_source("g", {}, [])
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(db) + suffix)
                if path.exists():
                    assert path.stat().st_mode & 0o777 == 0o600, suffix
    finally:
        os.umask(old)


def test_run_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_mod, "RUN_RETENTION", 2)
    db = tmp_path / "s.db"
    with Store(db) as store:
        for _ in range(4):
            store.record_runs([RunRecord("a", T0, T0, "ok", 0, 0, 0, 0, 1, None)])
        store.record_runs([RunRecord("b", T0, T0, "ok", 0, 0, 0, 0, 1, None)])
    assert _count(db, "SELECT COUNT(*) FROM run WHERE rule = 'a'") == 2
    assert _count(db, "SELECT COUNT(*) FROM run WHERE rule = 'b'") == 1


def test_target_objects_dropped_with_calendar(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    cache = TargetCache(cursor="c", objects={"/a/x.ics": CachedObject(None, "t", "o", None, None)})
    with Store(db) as store:
        store.save_target_calendar("d", "F", TargetCalendar("/a/", "F", "sync"))
        store.save_target_cache("d", "/a/", cache)
        store.save_target_calendar("d", "F", TargetCalendar("/b/", "F", "sync"))
        assert _count(db, "SELECT COUNT(*) FROM target_object") == 0
        store.save_target_calendar("d", "F", TargetCalendar("/b/", "F", "sync"))
        store.save_target_cache("d", "/b/", cache)
        assert _count(db, "SELECT COUNT(*) FROM target_object") == 1
        store.forget_target_calendar("d", "F")
        assert _count(db, "SELECT COUNT(*) FROM target_object") == 0


def test_non_integer_schema_version_rebuilds(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    with Store(db) as store:
        store.save_source("g", {"x": 1}, [])
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE meta SET value = 'abc'")
        conn.commit()
    with Store(db) as store:
        assert store.load_source("g") == ({}, {})


def test_corrupt_file_is_quarantined(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    db.write_bytes(b"this is definitely not a sqlite database" * 50)
    with Store(db) as store:
        store.save_source("g", {"x": 1}, [])
        assert store.load_source("g")[0] == {"x": 1}
    assert len(list(tmp_path.glob("s.db.corrupt-*"))) == 1


@pytest.mark.parametrize("how", ["moved", "forgotten"])
def test_dropping_objects_resets_every_sibling_ref_cursor(tmp_path: Path, how: str) -> None:
    """No ref may keep a cursor for an href whose objects are gone: the next load is a cold start."""
    db = tmp_path / "s.db"
    a = TargetCalendar("https://x/cal/a/", "A", "sync")
    with Store(db) as store:
        store.save_target_calendar("dav", "A", a)  # one calendar, referenced by name...
        store.save_target_calendar("dav", a.href, a)  # ...and by href
        objects = {f"{a.href}calsync-x.ics": CachedObject('"e"', "t", "o", "h", None)}
        store.save_target_cache("dav", a.href, TargetCache("tok-1", objects))
        if how == "moved":  # ref "A" now resolves elsewhere (renamed / recreated)
            store.save_target_calendar("dav", "A", TargetCalendar("https://x/cal/b/", "B", "sync"))
        else:
            store.forget_target_calendar("dav", "A")
        assert _count(db, "SELECT COUNT(*) FROM target_calendar WHERE cursor IS NOT NULL") == 0
        again = store.load_target_cache("dav", a.href)
        assert again.cursor is None and again.objects == {}


# ----- change history -------------------------------------------------------


def _rows(db: Path, sql: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql).fetchall()


def test_record_runs_writes_changes_with_their_run_id(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    with Store(db) as store:
        store.record_runs(
            [
                RunRecord("a", T0, T0, "ok", 1, 1, 0, 0, 3, None, reverted=1, protected=2, out_of_window=4),
                RunRecord("b", T0, T0, "blocked", 0, 0, 0, 0, 3, "guard", error_kind="guard"),
            ],
            [
                ChangeRecord("a", "o1", "created", T0, "Standup"),
                ChangeRecord("a", "o2", "reverted", T0, "Lunch"),
                ChangeRecord("b", "o3", "blocked", T0, None),
            ],
        )
        latest = {r.rule: r for r in store.latest_runs()}
    runs = dict(_rows(db, "SELECT rule, id FROM run"))
    changes = _rows(db, "SELECT run_id, rule, object_id, action, at, title FROM change ORDER BY id")
    assert changes == [
        (runs["a"], "a", "o1", "created", T0.isoformat(), "Standup"),
        (runs["a"], "a", "o2", "reverted", T0.isoformat(), "Lunch"),
        (runs["b"], "b", "o3", "blocked", T0.isoformat(), None),
    ]
    a, b = latest["a"], latest["b"]
    assert (a.reverted, a.protected, a.out_of_window, a.error_kind) == (1, 2, 4, None)
    assert (b.reverted, b.protected, b.out_of_window, b.error_kind) == (0, 0, 0, "guard")


def test_record_runs_without_runs_is_a_no_op(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    with Store(db) as store:
        store.record_runs([])
    assert _count(db, "SELECT COUNT(*) FROM run") == 0


def test_changes_older_than_the_retention_are_pruned(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    old = T0 - store_mod.CHANGE_RETENTION - timedelta(seconds=1)
    edge = T0 - store_mod.CHANGE_RETENTION
    with Store(db) as store:
        store.record_runs(
            [RunRecord("a", old, old, "ok", 2, 0, 0, 0, 1, None)],
            [
                ChangeRecord("a", "o1", "created", old, "Standup"),
                ChangeRecord("a", "o2", "created", edge, "Lunch"),
            ],
        )
        assert _count(db, "SELECT COUNT(*) FROM change") == 2  # "now" is the newest finished_at
        store.record_runs([RunRecord("b", T0, T0, "ok", 0, 0, 0, 0, 1, None)])
    assert _rows(db, "SELECT object_id FROM change") == [("o2",)]


def test_changes_outlive_their_pruned_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changes are pruned by age only: RUN_RETENTION keeps hours of runs, changes stay 90 days."""
    monkeypatch.setattr(store_mod, "RUN_RETENTION", 2)
    db = tmp_path / "s.db"
    with Store(db) as store:
        store.record_runs(
            [RunRecord("a", T0, T0, "ok", 1, 0, 0, 0, 1, None)],
            [ChangeRecord("a", "o1", "created", T0, "Lunch")],
        )
        later = T0 + store_mod.CHANGE_RETENTION - timedelta(seconds=1)
        for i in range(5):
            t = T0 + (later - T0) * (i + 1) / 5
            store.record_runs([RunRecord("a", t, t, "ok", 0, 0, 0, 0, 1, None)])
        assert _count(db, "SELECT COUNT(*) FROM run WHERE rule = 'a'") == 2
        assert store.last_title("a", "o1") == "Lunch"
    assert _rows(db, "SELECT rule, object_id, action FROM change") == [("a", "o1", "created")]


def test_age_prune_uses_an_index_on_at(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    Store(db).close()
    with closing(sqlite3.connect(db)) as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN DELETE FROM change WHERE at < ?", ("x",)).fetchall()
    assert any("change_at" in row[-1] for row in plan), plan


def test_failing_change_insert_rolls_back_the_runs_of_the_call(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    with Store(db) as store:
        store.record_runs([RunRecord("a", T0, T0, "ok", 0, 0, 0, 0, 1, None)])
        with pytest.raises(KeyError):
            store.record_runs(
                [RunRecord("a", T1, T1, "ok", 1, 0, 0, 0, 1, None)],
                [ChangeRecord("b", "o1", "created", T1, "Lunch")],  # rule "b" has no run in this call
            )
        assert _count(db, "SELECT COUNT(*) FROM run") == 1
        assert _count(db, "SELECT COUNT(*) FROM change") == 0
        store.record_runs([RunRecord("a", T1, T1, "ok", 0, 0, 0, 0, 1, None)])  # no dangling transaction
    assert _count(db, "SELECT COUNT(*) FROM run") == 2


def test_last_title_is_the_newest_known_title(tmp_path: Path) -> None:
    with Store(tmp_path / "s.db") as store:
        assert store.last_title("a", "o1") is None
        store.record_runs(
            [
                RunRecord("a", T0, T0, "ok", 1, 0, 0, 0, 1, None),
                RunRecord("b", T0, T0, "ok", 1, 0, 0, 0, 1, None),
            ],
            [
                ChangeRecord("a", "o1", "created", T0, "Standup"),
                ChangeRecord("b", "o1", "created", T0, "Other"),
            ],
        )
        store.record_runs(
            [RunRecord("a", T1, T1, "ok", 0, 1, 0, 0, 1, None)],
            [ChangeRecord("a", "o1", "updated", T1, "Weekly review")],
        )
        store.record_runs(
            [RunRecord("a", T1, T1, "ok", 0, 0, 0, 0, 1, None)],
            [ChangeRecord("a", "o1", "protected", T1, None)],
        )
        assert store.last_title("a", "o1") == "Weekly review"
        assert store.last_title("b", "o1") == "Other"
        assert store.last_title("a", "o2") is None


V3_RUN = """
CREATE TABLE run (
    id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL,
    updated INTEGER NOT NULL, deleted INTEGER NOT NULL, failed INTEGER NOT NULL,
    requests INTEGER NOT NULL, error TEXT
)
"""


def test_v3_database_is_rebuilt_to_v4(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '3')")
        conn.execute(V3_RUN)
        conn.execute("INSERT INTO run VALUES (1, 'a', 't', 't', 'ok', 0, 0, 0, 0, 1, NULL)")
        conn.commit()
    with Store(db) as store:
        assert store.latest_runs() == []
        store.record_runs(
            [RunRecord("a", T0, T0, "ok", 1, 0, 0, 0, 1, None)],
            [ChangeRecord("a", "o1", "created", T0, "Lunch")],
        )
    assert store_mod.SCHEMA_VERSION == 4
    assert _rows(db, "SELECT value FROM meta WHERE key = 'schema_version'") == [("4",)]
    run_columns = [r[1] for r in _rows(db, "PRAGMA table_info(run)")]
    assert run_columns[-4:] == ["reverted", "protected", "out_of_window", "error_kind"]
    assert _count(db, "SELECT COUNT(*) FROM change") == 1
