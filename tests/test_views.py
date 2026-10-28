"""Read-only views over the state database (FakeGoogle + FakeTarget)."""

import json
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from calsync.config import Config
from calsync.naming import object_id, rule_tag
from calsync.sources.base import CalendarPull
from calsync.store import SCHEMA_VERSION, AccountCalendar, ChangeRecord, RunRecord, Store
from calsync.targets.base import CachedObject, TargetCache
from calsync.views import (
    AccountCalendarView,
    ConfigChangeView,
    StateReader,
    StateUnavailable,
    build_blocked,
    build_changes,
    build_runs,
    build_status,
    build_upcoming,
    host_for,
)
from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, NOW, build_fake, config_for, rule


def seeded(summary_override: str | None = None) -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family", summary_override=summary_override)
    fake.put_event(
        GCAL,
        google_event(
            "standup",
            "Standup",
            "2026-10-23T18:00:00+08:00",
            "2026-10-23T18:15:00+08:00",
            recurrence=["RRULE:FREQ=DAILY;COUNT=10"],
        ),
    )
    fake.put_event(
        GCAL,
        google_event(
            "lunch", "Lunch", "2026-10-25T12:00:00+08:00", "2026-10-25T13:00:00+08:00", location="Cafe"
        ),
    )
    fake.put_event(
        GCAL,
        google_event("review", "Weekly review", "2026-11-20T10:00:00+08:00", "2026-11-20T11:00:00+08:00"),
    )
    return fake


def family(**extra: Any) -> Config:
    return config_for([rule(alarms=["-10m", "0m"], **extra)])


def reader(tmp_path: Path) -> StateReader:
    return StateReader(tmp_path / "state.db")


STANDUP_TODAY = {
    "rule": "family",
    "start": "2026-10-24T18:00:00+08:00",
    "end": "2026-10-24T18:15:00+08:00",
    "all_day": False,
    "title": "Standup",
    "location": "",
    "alarms_minutes_before": [10, 0],
    "recurring": True,
    "target_calendar": "Family",
    "delivery": "delivered",
}
LUNCH = {
    "rule": "family",
    "start": "2026-10-25T12:00:00+08:00",
    "end": "2026-10-25T13:00:00+08:00",
    "all_day": False,
    "title": "Lunch",
    "location": "Cafe",
    "alarms_minutes_before": [10, 0],
    "recurring": False,
    "target_calendar": "Family",
    "delivery": "delivered",
}


# ----- reader ---------------------------------------------------------------


def test_missing_database_is_state_unavailable(tmp_path: Path) -> None:
    with pytest.raises(StateUnavailable):
        StateReader(tmp_path / "state.db")
    assert not (tmp_path / "state.db").exists()


def test_reader_opens_a_path_with_uri_characters(tmp_path: Path) -> None:
    state_dir = tmp_path / "state %41 #1 ?x"
    state_dir.mkdir()
    with Store(state_dir / "state.db") as store:
        store.record_runs([_run("ok")])
    with StateReader(state_dir / "state.db") as r:
        assert [run.status for run in r.runs()] == ["ok"]


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("icloud", "iCloud"),
        ("https://caldav.icloud.com/", "iCloud"),
        ("https://p42-caldav.icloud.com.cn/123/", "iCloud"),
        ("https://dav.example.com/cal/", "dav.example.com"),
        ("https://user:tok3n@dav.example.com/cal/", "dav.example.com"),
        ("https://user:tok3n@/cal/", "(hidden)"),  # no host: never the raw text
    ],
)
def test_caldav_host(url: str, host: str) -> None:
    assert host_for("caldav", url) == host


def test_google_host() -> None:
    assert host_for("google", None) == "Google Calendar"


# ----- after a real cycle -------------------------------------------------------


@pytest.mark.anyio
async def test_status_after_a_cycle(tmp_path: Path) -> None:
    config = family()
    h = build_fake(tmp_path, seeded(), config)
    await h.engine.cycle()
    with reader(tmp_path) as r:
        view = build_status(config, r, NOW).to_json()
    assert list(view) == ["generated_at", "schema", "summary", "accounts", "rules", "attention"]
    assert view["generated_at"] == NOW.isoformat()
    assert view["schema"] == {"version": SCHEMA_VERSION, "current": True}
    assert view["summary"] == {
        "health": "ok",
        "rules": 1,
        "attention": 0,
        "last_sync_at": NOW.isoformat(),
        "changes_24h": 3,
        "delivered_today": 3,
    }
    assert view["accounts"] == [  # no calendar list cached yet
        {"name": "me", "provider": "google", "host": "Google Calendar", "auth": "ok", "calendars": []},
        {"name": "dav", "provider": "caldav", "host": "unused", "auth": "ok", "calendars": []},
    ]
    (fam,) = view["rules"]
    last_run = fam.pop("last_run")
    assert fam == {
        "name": "family",
        "source": {"account": "me", "calendar": "Family", "calendar_name": "Family"},
        "target": {"account": "dav", "calendar": "Family", "calendar_name": "Family"},
        "scope": {"kind": "all"},
        "alarms": {"minutes_before": [10, 0]},
        "fields": {"attendees": "notes", "conference": "copy", "content": "full"},
        "health": "ok",
        "last_ok_at": NOW.isoformat(),
        "consecutive_failures": 0,
        "copies": 3,
        "out_of_window": 0,
        "protected": 0,
    }
    assert list(last_run) == [
        "id",
        "rule",
        "started_at",
        "finished_at",
        "duration_ms",
        "status",
        "created",
        "updated",
        "deleted",
        "failed",
        "reverted",
        "protected",
        "out_of_window",
        "requests",
        "error",
        "error_kind",
        "error_account",
    ]
    assert last_run["status"] == "ok" and last_run["created"] == 3 and last_run["duration_ms"] == 0
    assert view["attention"] == []
    json.dumps(view)  # serializable as is


