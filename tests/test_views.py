"""Read-only views over the state database (FakeGoogle + FakeTarget)."""

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest

from calsync.config import Config
from calsync.naming import object_id, rule_tag
from calsync.store import SCHEMA_VERSION, ChangeRecord, RunRecord, Store
from calsync.views import (
    StateReader,
    StateUnavailable,
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


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("icloud", "iCloud"),
        ("https://caldav.icloud.com/", "iCloud"),
        ("https://p42-caldav.icloud.com.cn/123/", "iCloud"),
        ("https://dav.example.com/cal/", "dav.example.com"),
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
    }
    assert view["accounts"] == [
        {"name": "me", "provider": "google", "host": "Google Calendar"},
        {"name": "dav", "provider": "caldav", "host": "unused"},
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
    assert list(changes[0]) == ["id", "run_id", "rule", "object_id", "action", "at", "title"]
    assert changes[0] == {
        "id": 4,
        "run_id": 2,
        "rule": "family",
        "object_id": object_id("family", GCAL, "review"),
        "action": "deleted",
        "at": NOW.isoformat(),
        "title": "Weekly review",
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


def test_blocked_attention_counts_the_blocked_deletions(tmp_path: Path) -> None:
    blocked = [ChangeRecord("family", f"o{i}", "blocked", NOW, None) for i in range(4)]
    view = _status(tmp_path, [_run("blocked", error="guard", kind="guard")], blocked)
    assert view["attention"] == [
        {
            "kind": "blocked",
            "rule": "family",
            "account": None,
            "message": "Sync paused: 4 deletions are waiting for your confirmation.",
            "command": "calsync run --rule family --allow-deletes",
        }
    ]


def test_blocked_attention_without_rows_has_no_count(tmp_path: Path) -> None:
    view = _status(tmp_path, [_run("blocked", error="guard", kind="guard")])
    assert view["attention"][0]["message"] == "Sync paused: deletions are waiting for your confirmation."


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
    assert view["attention"][0]["message"] == "Sync paused: 4 deletions are waiting for your confirmation."


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
    assert status["attention"][1]["message"] == "Sync paused: deletions are waiting for your confirmation."
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
