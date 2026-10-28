"""Events by date range with per-rule delivery state, and one event's detail (FakeGoogle +
FakeTarget, synthetic data)."""

import base64
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from calsync.config import Config
from calsync.events_view import NotFound, build_event, build_events, event_id
from calsync.store import RunRecord
from calsync.views import StateReader
from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, NOW, build_fake, config_for, rule

pytestmark = pytest.mark.anyio

TZ = "+08:00"
# Two weeks, Monday to Monday (local +08:00).
FROM = datetime.fromisoformat(f"2026-10-26T00:00:00{TZ}")
TO = datetime.fromisoformat(f"2026-11-09T00:00:00{TZ}")


def yoga() -> list[dict[str, Any]]:
    """Tuesdays and Thursdays at 19:00 from 2026-10-20; the 29th moved to the 30th (and renamed),
    the 3 November cancelled."""
    master = google_event(
        "yoga",
        "Yoga",
        f"2026-10-20T19:00:00{TZ}",
        f"2026-10-20T20:00:00{TZ}",
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=10"],
        location="Studio",
    )
    moved = google_event(
        "yoga_20261029T110000Z",
        "Yoga (moved)",
        f"2026-10-30T19:00:00{TZ}",
        f"2026-10-30T20:00:00{TZ}",
        recurringEventId="yoga",
        originalStartTime={"dateTime": f"2026-10-29T19:00:00{TZ}", "timeZone": "Asia/Shanghai"},
        location="Studio",
    )
    cancelled = {
        "id": "yoga_20261103T110000Z",
        "status": "cancelled",
        "recurringEventId": "yoga",
        "originalStartTime": {"dateTime": f"2026-11-03T19:00:00{TZ}", "timeZone": "Asia/Shanghai"},
    }
    return [master, moved, cancelled]