@pytest.mark.anyio
async def test_status_scope_alarms_and_source_name_override(tmp_path: Path) -> None:
    fake = seeded(summary_override="Home")
    config = config_for([rule(source="Home", alarms="strip", window={"past": "7d", "future": "20d"})])
    h = build_fake(tmp_path, fake, config)
    await h.engine.cycle()
    with reader(tmp_path) as r:
        (fam,) = build_status(config, r, NOW).to_json()["rules"]
    assert fam["source"] == {"account": "me", "calendar": "Home", "calendar_name": "Home"}
    assert fam["scope"] == {"kind": "window", "past_days": 7, "future_days": 20}
    assert fam["alarms"] == "strip"
    assert fam["copies"] == 2 and fam["out_of_window"] == 1


@pytest.mark.anyio
async def test_upcoming_expands_instances_with_policy_alarms(tmp_path: Path) -> None:
    config = family()
    h = build_fake(tmp_path, seeded(), config)
    await h.engine.cycle()
    with reader(tmp_path) as r:
        day = build_upcoming(config, r, NOW).to_json()
        two = build_upcoming(config, r, NOW, hours=48).to_json()
        none = build_upcoming(config, r, NOW, rule="other").to_json()
    assert list(day) == ["from", "to", "hours", "schema", "events"]
    assert day["from"] == NOW.isoformat() and day["to"] == (NOW + timedelta(hours=24)).isoformat()
    assert day["hours"] == 24 and day["schema"] == {"version": SCHEMA_VERSION, "current": True}
    assert day["events"] == [STANDUP_TODAY, LUNCH]
    standup_tomorrow = {
        **STANDUP_TODAY,
        "start": "2026-10-25T18:00:00+08:00",
        "end": "2026-10-25T18:15:00+08:00",
    }
    assert two["events"] == [STANDUP_TODAY, LUNCH, standup_tomorrow]
    assert none["events"] == []


@pytest.mark.anyio
async def test_upcoming_is_pending_until_the_next_cycle_writes_the_change(tmp_path: Path) -> None:
    config = family()
    fake = seeded()
    h = build_fake(tmp_path, fake, config)
    await h.engine.cycle()
    fake.put_event(
        GCAL,
        google_event(
            "lunch", "Team lunch", "2026-10-25T12:00:00+08:00", "2026-10-25T13:00:00+08:00", location="Cafe"
        ),
    )
    await h.engine.cycle(dry_run=True)  # the mirror has the change, the target does not
    with reader(tmp_path) as r:
        events = build_upcoming(config, r, NOW).to_json()["events"]
    assert events == [STANDUP_TODAY, {**LUNCH, "title": "Team lunch", "delivery": "pending"}]
    await h.engine.cycle()
    with reader(tmp_path) as r:
        events = build_upcoming(config, r, NOW).to_json()["events"]
    assert events[1]["delivery"] == "delivered"


@pytest.mark.anyio
async def test_upcoming_skips_series_outside_the_rule_window(tmp_path: Path) -> None:
    config = config_for([rule(window={"past": "1d", "future": "1d"})])
    h = build_fake(tmp_path, seeded(), config)
    await h.engine.cycle()
    with reader(tmp_path) as r:
        titles = [e["title"] for e in build_upcoming(config, r, NOW, hours=24 * 60).to_json()["events"]]
    assert "Weekly review" not in titles and "Lunch" in titles


@pytest.mark.anyio
async def test_upcoming_applies_the_field_policy(tmp_path: Path) -> None:
    config = config_for([rule(fields={"content": "busy"})])
    h = build_fake(tmp_path, seeded(), config)
    await h.engine.cycle()
    with reader(tmp_path) as r:
        events = build_upcoming(config, r, NOW).to_json()["events"]
    assert {(e["title"], e["location"], e["delivery"]) for e in events} == {("Busy", "", "delivered")}


@pytest.mark.anyio
async def test_runs_and_changes_newest_first(tmp_path: Path) -> None:
    config = family()
    fake = seeded()
    h = build_fake(tmp_path, fake, config)
    await h.engine.cycle()
    fake.cancel_event(GCAL, "review")
    await h.engine.cycle()
    with reader(tmp_path) as r:
        runs = build_runs(config, r).to_json()["runs"]
        one = build_runs(config, r, limit=1).to_json()["runs"]
        other = build_runs(config, r, rule="other").to_json()["runs"]
        changes = build_changes(config, r).to_json()["changes"]
        latest = build_changes(config, r, limit=1).to_json()["changes"]
        later = build_changes(config, r, since=NOW + timedelta(seconds=1)).to_json()["changes"]
    assert [run["id"] for run in runs] == [2, 1]
    assert [run["deleted"] for run in runs] == [1, 0]
    assert one == runs[:1] and other == []
    assert list(changes[0]) == ["id", "run_id", "rule", "object_id", "action", "at", "title", "detail"]
    assert changes[0] == {
        "id": 4,
        "run_id": 2,
        "rule": "family",
        "object_id": object_id("family", GCAL, "review"),
        "action": "deleted",
        "at": NOW.isoformat(),
        "title": "Weekly review",
        "detail": None,
    }
    assert sorted(c["action"] for c in changes) == ["created", "created", "created", "deleted"]
    assert [c["id"] for c in changes] == [4, 3, 2, 1]
    assert latest == changes[:1] and later == []


# ----- health and attention, from run rows ------------------------------------------


def _run(status: str, *, failed: int = 0, error: str | None = None, kind: str | None = None) -> RunRecord:
    return RunRecord("family", NOW, NOW, status, 0, 0, 0, failed, 2, error, error_kind=kind)


def _status(tmp_path: Path, runs: list[RunRecord], changes: Sequence[ChangeRecord] = ()) -> dict[str, Any]:
    """Records ``runs`` one by one; ``changes`` belong to the last one."""
    config = family()
    with Store(tmp_path / "state.db") as store:
        for i, run in enumerate(runs):
            store.record_runs([run], changes if i == len(runs) - 1 else ())
    with reader(tmp_path) as r:
        return build_status(config, r, NOW).to_json()


