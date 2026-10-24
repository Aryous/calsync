from pathlib import Path

import pytest
from icalendar import Calendar

from calsync.engine import apply_target_delta
from calsync.naming import parse_resource_name
from calsync.render import inspect
from calsync.targets.base import CachedObject, TargetCache, TargetDelta
from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, Harness, build, build_fake, config_for, rule

pytestmark = pytest.mark.anyio


def seeded() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(
        GCAL,
        google_event(
            "standup",
            "Standup",
            "2026-10-23T09:30:00+08:00",
            "2026-10-23T09:35:00+08:00",
            recurrence=["RRULE:FREQ=DAILY;COUNT=21"],
        ),
    )
    fake.put_event(
        GCAL,
        google_event(
            "standup_20261025T013000Z",
            "Standup (late)",
            "2026-10-25T21:00:00+08:00",
            "2026-10-25T21:05:00+08:00",
            recurringEventId="standup",
            originalStartTime={"dateTime": "2026-10-25T09:30:00+08:00", "timeZone": "Asia/Shanghai"},
        ),
    )
    fake.put_event(
        GCAL, google_event("checkup", "Checkup", "2026-10-28T10:00:00+08:00", "2026-10-28T11:00:00+08:00")
    )
    return fake


async def managed(h: Harness) -> dict[str, bytes]:
    cal = await h.target.ensure_calendar("Family", create=False)
    delta = await h.target.changes(cal, None)
    return {o.href: o.data for o in await h.target.fetch(cal, list(delta.changed))}


# ----- pure ---------------------------------------------------------------


def test_apply_target_delta() -> None:
    mine = CachedObject('"e1"', "t", "o1", "h", None)
    pending = CachedObject(None, "t", "o2", "h", None)
    cache = TargetCache("c0", {"/a": mine, "/b": pending, "/c": mine, "/d": mine})
    need = apply_target_delta(
        cache, TargetDelta({"/a": '"e1"', "/b": '"e2"', "/c": '"x"', "/new": '"n"'}, ("/d",), False, "c1")
    )
    assert need == ["/c", "/new"]
    assert cache.objects["/b"].etag == '"e2"' and "/d" not in cache.objects
    full = apply_target_delta(cache, TargetDelta({"/a": '"e1"'}, (), True, "c2"))
    assert full == [] and list(cache.objects) == ["/a"]


def far_past() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(GCAL, google_event("old", "Old", "2026-01-05T09:00:00+08:00", "2026-01-05T09:30:00+08:00"))
    fake.put_event(GCAL, google_event("new", "New", "2026-10-25T09:00:00+08:00", "2026-10-25T09:30:00+08:00"))
    return fake


async def test_no_window_syncs_everything(tmp_path: Path) -> None:
    h = build_fake(tmp_path, far_past(), config_for([rule()]))
    res = (await h.engine.cycle()).rules["family"]
    assert res.created == 2 and res.out_of_window == 0


async def test_window_skips_and_counts(tmp_path: Path) -> None:
    cfg = config_for([rule(window={"past": "7d", "future": "180d"})])
    h = build_fake(tmp_path, far_past(), cfg)
    res = (await h.engine.cycle()).rules["family"]
    assert res.created == 1 and res.out_of_window == 1


# ----- against Radicale -----------------------------------------------------

pytest_e2e = pytest.mark.e2e