def seeded() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for event in yoga():
        fake.put_event(GCAL, event)
    fake.put_event(
        GCAL,
        google_event(
            "dinner",
            "Dinner",
            f"2026-10-28T18:00:00{TZ}",
            f"2026-10-28T19:30:00{TZ}",
            location="Cafe",
            reminders={"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]},
        ),
    )
    fake.put_event(
        GCAL, google_event("review", "Review", f"2026-12-20T10:00:00{TZ}", f"2026-12-20T11:00:00{TZ}")
    )
    return fake


def two_rules(**home: Any) -> Config:
    """``family`` copies everything to Family; ``home`` copies the same calendar to Home as busy
    blocks without alarms."""
    return config_for(
        [
            rule("family", alarms=["-10m", "0m"]),
            rule("home", target="Home", alarms="strip", fields={"content": "busy"}, **home),
        ]
    )


async def synced(tmp_path: Path, config: Config | None = None) -> tuple[Any, FakeGoogle]:
    fake = seeded()
    h = build_fake(tmp_path, fake, config or two_rules())
    await h.engine.cycle()
    return h, fake


def events(
    tmp_path: Path, config: Config, start: datetime = FROM, end: datetime = TO, **kw: Any
) -> list[dict]:
    with StateReader(tmp_path / "state.db") as reader:
        return build_events(config, reader, start, end, now=NOW, **kw).to_json()["events"]


def detail(tmp_path: Path, config: Config, eid: str) -> dict[str, Any]:
    with StateReader(tmp_path / "state.db") as reader:
        return build_event(config, reader, eid, NOW).to_json()


def states(event: dict[str, Any]) -> dict[str, str]:
    return {d["rule"]: d["state"] for d in event["deliveries"]}


# ----- expansion and grouping --------------------------------------------------------------------


async def test_a_weekly_series_with_an_exception_and_a_cancellation(tmp_path: Path) -> None:
    await synced(tmp_path)
    found = events(tmp_path, two_rules())
    assert [(e["title"], e["start"]) for e in found] == [
        ("Yoga", f"2026-10-27T19:00:00{TZ}"),
        ("Dinner", f"2026-10-28T18:00:00{TZ}"),
        ("Yoga (moved)", f"2026-10-30T19:00:00{TZ}"),  # not on the 29th
        ("Yoga", f"2026-11-05T19:00:00{TZ}"),  # the 3rd is cancelled
    ]
    assert [e["recurring"] for e in found] == [True, False, True, True]
    assert len({e["id"] for e in found}) == 4


async def test_one_source_event_with_two_rules_is_one_event_with_two_deliveries(tmp_path: Path) -> None:
    await synced(tmp_path)
    dinner = next(e for e in events(tmp_path, two_rules()) if e["title"] == "Dinner")
    assert dinner == {
        "id": dinner["id"],
        "title": "Dinner",
        "start": f"2026-10-28T18:00:00{TZ}",
        "end": f"2026-10-28T19:30:00{TZ}",
        "all_day": False,
        "location": "Cafe",
        "recurring": False,
        "alarms_minutes_before": [30],  # the source's own
        "source": {"account": "me", "provider": "google", "calendar": "Family", "calendar_name": "Family"},
        "deliveries": [
            {
                "rule": "family",
                "target": {
                    "account": "dav",
                    "provider": "caldav",
                    "calendar": "Family",
                    "calendar_name": "Family",
                },
                "state": "delivered",
                "delivered_at": NOW.isoformat(),
                "content": "full",
                "alarms_minutes_before": [10, 0],
            },
            {
                "rule": "home",
                "target": {
                    "account": "dav",
                    "provider": "caldav",
                    "calendar": "Home",
                    "calendar_name": "Home",
                },
                "state": "delivered",
                "delivered_at": NOW.isoformat(),
                "content": "busy",
                "alarms_minutes_before": [],
            },
        ],
    }


async def test_an_event_without_location_reads_null(tmp_path: Path) -> None:
    await synced(tmp_path)
    found = events(tmp_path, two_rules(), end=datetime.fromisoformat(f"2026-12-31T00:00:00{TZ}"))
    review = next(e for e in found if e["title"] == "Review")
    assert review["location"] is None


async def test_the_view_shape(tmp_path: Path) -> None:
    await synced(tmp_path)
    with StateReader(tmp_path / "state.db") as reader:
        body = build_events(two_rules(), reader, FROM, TO, now=NOW).to_json()
    assert list(body) == ["from", "to", "schema", "truncated", "events"]
    assert body["from"] == FROM.isoformat() and body["to"] == TO.isoformat()
    assert body["schema"]["current"] is True and body["truncated"] is False


async def test_rule_and_target_filters_restrict_the_deliveries(tmp_path: Path) -> None:
    await synced(tmp_path)
    config = two_rules()
    by_rule = events(tmp_path, config, rule="home")
    by_target = events(tmp_path, config, target=("dav", "Family"))
    assert len(by_rule) == 4 and all([d["rule"] for d in e["deliveries"]] == ["home"] for e in by_rule)
    assert len(by_target) == 4 and all([d["rule"] for d in e["deliveries"]] == ["family"] for e in by_target)
    assert events(tmp_path, config, target=("dav", "Nowhere")) == []


async def test_query_matches_title_and_location_case_insensitively(tmp_path: Path) -> None:
    await synced(tmp_path)
    config = two_rules()
    assert [e["title"] for e in events(tmp_path, config, query="yOgA")] == ["Yoga", "Yoga (moved)", "Yoga"]
    assert [e["title"] for e in events(tmp_path, config, query="cafe")] == ["Dinner"]
    assert events(tmp_path, config, query="nothing like it") == []


# ----- delivery states -----------------------------------------------------------------------------


async def changed_dinner(tmp_path: Path) -> Any:
    """Dinner renamed in the source; the mirror has it, no copy was written yet."""
    h, fake = await synced(tmp_path)
    fake.put_event(
        GCAL,
        google_event(
            "dinner",
            "Late dinner",
            f"2026-10-28T18:00:00{TZ}",
            f"2026-10-28T19:30:00{TZ}",
            location="Cafe",
            reminders={"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]},
        ),
    )
    await h.engine.cycle(dry_run=True)
    return h


def dinner(tmp_path: Path, config: Config) -> dict[str, Any]:
    return next(e for e in events(tmp_path, config) if e["title"] in ("Dinner", "Late dinner"))


def run(rule_name: str, status: str, *, failed: int = 0, kind: str | None = None) -> RunRecord:
    return RunRecord(rule_name, NOW, NOW, status, 0, 0, 0, failed, 1, "boom", error_kind=kind)


async def test_delivered_and_pending(tmp_path: Path) -> None:
    h = await changed_dinner(tmp_path)
    assert states(dinner(tmp_path, two_rules())) == {
        "family": "pending",
        "home": "delivered",
    }  # busy: same copy
    await h.engine.cycle()
    assert states(dinner(tmp_path, two_rules())) == {"family": "delivered", "home": "delivered"}


async def test_paused_and_retiring(tmp_path: Path) -> None:
    await changed_dinner(tmp_path)
    assert states(dinner(tmp_path, two_rules(paused=True)))["home"] == "paused"
    assert states(dinner(tmp_path, two_rules(retire=True)))["home"] == "retiring"
    both = config_for([rule("family", paused=True, retire=True)])
    assert states(dinner(tmp_path, both)) == {"family": "paused"}


async def test_out_of_window(tmp_path: Path) -> None:
    await synced(tmp_path)
    config = two_rules(window={"past": "1d", "future": "30d"})
    found = events(tmp_path, config, end=datetime.fromisoformat(f"2026-12-31T00:00:00{TZ}"))
    review = next(e for e in found if e["title"] == "Review")
    assert states(review) == {"family": "delivered", "home": "out_of_window"}


@pytest.mark.parametrize(
    ("record", "state"),
    [
        (run("family", "blocked", kind="guard"), "blocked"),
        (run("family", "failed", kind="other"), "failing"),
        (run("family", "failed", kind="auth"), "failing"),
        (run("family", "partial", failed=2), "failing"),
        (run("family", "partial"), "pending"),
        (run("family", "ok"), "pending"),
    ],
)
async def test_blocked_and_failing_only_when_the_copy_is_not_up_to_date(
    tmp_path: Path, record: RunRecord, state: str
) -> None:
    h = await changed_dinner(tmp_path)
    h.store.record_runs([record])
    assert states(dinner(tmp_path, two_rules()))["family"] == state
    yoga_event = events(tmp_path, two_rules())[0]
    assert states(yoga_event)["family"] == "delivered"  # up to date: the run does not matter


async def test_unknown_when_the_schema_is_not_current(tmp_path: Path) -> None:
    h, _ = await synced(tmp_path)
    h.store.close()
    with closing(sqlite3.connect(tmp_path / "state.db")) as db, db:
        db.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
    found = events(tmp_path, two_rules(paused=True))
    assert {s for e in found for s in states(e).values()} == {"unknown"}


async def test_delivered_at_is_the_newest_write(tmp_path: Path) -> None:
    later = NOW + timedelta(hours=1)
    h = await changed_dinner(tmp_path)
    h.engine._clock = lambda: later  # type: ignore[attr-defined]
    await h.engine.cycle()
    by_rule = {d["rule"]: d["delivered_at"] for d in dinner(tmp_path, two_rules())["deliveries"]}
    assert by_rule == {"family": later.isoformat(), "home": NOW.isoformat()}


# ----- ids and the detail ------------------------------------------------------------------------------


async def test_ids_round_trip(tmp_path: Path) -> None:
    await synced(tmp_path)
    config = two_rules()
    for event in events(tmp_path, config):
        shown = detail(tmp_path, config, event["id"])
        assert {k: shown[k] for k in event if k != "deliveries"} == {
            k: v for k, v in event.items() if k != "deliveries"
        }
        assert [d["state"] for d in shown["deliveries"]] == [d["state"] for d in event["deliveries"]]


async def test_all_day_instances_round_trip(tmp_path: Path) -> None:
    fake = seeded()
    fake.put_event(
        GCAL,
        {
            "id": "bins",
            "status": "confirmed",
            "summary": "Bins out",
            "start": {"date": "2026-10-26"},
            "end": {"date": "2026-10-27"},
            "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"],
        },
    )
    config = config_for([rule("family")])
    h = build_fake(tmp_path, fake, config)
    await h.engine.cycle()
    bins = [e for e in events(tmp_path, config) if e["title"] == "Bins out"]
    assert [(e["start"], e["all_day"]) for e in bins] == [("2026-10-26", True), ("2026-11-02", True)]
    for event in bins:
        shown = detail(tmp_path, config, event["id"])
        assert (shown["start"], shown["deliveries"][0]["state"]) == (event["start"], "delivered")


def test_the_id_is_url_safe_base64_of_the_source_key() -> None:
    eid = event_id("me", GCAL, "yoga", f"2026-10-27T19:00:00{TZ}")
    assert "=" not in eid and "/" not in eid and "+" not in eid
    padded = eid + "=" * (-len(eid) % 4)
    assert base64.urlsafe_b64decode(padded).decode() == f"me/{GCAL}/yoga@2026-10-27T19:00:00{TZ}"
    assert base64.urlsafe_b64decode(event_id("me", GCAL, "dinner", None) + "==").decode() == (
        f"me/{GCAL}/dinner"
    )


@pytest.mark.parametrize(
    "eid",
    [
        "not base64!",
        "",
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        event_id("me", GCAL, "nope", None),
        event_id("me", GCAL, "dinner", "2026-10-28T18:00:00+08:00"),  # not recurring
        event_id("me", GCAL, "yoga", None),  # recurring: an instance is needed
        event_id("me", GCAL, "yoga", "2026-10-29T19:00:00+08:00"),  # moved away
        event_id("me", GCAL, "yoga", "2026-11-03T19:00:00+08:00"),  # cancelled
        event_id("me", GCAL, "yoga", "not a time"),
        event_id("me", "other@group.calendar.google.com", "dinner", None),
        event_id("ghost", GCAL, "dinner", None),
    ],
)
async def test_unknown_or_malformed_ids_are_not_found(tmp_path: Path, eid: str) -> None:
    await synced(tmp_path)
    with pytest.raises(NotFound):
        detail(tmp_path, two_rules(), eid)


async def test_event_show_compares_the_original_with_each_copy(tmp_path: Path) -> None:
    await changed_dinner(tmp_path)
    shown = detail(tmp_path, two_rules(), dinner(tmp_path, two_rules())["id"])
    assert shown["original"] == {
        "title": "Late dinner",
        "start": f"2026-10-28T18:00:00{TZ}",
        "end": f"2026-10-28T19:30:00{TZ}",
        "all_day": False,
        "location": "Cafe",
        "alarms_minutes_before": [30],
        "recurring": False,
    }
    family, home = shown["deliveries"]
    assert family["state"] == "pending" and family["differs"] == ["title"]
    assert family["copy"]["title"] == "Dinner" and family["copy"]["alarms_minutes_before"] == [10, 0]
    assert home["state"] == "delivered" and home["differs"] == []
    assert home["copy"]["title"] == "Busy" and home["copy"]["location"] is None


async def test_event_show_of_a_moved_instance(tmp_path: Path) -> None:
    await synced(tmp_path)
    moved = next(e for e in events(tmp_path, two_rules()) if e["title"] == "Yoga (moved)")
    shown = detail(tmp_path, two_rules(), moved["id"])
    assert shown["start"] == f"2026-10-30T19:00:00{TZ}" and shown["recurring"] is True
    assert shown["original"]["title"] == "Yoga" and shown["original"]["recurring"] is True  # the series
    assert [d["differs"] for d in shown["deliveries"]] == [[], []]


async def test_event_show_without_a_stored_copy(tmp_path: Path) -> None:
    h, fake = await synced(tmp_path, config_for([rule("family")]))
    fake.put_event(GCAL, google_event("new", "New", f"2026-10-29T08:00:00{TZ}", f"2026-10-29T09:00:00{TZ}"))
    await h.engine.cycle(dry_run=True)
    config = config_for([rule("family")])
    new = next(e for e in events(tmp_path, config) if e["title"] == "New")
    (delivery,) = detail(tmp_path, config, new["id"])["deliveries"]
    assert delivery == {**delivery, "state": "pending", "delivered_at": None, "copy": None, "differs": []}


# ----- performance -------------------------------------------------------------------------------------


async def test_a_62_day_range_over_300_series_is_fast(tmp_path: Path) -> None:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for i in range(300):
        day = 1 + i % 28
        recurrence = ["RRULE:FREQ=DAILY"] if i % 3 == 0 else ["RRULE:FREQ=WEEKLY"] if i % 3 == 1 else None
        fake.put_event(
            GCAL,
            google_event(
                f"e{i}",
                f"Event {i}",
                f"2026-10-{day:02d}T09:00:00{TZ}",
                f"2026-10-{day:02d}T10:00:00{TZ}",
                recurrence=recurrence,
            ),
        )
    config = two_rules()
    h = build_fake(tmp_path, fake, config)
    await h.engine.cycle()
    start = datetime(2026, 10, 24, tzinfo=UTC)
    started = time.monotonic()
    found = events(tmp_path, config, start=start, end=start + timedelta(days=62))
    elapsed = time.monotonic() - started
    assert len(found) > 6000
    # Catches an unbounded or quadratic path (minutes), not a benchmark: generous for a loaded
    # machine (it takes well under a second).
    assert elapsed < 15.0, elapsed


# ----- fix round 1 ------------------------------------------------------------------------------------


def _all_day_event(eid: str, title: str, first: str, end: str) -> dict[str, Any]:
    return {
        "id": eid,
        "status": "confirmed",
        "summary": title,
        "start": {"date": first},
        "end": {"date": end},
    }


@pytest.mark.parametrize("offset", ["+08:00", "-05:00"])
async def test_a_local_day_view_shows_that_days_all_day_events(tmp_path: Path, offset: str) -> None:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for eid, first, end in [
        ("before", "2026-10-27", "2026-10-28"),
        ("on", "2026-10-28", "2026-10-29"),
        ("after", "2026-10-29", "2026-10-30"),
        ("trip", "2026-10-26", "2026-10-31"),  # five days over the 28th
    ]:
        fake.put_event(GCAL, _all_day_event(eid, eid.capitalize(), first, end))
    config = config_for([rule("family")])
    h = build_fake(tmp_path, fake, config)
    await h.engine.cycle()
    start = datetime.fromisoformat(f"2026-10-28T00:00:00{offset}")
    found = events(tmp_path, config, start=start, end=start + timedelta(days=1))
    assert sorted(e["title"] for e in found) == ["On", "Trip"]
    for event in found:  # the ids still name the occurrence
        assert detail(tmp_path, config, event["id"])["start"] == event["start"]


@pytest.mark.parametrize(
    "start",
    [
        datetime(2040, 1, 1, tzinfo=UTC),  # beyond ten years from now
        datetime(2010, 1, 1, tzinfo=UTC),  # ten years before
        datetime(9999, 12, 30, tzinfo=UTC),  # the default week after it would overflow
        datetime(1, 1, 1, tzinfo=UTC),
    ],
)
def test_the_range_must_lie_within_ten_years_of_now(start: datetime) -> None:
    from calsync.events_view import event_range

    with pytest.raises(ValueError, match="within 10 years"):
        event_range(start, None, NOW)


def test_a_default_end_beyond_the_horizon_names_from() -> None:
    from calsync.events_view import event_range

    start = NOW + timedelta(days=3650)  # inside; a week later is not
    with pytest.raises(ValueError, match=r"^from: the default end \(a week later\) must be within 10 years"):
        event_range(start, None, NOW)


def test_the_range_end_must_lie_within_ten_years_of_now() -> None:
    from calsync.events_view import event_range

    start = NOW + timedelta(days=3650)
    assert event_range(start, start + timedelta(days=1), NOW)[0] == start  # still inside
    with pytest.raises(ValueError, match="within 10 years"):
        event_range(start, start + timedelta(days=10), NOW)


@pytest.mark.parametrize(
    "instance",
    [
        "9000-01-01T19:00:00+08:00",  # a far instance: refused before expanding to it
        "9999-12-31",  # the day after it is not representable
        "9999-12-31T23:59:59+00:00",
        "0001-01-01T00:00:00+08:00",
        "2040-01-03T19:00:00+08:00",
    ],
)
async def test_ids_naming_far_instances_are_not_found(tmp_path: Path, instance: str) -> None:
    fake = seeded()
    fake.put_event(
        GCAL,
        google_event(
            "daily",
            "Daily",
            f"2026-10-20T19:00:00{TZ}",
            f"2026-10-20T20:00:00{TZ}",
            recurrence=["RRULE:FREQ=DAILY"],
        ),
    )
    h = build_fake(tmp_path, fake, two_rules())
    await h.engine.cycle()
    started = time.monotonic()
    with pytest.raises(NotFound):
        detail(tmp_path, two_rules(), event_id("me", GCAL, "daily", instance))
    assert time.monotonic() - started < 5.0  # bounded, not expanded from 2026 onwards (generous)


async def test_a_series_cut_at_the_expansion_limit_marks_the_view_truncated(tmp_path: Path) -> None:
    import io

    from calsync.cli import _print_view  # pyright: ignore[reportPrivateUsage]

    fake = seeded()
    fake.put_event(
        GCAL,
        google_event(
            "hourly",
            "Hourly",
            f"2026-10-20T00:00:00{TZ}",
            f"2026-10-20T00:10:00{TZ}",
            recurrence=["RRULE:FREQ=HOURLY"],
        ),
    )
    config = config_for([rule("family")])
    h = build_fake(tmp_path, fake, config)
    await h.engine.cycle()
    start = datetime(2026, 10, 24, tzinfo=UTC)
    with StateReader(tmp_path / "state.db") as reader:
        cut = build_events(config, reader, start, start + timedelta(days=62), now=NOW)
        whole = build_events(config, reader, start, start + timedelta(days=7), now=NOW)
    body = cut.to_json()
    assert body["truncated"] is True
    assert sum(e["title"] == "Hourly" for e in body["events"]) == 1000
    assert whole.to_json()["truncated"] is False
    out = io.StringIO()
    _print_view(cut, out)
    assert out.getvalue().splitlines()[-1] == (
        "note: a recurring event has more instances than shown (at most 1000 per event); narrow the range"
    )