@pytest.mark.parametrize(
    ("runs", "health"),
    [
        ([], "never_run"),
        ([_run("ok")], "ok"),
        ([_run("partial")], "ok"),
        ([_run("partial", failed=1)], "failing"),
        ([_run("failed", error="Google API 401: revoked", kind="auth")], "auth"),
        ([_run("failed", error="AuthError: old row without a kind")], "auth"),
        ([_run("blocked", error="would delete 4 of 4", kind="guard")], "blocked"),
        ([_run("failed", error="boom", kind="other")], "failing"),
        ([_run("failed", error="boom", kind="other"), _run("ok")], "ok"),
    ],
)
def test_rule_health(tmp_path: Path, runs: list[RunRecord], health: str) -> None:
    view = _status(tmp_path, runs)
    assert view["rules"][0]["health"] == health
    assert view["summary"]["health"] == health


def test_auth_attention_names_the_google_account(tmp_path: Path) -> None:
    view = _status(tmp_path, [_run("failed", error="Google API 401: revoked", kind="auth")])
    assert view["attention"] == [
        {
            "kind": "auth",
            "rule": "family",
            "account": "me",
            "message": "The me account needs to be connected again.",
            "command": "calsync auth me",
        }
    ]
    assert view["summary"]["attention"] == 1


def test_auth_attention_names_the_target_account(tmp_path: Path) -> None:
    view = _status(tmp_path, [_run("failed", error="CalDAV 401 for /cal/", kind="auth")])
    (item,) = view["attention"]
    assert item["account"] == "dav" and item["command"] == "calsync auth dav"
    assert item["message"] == "The dav account needs to be connected again."


def test_failing_attention_counts_consecutive_failures(tmp_path: Path) -> None:
    runs = [_run("failed", error="x"), _run("ok"), *[_run("failed", error="boom") for _ in range(3)]]
    view = _status(tmp_path, runs)
    assert view["rules"][0]["consecutive_failures"] == 3
    assert view["attention"] == [
        {
            "kind": "failing",
            "rule": "family",
            "account": None,
            "message": "Sync has failed 3 times in a row.",
            "command": None,
        }
    ]


def test_consecutive_failures_count_only_runs_that_are_not_ok(tmp_path: Path) -> None:
    # A partial run without failures is healthy (as health says), so it ends the streak.
    runs = [_run("failed", error="x"), _run("partial"), _run("partial", failed=1), _run("failed", error="y")]
    view = _status(tmp_path, runs)
    assert view["rules"][0]["consecutive_failures"] == 2


def test_blocked_attention_counts_the_blocked_deletions(tmp_path: Path) -> None:
    blocked = [ChangeRecord("family", f"o{i}", "blocked", NOW, None) for i in range(4)]
    view = _status(tmp_path, [_run("blocked", error="guard", kind="guard")], blocked)
    assert view["attention"] == [
        {
            "kind": "blocked",
            "rule": "family",
            "account": None,
            "message": "4 copies would be deleted — check `calsync blocked --rule family`, then approve if intended",
            "command": "calsync approve family",
        }
    ]


def test_blocked_attention_for_one_copy(tmp_path: Path) -> None:
    blocked = [ChangeRecord("family", "o1", "blocked", NOW, None)]
    view = _status(tmp_path, [_run("blocked", error="guard", kind="guard")], blocked)
    assert view["attention"][0]["message"] == (
        "1 copy would be deleted — check `calsync blocked --rule family`, then approve if intended"
    )


def test_blocked_attention_without_rows_has_no_count(tmp_path: Path) -> None:
    view = _status(tmp_path, [_run("blocked", error="guard", kind="guard")])
    assert (
        view["attention"][0]["message"]
        == "Copies would be deleted — check `calsync blocked --rule family`, then approve if intended"
    )


# A blocked rule's history, run by run: (status, blocked object ids recorded with that run).
T0, T1, T2 = NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)
SET_A = ["oa1", "oa2", "oa3", "oa4"]


def _history(tmp_path: Path, runs: list[tuple[str, list[str]]]) -> None:
    """Records ``runs`` one minute apart from ``T0``, each with its own blocked rows."""
    with Store(tmp_path / "state.db") as store:
        for i, (status, blocked) in enumerate(runs):
            at = T0 + timedelta(minutes=i)
            error, kind = (
                ("guard", "guard")
                if status == "blocked"
                else ("boom", "other")
                if status == "failed"
                else (None, None)
            )
            store.record_runs(
                [RunRecord("family", at, at, status, 0, 0, 0, 0, 1, error, error_kind=kind)],
                [ChangeRecord("family", oid, "blocked", at, f"Event {oid}") for oid in blocked],
            )


def _blocked_and_attention(tmp_path: Path) -> tuple[Any, str]:
    config = family()
    with reader(tmp_path) as r:
        view = build_blocked(config, r)
        status = build_status(config, r, T2).to_json()
    [entry] = view.rules
    return entry, status["attention"][0]["message"]


def test_rows_of_an_older_block_are_not_the_current_one(tmp_path: Path) -> None:
    """Blocked again after an ok run, without rows of its own: the set is unknown, and the
    attention count agrees with ``calsync blocked`` (no count from the older block's rows)."""
    from calsync.engine import delete_fingerprint

    _history(tmp_path, [("blocked", SET_A), ("ok", []), ("blocked", [])])
    entry, message = _blocked_and_attention(tmp_path)
    assert (entry.since, entry.count, entry.fingerprint, entry.items) == (T2, 0, None, ())
    assert (
        message == "Copies would be deleted — check `calsync blocked --rule family`, then approve if intended"
    )
    assert delete_fingerprint(SET_A) != entry.fingerprint


def test_the_same_set_blocked_again_after_an_ok_run_is_a_new_block(tmp_path: Path) -> None:
    from calsync.engine import delete_fingerprint

    _history(tmp_path, [("blocked", SET_A), ("ok", []), ("blocked", SET_A)])
    entry, message = _blocked_and_attention(tmp_path)
    assert (entry.since, entry.count, entry.fingerprint) == (T2, 4, delete_fingerprint(SET_A))
    assert message.startswith("4 copies would be deleted")