@pytest_e2e
async def test_first_cycle_creates_then_idles(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    h = build(tmp_path, radicale_url, dav_user, seeded(), config_for([rule()]))
    first = await h.engine.cycle()
    res = first.rules["family"]
    assert (res.status, res.created, res.updated, res.deleted) == ("ok", 2, 0, 0)

    bodies = await managed(h)
    assert len(bodies) == 2 and all(parse_resource_name(href) for href in bodies)
    series_body = next(b for b in bodies.values() if b"RRULE" in b)
    assert b"RECURRENCE-ID" in series_body and series_body.count(b"BEGIN:VALARM") == 2

    second = await h.engine.cycle()
    assert second.rules["family"].plan.empty
    mark = h.log.mark()
    third = await h.engine.cycle()
    assert third.rules["family"].plan.empty
    assert sorted(e.kind for e in h.log.since(mark)) == [
        "google-calendarlist",
        "google-events",
        "sync-collection",
    ]


@pytest_e2e
async def test_update_delete_and_foreign_untouched(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    fake = seeded()
    h = build(tmp_path, radicale_url, dav_user, fake, config_for([rule()]))
    await h.engine.cycle()
    cal = await h.target.ensure_calendar("Family", create=False)
    foreign = h.target.href_for(cal, "my-own-event.ics")
    await h.target.create(
        foreign,
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:mine\r\n"
            b"DTSTAMP:20261024T000000Z\r\nDTSTART:20261026T010000Z\r\nDTEND:20261026T020000Z\r\n"
            b"END:VEVENT\r\nEND:VCALENDAR\r\n"
        ),
    )

    fake.put_event(
        GCAL,
        google_event("checkup", "Checkup (moved)", "2026-10-29T10:00:00+08:00", "2026-10-29T11:00:00+08:00"),
    )
    res = (await h.engine.cycle()).rules["family"]
    assert (res.created, res.updated, res.deleted) == (0, 1, 0)
    assert any(b"Checkup (moved)" in b for b in (await managed(h)).values())

    fake.cancel_event(GCAL, "checkup")
    res = (await h.engine.cycle()).rules["family"]
    assert (res.created, res.updated, res.deleted) == (0, 0, 1)
    listing = await h.target.changes(cal, None)
    assert len(listing.changed) == 1  # only the recurring series is still managed
    (still_there,) = await h.target.fetch(cal, [foreign])
    assert b"UID:mine" in still_there.data


@pytest_e2e
async def test_dry_run_writes_nothing(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    h = build(tmp_path, radicale_url, dav_user, seeded(), config_for([rule()]))
    await h.target.ensure_calendar("Family", create=True)
    result = await h.engine.cycle(dry_run=True)
    assert len(result.rules["family"].plan.create) == 2
    assert await managed(h) == {}
    assert h.store.latest_runs() == []


@pytest_e2e
async def test_delete_guard_blocks_mass_delete(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for i in range(12):
        fake.put_event(
            GCAL,
            google_event(
                f"e{i}",
                f"E{i}",
                f"2026-10-{25 + i % 3}T09:00:00+08:00",
                f"2026-10-{25 + i % 3}T09:05:00+08:00",
            ),
        )
    h = build(tmp_path, radicale_url, dav_user, fake, config_for([rule()]))
    await h.engine.cycle()
    for i in range(12):
        fake.cancel_event(GCAL, f"e{i}")
    blocked = (await h.engine.cycle()).rules["family"]
    assert blocked.status == "blocked" and blocked.error_kind == "guard"
    assert sorted(blocked.blocked_titles) == sorted(f"E{i}" for i in range(12))
    assert len(await managed(h)) == 12
    forced = (await h.engine.cycle(allow_deletes=True)).rules["family"]
    assert forced.deleted == 12 and await managed(h) == {}


@pytest_e2e
async def test_lost_state_reconciles_without_duplicates(
    tmp_path: Path, radicale_url: str, dav_user: str
) -> None:
    fake = seeded()
    h = build(tmp_path, radicale_url, dav_user, fake, config_for([rule()]))
    await h.engine.cycle()
    h.store.close()
    (tmp_path / "state.db").unlink()
    fresh = build(tmp_path, radicale_url, dav_user, fake, config_for([rule()]))
    res = (await fresh.engine.cycle()).rules["family"]
    # updated == 0 relies on the server keeping X-CALSYNC-HASH (Radicale does; for iCloud see
    # spec §13 item 2 - if it strips the property, each resource is rewritten once here).
    assert (res.status, res.created, res.updated, res.deleted) == ("ok", 0, 0, 0)
    assert "calendar-multiget" in fresh.log.kinds()
    assert len(await managed(fresh)) == 2


@pytest_e2e
async def test_failing_source_only_fails_its_rules(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    rules = [rule("good"), {**rule("bad"), "source": {"account": "broken", "calendar": "Other"}}]
    h = build(
        tmp_path, radicale_url, dav_user, seeded(), config_for(rules, extra_google=True), broken_google=True
    )
    result = await h.engine.cycle()
    assert result.rules["good"].status == "ok" and result.rules["good"].created == 2
    assert result.rules["bad"].status == "failed" and result.rules["bad"].error_kind == "auth"
    assert not result.ok
    assert {r.rule: r.status for r in h.store.latest_runs()} == {"bad": "failed", "good": "ok"}


@pytest_e2e
async def test_two_rules_share_one_target_calendar(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    fake = seeded()
    fake.add_calendar("work@group.calendar.google.com", "Work")
    fake.put_event(
        "work@group.calendar.google.com",
        google_event("w1", "Standup", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00"),
    )
    rules = [rule("family"), rule("work", source="Work")]
    h = build(tmp_path, radicale_url, dav_user, fake, config_for(rules))
    result = await h.engine.cycle()
    assert result.rules["family"].created == 2 and result.rules["work"].created == 1
    fake.cancel_event("work@group.calendar.google.com", "w1")
    result = await h.engine.cycle()
    assert result.rules["work"].deleted == 1 and result.rules["family"].plan.empty
    titles = [inspect(b) for b in (await managed(h)).values()]
    assert len(titles) == 2 and {t.rule for t in titles if t} == {"family"}


def with_attendees() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(
        GCAL,
        google_event(
            "m1",
            "Visit",
            "2026-10-28T10:00:00+08:00",
            "2026-10-28T10:30:00+08:00",
            attendees=[{"email": "alice@example.com", "displayName": "Alice"}],
            hangoutLink="https://meet.example.com/abc",
        ),
    )
    return fake


async def test_default_fields_write_notes_not_invitations(tmp_path: Path) -> None:
    h = build_fake(tmp_path, with_attendees(), config_for([rule()]))
    await h.engine.cycle()
    [body] = h.target.managed(h.target.calendars["Family"]).values()
    text = body.decode()
    assert "Attendees: Alice <alice@example.com>" in text.replace("\r\n ", "")
    assert "URL:https://meet.example.com/abc" in text
    assert "ATTENDEE" not in text


async def test_busy_rule_hides_content(tmp_path: Path) -> None:
    h = build_fake(tmp_path, with_attendees(), config_for([rule(fields={"content": "busy"})]))
    await h.engine.cycle()
    [body] = h.target.managed(h.target.calendars["Family"]).values()
    [event] = Calendar.from_ical(body).walk("VEVENT")
    assert event["summary"] == "Busy"
    # The alarm stays (its own DESCRIPTION is the title, "Busy"); the event carries no content.
    assert event.walk("VALARM")
    assert "description" not in event and "location" not in event and "url" not in event
    assert b"Visit" not in body and b"alice@example.com" not in body and b"meet.example.com" not in body