def test_a_failed_run_in_the_middle_keeps_the_block(tmp_path: Path) -> None:
    from calsync.engine import delete_fingerprint

    _history(tmp_path, [("blocked", SET_A), ("failed", []), ("blocked", [])])
    entry, message = _blocked_and_attention(tmp_path)
    assert (entry.since, entry.count, entry.fingerprint) == (T0, 4, delete_fingerprint(SET_A))
    assert [i.object_id for i in entry.items] == SET_A
    assert message.startswith("4 copies would be deleted")


def four() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for i in range(4):
        fake.put_event(
            GCAL,
            google_event(f"e{i}", f"Standup {i}", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00"),
        )
    return fake


@pytest.mark.anyio
async def test_blocked_count_survives_consecutive_blocked_runs(tmp_path: Path) -> None:
    # Blocked rows are written only when the blocked set changes: the second run has none.
    fake = four()
    config = config_for([rule()])
    h = build_fake(tmp_path, fake, config)
    await h.engine.cycle()
    for i in range(4):
        fake.cancel_event(GCAL, f"e{i}")
    for _ in range(2):
        assert (await h.engine.cycle()).rules["family"].status == "blocked"
    with reader(tmp_path) as r:
        view = build_status(config, r, NOW).to_json()
    assert view["rules"][0]["consecutive_failures"] == 2
    assert (
        view["attention"][0]["message"]
        == "4 copies would be deleted — check `calsync blocked --rule family`, then approve if intended"
    )


def test_summary_health_is_the_worst_rule(tmp_path: Path) -> None:
    config = config_for([rule("a"), rule("b"), rule("c")])
    with Store(tmp_path / "state.db") as store:
        store.record_runs(
            [
                RunRecord("a", NOW, NOW, "ok", 0, 0, 0, 0, 1, None),
                RunRecord("b", NOW, NOW, "failed", 0, 0, 0, 0, 1, "boom", error_kind="other"),
            ]
        )
    with reader(tmp_path) as r:
        view = build_status(config, r, NOW).to_json()
    assert [x["health"] for x in view["rules"]] == ["ok", "failing", "never_run"]
    assert view["summary"]["health"] == "failing"
    with reader(tmp_path) as r:
        assert build_status(config_for([]), r, NOW).to_json()["summary"]["health"] == "ok"


def test_changes_24h_counts_only_recent_runs(tmp_path: Path) -> None:
    old = NOW - timedelta(hours=25)
    with Store(tmp_path / "state.db") as store:
        store.record_runs([RunRecord("family", old, old, "ok", 5, 0, 0, 0, 1, None)])
        store.record_runs([RunRecord("family", NOW, NOW, "ok", 1, 2, 3, 0, 1, None)])
    with reader(tmp_path) as r:
        assert build_status(family(), r, NOW).to_json()["summary"]["changes_24h"] == 6


# ----- an old (schema 1) database --------------------------------------------------

V1_SCHEMA = [
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE source_state (account TEXT PRIMARY KEY, state TEXT NOT NULL)",
    "CREATE TABLE source_event (account TEXT NOT NULL, calendar TEXT NOT NULL, event_id TEXT NOT NULL, "
    "raw TEXT NOT NULL, PRIMARY KEY (account, calendar, event_id))",
    "CREATE TABLE target_calendar (account TEXT NOT NULL, ref TEXT NOT NULL, href TEXT NOT NULL, "
    "name TEXT NOT NULL, mode TEXT NOT NULL, cursor TEXT, PRIMARY KEY (account, ref))",
    "CREATE TABLE target_object (account TEXT NOT NULL, calendar_href TEXT NOT NULL, href TEXT NOT NULL, "
    "etag TEXT, rule_tag TEXT NOT NULL, object_id TEXT NOT NULL, content_hash TEXT, span_start TEXT, "
    "span_end TEXT, PRIMARY KEY (account, calendar_href, href))",
    "CREATE TABLE run (id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL, started_at TEXT NOT NULL, "
    "finished_at TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL, "
    "deleted INTEGER NOT NULL, failed INTEGER NOT NULL, requests INTEGER NOT NULL, error TEXT)",
    "CREATE INDEX run_rule ON run (rule, id)",
    "CREATE TABLE alert (key TEXT PRIMARY KEY, state TEXT NOT NULL, last_sent_at TEXT)",
]


def make_v1(path: Path) -> None:
    """A state database as calsync 0.1 wrote it: WAL, no change table, no newer columns."""
    lunch = google_event(
        "lunch", "Lunch", "2026-10-25T12:00:00+08:00", "2026-10-25T13:00:00+08:00", location="Cafe"
    )
    standup = google_event(
        "standup",
        "Standup",
        "2026-10-23T18:00:00+08:00",
        "2026-10-23T18:15:00+08:00",
        recurrence=["RRULE:FREQ=DAILY;COUNT=10"],
    )
    state = {
        "calendars_token": "c1",
        "events_tokens": {GCAL: "e1"},
        "meta": {
            GCAL: {
                "summary": "Family",
                "summary_override": "",
                "timezone": "Asia/Shanghai",
                "default_alarms": [],
            }
        },
    }
    started = (NOW - timedelta(minutes=5)).isoformat()
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        for statement in V1_SCHEMA:
            db.execute(statement)
        db.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        db.execute("INSERT INTO source_state VALUES ('me', ?)", (json.dumps(state),))
        for event in (lunch, standup):
            db.execute(
                "INSERT INTO source_event VALUES ('me', ?, ?, ?)", (GCAL, event["id"], json.dumps(event))
            )
        db.execute(
            "INSERT INTO target_calendar VALUES ('dav', 'Family', '/cal/family/', 'Family', 'sync', 'c')"
        )
        db.execute(
            "INSERT INTO target_object VALUES ('dav', '/cal/family/', '/cal/family/x.ics', '\"e\"', ?, ?, "
            "'oldhash', NULL, NULL)",
            (rule_tag("family"), object_id("family", GCAL, "lunch")),
        )
        db.execute(
            "INSERT INTO run (rule, started_at, finished_at, status, created, updated, deleted, failed, "
            "requests, error) VALUES ('family', ?, ?, 'ok', 2, 0, 0, 0, 4, NULL)",
            (started, started),
        )
        db.execute(
            "INSERT INTO run (rule, started_at, finished_at, status, created, updated, deleted, failed, "
            "requests, error) VALUES ('family', ?, ?, 'failed', 0, 0, 0, 0, 1, 'AuthError: Google API 401')",
            (started, NOW.isoformat()),
        )
        db.execute(
            "INSERT INTO run (rule, started_at, finished_at, status, created, updated, deleted, failed, "
            "requests, error) VALUES ('other', ?, ?, 'blocked', 0, 0, 0, 0, 1, 'would delete 5')",
            (started, NOW.isoformat()),
        )


def snapshot(directory: Path) -> dict[str, tuple[int, bytes]]:
    return {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(directory.iterdir())}


def test_v1_database_is_read_gracefully(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db = state_dir / "state.db"
    make_v1(db)
    before = snapshot(state_dir)
    config = config_for([rule(alarms=["-10m", "0m"]), rule("other")])
    with StateReader(db) as r:
        assert r.schema_version() == 1
        status = build_status(config, r, NOW).to_json()
        upcoming = build_upcoming(config, r, NOW).to_json()
        only_family = build_upcoming(config, r, NOW, rule="family").to_json()
        runs = build_runs(config, r).to_json()
        changes = build_changes(config, r).to_json()
    assert snapshot(state_dir) == before  # nothing written, no file created

    assert status["schema"] == {"version": 1, "current": False}
    fam, other = status["rules"]
    assert fam["health"] == "auth" and fam["copies"] == 1
    assert fam["source"]["calendar_name"] == "Family" and fam["target"]["calendar_name"] == "Family"
    assert fam["last_ok_at"] == started_iso() and fam["consecutive_failures"] == 1
    assert fam["last_run"]["reverted"] == 0 and fam["last_run"]["error_kind"] is None
    assert fam["last_run"]["duration_ms"] == 5 * 60 * 1000
    assert (fam["out_of_window"], fam["protected"]) == (0, 0)
    assert other["health"] == "blocked"
    assert [a["kind"] for a in status["attention"]] == ["auth", "blocked"]
    assert status["attention"][0]["account"] == "me"
    assert (
        status["attention"][1]["message"]
        == "Copies would be deleted — check `calsync blocked --rule other`, then approve if intended"
    )
    assert status["summary"]["health"] == "auth"

    assert upcoming["schema"] == {"version": 1, "current": False}
    assert [(e["rule"], e["title"]) for e in upcoming["events"]] == [
        ("family", "Standup"),
        ("other", "Standup"),
        ("family", "Lunch"),
        ("other", "Lunch"),
    ]
    assert {e["delivery"] for e in upcoming["events"]} == {"unknown"}
    assert [e["title"] for e in only_family["events"]] == ["Standup", "Lunch"]
    assert only_family["events"][0]["alarms_minutes_before"] == [10, 0]

    assert [run["rule"] for run in runs["runs"]] == ["other", "family", "family"]
    assert changes == {"changes": []}


def started_iso() -> str:
    return (NOW - timedelta(minutes=5)).isoformat()


def test_reader_never_modifies_a_current_database(tmp_path: Path) -> None:
    with Store(tmp_path / "state.db") as store:
        store.record_runs([RunRecord("family", NOW, NOW, "ok", 1, 0, 0, 0, 1, None)])
    before = snapshot(tmp_path)
    with reader(tmp_path) as r:
        assert r.schema_version() == SCHEMA_VERSION
        build_status(family(), r, NOW)
        build_upcoming(family(), r, NOW)
        build_runs(family(), r)
        build_changes(family(), r)
    assert snapshot(tmp_path) == before


def test_database_without_meta_has_no_version(tmp_path: Path) -> None:
    with closing(sqlite3.connect(tmp_path / "state.db")) as db:
        db.execute("CREATE TABLE unrelated (x)")
    with reader(tmp_path) as r:
        assert r.schema_version() is None
        view = build_status(family(), r, NOW).to_json()
        events = build_upcoming(family(), r, NOW).to_json()["events"]
    assert view["schema"] == {"version": None, "current": False}
    assert view["rules"][0]["health"] == "never_run" and view["rules"][0]["copies"] == 0
    assert view["rules"][0]["source"]["calendar_name"] == "Family"
    assert events == []


def test_change_since_filters_by_instant(tmp_path: Path) -> None:
    with Store(tmp_path / "state.db") as store:
        store.record_runs(
            [RunRecord("family", NOW, NOW, "ok", 2, 0, 0, 0, 1, None)],
            [
                ChangeRecord("family", "a", "created", NOW - timedelta(hours=2), "Standup"),
                ChangeRecord("family", "b", "created", NOW, None),
            ],
        )
    since = (NOW - timedelta(hours=1)).astimezone(UTC)
    with reader(tmp_path) as r:
        changes = build_changes(family(), r, since=since).to_json()["changes"]
        assert build_changes(family(), r, rule="other").to_json()["changes"] == []
    assert [(c["object_id"], c["title"]) for c in changes] == [("b", None)]


def test_change_since_keeps_rows_whose_ids_do_not_follow_time(tmp_path: Path) -> None:
    """Rules run concurrently and stamp their own clocks, but one cycle's rows are inserted in
    config order: a rule earlier in the config may have lower ids and later times."""
    config = config_for([rule("a"), rule("b", source="B", target="B")])
    with Store(tmp_path / "state.db") as store:
        store.record_runs(
            [
                RunRecord("a", NOW, NOW + timedelta(seconds=2), "ok", 1, 0, 0, 0, 1, None),
                RunRecord("b", NOW, NOW + timedelta(seconds=1), "ok", 1, 0, 0, 0, 1, None),
            ],
            [
                ChangeRecord("a", "late", "created", NOW + timedelta(seconds=2), "Standup"),
                ChangeRecord("b", "early", "created", NOW + timedelta(seconds=1), "Standup"),
                ChangeRecord("b", "old", "created", NOW - timedelta(hours=1), "Standup"),
            ],
        )
    with reader(tmp_path) as r:
        changes = build_changes(config, r, since=NOW + timedelta(milliseconds=1500)).to_json()["changes"]
        both = build_changes(config, r, since=NOW + timedelta(seconds=1)).to_json()["changes"]
        limited = build_changes(config, r, since=NOW, limit=1).to_json()["changes"]
    assert [c["object_id"] for c in changes] == ["late"]
    assert [c["object_id"] for c in both] == ["early", "late"]  # newest first by id, as before
    assert [c["object_id"] for c in limited] == ["early"]


def test_changes_of_one_run(tmp_path: Path) -> None:
    with Store(tmp_path / "state.db") as store:
        for i, oid in enumerate(("a", "b", "c")):
            at = NOW + timedelta(minutes=i)
            store.record_runs(
                [RunRecord("family", at, at, "ok", 1, 0, 0, 0, 1, None)],
                [ChangeRecord("family", oid, "created", at, "Standup")],
            )
    with reader(tmp_path) as r:
        second = build_changes(family(), r, run_id=2).to_json()["changes"]
        assert build_changes(family(), r, rule="other", run_id=2).to_json()["changes"] == []
        assert build_changes(family(), r, run_id=99).to_json()["changes"] == []
    assert [(c["run_id"], c["object_id"]) for c in second] == [(2, "b")]


# ----- schema v5 accessors ------------------------------------------------------

SUMMARY = {
    "title": "Lunch",
    "start": "2026-10-25T12:00:00+08:00",
    "end": "2026-10-25T13:00:00+08:00",
    "all_day": False,
    "location": "Cafe",
    "alarms_minutes_before": [10, 0],
    "recurring": False,
}


def test_v5_accessors_read_what_the_store_wrote(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    t = [NOW + timedelta(minutes=i) for i in range(6)]
    with Store(db) as store:
        first = store.record_config_change(
            at=t[0], source="cli", summary='rule "family" paused', before="x = 1\n", after_fingerprint="f1"
        )
        store.record_config_change(
            at=t[1], source="ui", summary="undo", before=None, after_fingerprint="f2", undoes=first
        )
        store.save_account_calendars(
            "me",
            [
                AccountCalendar("me", GCAL, "Family", "owner", t[0]),
                AccountCalendar("me", "c2", "Work", None, t[0]),
            ],
            t[0],
        )
        store.save_source(
            "me",
            {},
            [
                CalendarPull(GCAL, full=True, upserts=[{"id": "a"}, {"id": "b"}]),
                CalendarPull("c2", full=True),
            ],
        )
        cache = TargetCache(cursor=None)
        cache.objects["/cal/family/x.ics"] = CachedObject(
            '"e"', rule_tag("family"), "o1", "h", None, None, False
        )
        cache.objects["/cal/family/y.ics"] = CachedObject(
            '"e"', rule_tag("family"), "o2", "h", None, None, False
        )
        store.save_target_cache("dav", "/cal/family/", cache)
        for i, action in enumerate(("created", "updated", "reverted", "deleted", "blocked"), start=1):
            store.record_runs(
                [RunRecord("family", t[i], t[i], "ok", 0, 0, 0, 0, 1, None)],
                [ChangeRecord("family", "o1", action, t[i], "Lunch")],
            )
    with closing(sqlite3.connect(db)) as conn:  # Task 6 writes summaries; here by hand
        conn.execute("UPDATE target_object SET summary = ? WHERE object_id = 'o1'", (json.dumps(SUMMARY),))
        conn.commit()

    with reader(tmp_path) as r:
        assert r.schema_current()
        changes = r.config_changes()
        assert changes == [
            ConfigChangeView(first + 1, t[1], "ui", "undo", None, "f2", first),
            ConfigChangeView(first, t[0], "cli", 'rule "family" paused', "x = 1\n", "f1", None),
        ]
        assert r.config_changes(limit=1) == changes[:1]
        assert r.account_calendars("me") == [
            AccountCalendarView("me", "c2", "Work", None, t[0]),
            AccountCalendarView("me", GCAL, "Family", "owner", t[0]),
        ]
        assert r.account_calendars("other") == []
        assert r.copy_summary(rule_tag("family"), "o1") == SUMMARY
        assert r.copy_summary(rule_tag("family"), "o2") is None  # no summary written
        assert r.copy_summary(rule_tag("other"), "o1") is None
        # The newest create/update/revert: a later delete or block is not a delivery.
        assert r.delivered_at("family", "o1") == t[3]
        assert r.delivered_at("family", "o2") is None
        assert r.series_count("me", GCAL) == 2
        assert r.series_count("me", "c2") == 0
        assert r.series_count("other", GCAL) == 0


def test_copy_summary_ignores_unreadable_json(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with Store(db) as store:
        cache = TargetCache(cursor=None)
        cache.objects["/cal/x.ics"] = CachedObject(None, "tag", "o1", None, None, None, False)
        cache.objects["/cal/y.ics"] = CachedObject(None, "tag", "o2", None, None, None, False)
        store.save_target_cache("dav", "/cal/", cache)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE target_object SET summary = '{broken' WHERE object_id = 'o1'")
        conn.execute("UPDATE target_object SET summary = '[1, 2]' WHERE object_id = 'o2'")
        conn.commit()
    with reader(tmp_path) as r:
        assert r.copy_summary("tag", "o1") is None
        assert r.copy_summary("tag", "o2") is None


def test_v5_accessors_tolerate_an_older_database(tmp_path: Path) -> None:
    """A v1 database: no config history, no calendar cache, no summaries, no change log."""
    make_v1(tmp_path / "state.db")
    before = snapshot(tmp_path)
    with reader(tmp_path) as r:
        assert not r.schema_current()
        assert r.config_changes() == []
        assert r.account_calendars("me") == []
        assert r.copy_summary(rule_tag("family"), object_id("family", GCAL, "lunch")) is None
        assert r.delivered_at("family", "o1") is None
        assert r.series_count("me", GCAL) == 2
    assert snapshot(tmp_path) == before


def test_v5_accessors_on_a_database_without_tables(tmp_path: Path) -> None:
    with closing(sqlite3.connect(tmp_path / "state.db")) as db:
        db.execute("CREATE TABLE unrelated (x)")
    with reader(tmp_path) as r:
        assert r.config_changes() == []
        assert r.account_calendars("me") == []
        assert r.copy_summary("tag", "o1") is None
        assert r.delivered_at("family", "o1") is None
        assert r.series_count("me", GCAL) == 0


# ----- paused and retiring rules ------------------------------------------------------------


def _flagged(tmp_path: Path, runs: list[RunRecord], **flags: bool) -> dict[str, Any]:
    with Store(tmp_path / "state.db") as store:
        for run in runs:
            store.record_runs([run])
    with reader(tmp_path) as r:
        return build_status(config_for([rule(**flags)]), r, NOW).to_json()


@pytest.mark.parametrize(
    "runs",
    [[], [_run("ok")], [_run("failed", error="boom", kind="other")], [_run("blocked", kind="guard")]],
)
def test_a_paused_rule_is_paused_whatever_its_runs_say(tmp_path: Path, runs: list[RunRecord]) -> None:
    view = _flagged(tmp_path, runs, paused=True)
    assert view["rules"][0]["health"] == "paused"
    assert view["attention"] == [] and view["summary"]["health"] == "ok"


def test_a_retiring_rule_is_retiring_and_counts_as_ok(tmp_path: Path) -> None:
    view = _flagged(tmp_path, [_run("failed", error="boom", kind="other")], retire=True)
    assert view["rules"][0]["health"] == "retiring" and view["summary"]["health"] == "ok"
    assert [a["kind"] for a in view["attention"]] == ["failing"]  # a failure still needs a look


def test_a_blocked_retiring_rule_keeps_the_blocked_attention(tmp_path: Path) -> None:
    view = _flagged(tmp_path, [_run("blocked", error="would delete 4 of 4", kind="guard")], retire=True)
    assert view["rules"][0]["health"] == "retiring"
    assert [(a["kind"], a["rule"]) for a in view["attention"]] == [("blocked", "family")]


def test_a_paused_rule_is_checked_before_retiring(tmp_path: Path) -> None:
    view = _flagged(tmp_path, [], paused=True, retire=True)
    assert view["rules"][0]["health"] == "paused" and view["attention"] == []


# ----- status additions: the calendar map, account auth, deliveries today ---------------------------


async def _cached_status(tmp_path: Path, config: Config, **kwargs: Any) -> dict[str, Any]:
    """After one cycle (of ``family()``), with both accounts' calendar lists cached, as ``config``
    sees it."""
    h = build_fake(tmp_path, seeded(), family())
    await h.engine.cycle()
    with reader(tmp_path) as r:
        href = r.target_calendar_href("dav", "Family")
    assert href is not None
    h.store.save_account_calendars(
        "me",
        [
            AccountCalendar("me", GCAL, "Family", "owner", NOW),
            AccountCalendar("me", "other@group.calendar.google.com", "Other", "reader", NOW),
        ],
        NOW,
    )
    h.store.save_account_calendars(
        "dav",
        [
            AccountCalendar("dav", href, "Family", None, NOW),
            AccountCalendar("dav", "/dav/x/", "Spare", None, NOW),
        ],
        NOW,
    )
    for run in kwargs.get("runs", ()):
        h.store.record_runs([run])
    with reader(tmp_path) as r:
        return build_status(config, r, kwargs.get("now", NOW)).to_json()


@pytest.mark.anyio
async def test_status_accounts_carry_their_cached_calendars(tmp_path: Path) -> None:
    view = await _cached_status(tmp_path, family())
    me, dav = view["accounts"]
    assert me["calendars"] == [
        {"id": GCAL, "name": "Family", "role": "owner", "series": 3, "rules": ["family"], "synced": True},
        {
            "id": "other@group.calendar.google.com",
            "name": "Other",
            "role": "reader",
            "series": 0,
            "rules": [],
            "synced": False,
        },
    ]
    family_cal, spare = dav["calendars"]
    assert family_cal["rules"] == ["family"] and family_cal["synced"] is True
    assert family_cal["series"] is None and family_cal["name"] == "Family"
    assert spare == {
        "id": "/dav/x/",
        "name": "Spare",
        "role": None,
        "series": None,
        "rules": [],
        "synced": False,
    }


@pytest.mark.anyio
async def test_a_paused_rule_leaves_its_calendars_unsynced(tmp_path: Path) -> None:
    view = await _cached_status(tmp_path, family(paused=True))
    (cal, _) = view["accounts"][0]["calendars"]
    assert cal["rules"] == ["family"] and cal["synced"] is False


@pytest.mark.anyio
async def test_account_auth(tmp_path: Path) -> None:
    def failed(account: str | None, kind: str = "auth") -> RunRecord:
        return RunRecord(
            "family", NOW, NOW, "failed", 0, 0, 0, 0, 1, "AuthError", error_kind=kind, error_account=account
        )

    view = await _cached_status(tmp_path / "a", family(), runs=[failed("dav")])
    assert [a["auth"] for a in view["accounts"]] == ["unknown", "failed"]
    view = await _cached_status(tmp_path / "b", family(), runs=[failed("me")])
    assert [a["auth"] for a in view["accounts"]] == ["failed", "unknown"]
    view = await _cached_status(tmp_path / "c", family(), runs=[failed("me", kind="other")])
    assert [a["auth"] for a in view["accounts"]] == ["unknown", "unknown"]


def test_account_auth_without_runs_or_rules_is_unknown(tmp_path: Path) -> None:
    view = _status(tmp_path, [])
    assert [a["auth"] for a in view["accounts"]] == ["unknown", "unknown"]
    with reader(tmp_path) as r:
        view = build_status(config_for([]), r, NOW).to_json()
    assert [a["auth"] for a in view["accounts"]] == ["unknown", "unknown"]


def test_delivered_today_counts_writes_since_local_midnight(
    tmp_path: Path, local_zone: Callable[[str], None]
) -> None:
    # 20:00Z on the 24th is 04:00 on the 25th in Shanghai: "today" there began at 16:00Z.
    local_zone("Asia/Shanghai")
    now = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
    midnight = datetime(2026, 10, 24, 16, 0, tzinfo=UTC)

    def change(action: str, at: datetime) -> ChangeRecord:
        return ChangeRecord("family", "o1", action, at, "Lunch")

    changes = [
        change("created", midnight - timedelta(minutes=1)),  # yesterday there (but today in UTC)
        change("created", midnight),
        change("updated", midnight + timedelta(hours=1, microseconds=5)),
        change("reverted", now),
        change("deleted", now),  # not a delivery
        change("blocked", now),
    ]
    with Store(tmp_path / "state.db") as store:
        store.record_runs([_run("ok")], changes)
    with reader(tmp_path) as r:
        view = build_status(family(), r, now).to_json()
        assert view["summary"]["delivered_today"] == 3
        assert view["generated_at"] == now.isoformat()  # still the instant as given
        local_zone("UTC")
        assert build_status(family(), r, now).to_json()["summary"]["delivered_today"] == 4


@pytest.mark.anyio
async def test_upcoming_all_day_events_fall_on_local_days(
    tmp_path: Path, local_zone: Callable[[str], None]
) -> None:
    local_zone("Asia/Shanghai")
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for eid, first, end in [("yesterday", "2026-10-24", "2026-10-25"), ("today", "2026-10-25", "2026-10-26")]:
        fake.put_event(
            GCAL,
            {
                "id": eid,
                "status": "confirmed",
                "summary": eid,
                "start": {"date": first},
                "end": {"date": end},
            },
        )
    config = family()
    h = build_fake(tmp_path, fake, config)
    await h.engine.cycle()
    now = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)  # 07:00 on the 25th in Shanghai
    with reader(tmp_path) as r:
        view = build_upcoming(config, r, now, hours=12).to_json()
    assert [e["title"] for e in view["events"]] == ["today"]
    assert view["from"] == now.isoformat()  # the JSON keeps the instant as given


def test_changes_carry_their_detail(tmp_path: Path) -> None:
    detail = json.dumps({"fields": {"title": ["Lunch", "Team lunch"]}})
    with Store(tmp_path / "state.db") as store:
        store.record_runs(
            [_run("ok")],
            [
                ChangeRecord("family", "o1", "updated", NOW, "Team lunch", detail),
                ChangeRecord("family", "o2", "created", NOW, "Lunch"),
            ],
        )
    with reader(tmp_path) as r:
        changes = build_changes(family(), r).to_json()["changes"]
        runs = build_runs(family(), r).to_json()["runs"]
    assert [c["detail"] for c in changes] == [None, {"fields": {"title": ["Lunch", "Team lunch"]}}]
    assert runs[0]["error_account"] is None


# ----- the config text as the web UI sees it ---------------------------------------------------

UNMASKED_TOML = """\
accounts.top.token = "TOPTOKEN"

[google]
client_secret_file = "~/secret.json"

[accounts.icloud]
provider = "caldav"
server_url = "icloud"
username = "someone@example.com"  # the Apple ID
credential = { backend = "keychain", ref = "calsync:icloud" }

[accounts.other]
provider = "caldav"
server_url = "https://user:pw@dav.example.com:8443/path"
username = 'lit@example.com'
password = "PLAINPW"
credential.backend = "file"
credential.ref = "~/.pw-file"

[accounts.third.credential]
backend = "env"
ref = "PW_VAR"

[notify]
bark_url = "https://api.day.app/DEVICEKEY"
## bark_url = "https://api.day.app/HASHHASH"
# webhook_url = "https://hooks.example.com/OLDPATH"
webhook_url = \"\"\"https://hooks.example.com/
MULTILINE\"\"\"
failure_threshold = 3  # old bark_url = "https://api.day.app/TRAILING"
public_url="http://nas.lan:8766/x"
language = "en"

[[rules]]
name = "family"
source = { account = "me", calendar = "family@example.com" }
target = { account = "icloud", calendar = "Family", create_if_missing = true }
note = "PRIVATENOTE"
extra = { password = "INLINEPW" }

[rules.window]
past = "7d"

[foo]
password = "FOOPW"
"""

MASKED_TOML = """\
accounts.top.token = "(hidden)"

[google]
client_secret_file = "(hidden)"

[accounts.icloud]
provider = "caldav"
server_url = "icloud"
username = "(hidden)"  # (comment hidden)
credential = { backend = "keychain", ref = "(hidden)" }

[accounts.other]
provider = "caldav"
server_url = "https://dav.example.com:8443"
username = "(hidden)"
password = "(hidden)"
credential.backend = "file"
credential.ref = "(hidden)"

[accounts.third.credential]
backend = "env"
ref = "(hidden)"

[notify]
bark_url = "https://api.day.app"
# (comment hidden)
# (comment hidden)
webhook_url = "(hidden)"
failure_threshold = 3  # (comment hidden)
public_url="http://nas.lan:8766"
language = "en"

[[rules]]
name = "family"
source = { account = "me", calendar = "family@example.com" }
target = { account = "icloud", calendar = "Family", create_if_missing = true }
note = "(hidden)"
extra = { password = "(hidden)" }

[rules.window]
past = "7d"

[foo]
password = "(hidden)"
"""


def test_mask_config_text_masks_what_config_json_masks() -> None:
    from calsync.views import mask_config_text

    masked = mask_config_text(UNMASKED_TOML)
    assert masked == MASKED_TOML
    for secret in (
        "DEVICEKEY",
        "someone@",
        "lit@",
        "user:pw",
        "/path",
        "calsync:icloud",
        "pw-file",
        "PW_VAR",
        "PLAINPW",
        "TOPTOKEN",
        "HASHHASH",
        "TRAILING",
        "PRIVATENOTE",
        "INLINEPW",
        "FOOPW",
    ):
        assert secret not in masked
    for secret in ("secret.json", "OLDPATH", "MULTILINE", "nas.lan:8766/x"):
        assert secret not in masked
    assert mask_config_text(masked) == masked  # stable
