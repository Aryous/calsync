"""Engine edge cases against FakeGoogle + an in-memory FakeTarget (no Radicale)."""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

import calsync.engine
import calsync.store as store_mod
from calsync.errors import ConflictError, ForbiddenError, TransientError
from calsync.http import RequestLog, make_client
from calsync.model import Series
from calsync.naming import object_id, resource_name
from calsync.sources.base import CalendarInfo, CalendarMeta, RawEvent, SourcePull
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import StaticToken
from tests.factories import google_event
from tests.fakes.google import BASE_URL, FakeGoogle
from tests.fakes.target import FakeTarget
from tests.harness import GCAL, FakeHarness, build_fake, config_for, rule

pytestmark = pytest.mark.anyio


def simple() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(
        GCAL, google_event("checkup", "Checkup", "2026-10-28T10:00:00+08:00", "2026-10-28T11:00:00+08:00")
    )
    fake.put_event(
        GCAL, google_event("pills", "Reminders", "2026-10-25T08:00:00+08:00", "2026-10-25T08:05:00+08:00")
    )
    return fake


# ----- 1. detection failure is isolated -----------------------------------


async def test_detect_failure_fails_only_its_rule(tmp_path: Path) -> None:
    h = build_fake(tmp_path, simple(), config_for([rule("a", target="A"), rule("b", target="B")]))
    h.target.fail_changes["A"] = TransientError("sync-collection timed out")
    result = await h.engine.cycle()
    a, b = result.rules["a"], result.rules["b"]
    assert a.status == "failed" and a.error == "TransientError: sync-collection timed out"
    assert a.error_kind == "other"
    assert b.status == "ok" and b.created == 2
    assert {r.rule: r.status for r in h.store.latest_runs()} == {"a": "failed", "b": "ok"}
    # The next cycle recovers.
    again = (await h.engine.cycle()).rules["a"]
    assert again.status == "ok" and again.created == 2


# ----- 2. unparseable source series keep their target copy -----------------


async def test_unparseable_series_keeps_its_target_copy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = simple()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    assert (await h.engine.cycle()).rules["family"].created == 2
    cal = h.target.calendars["Family"]
    before = h.target.managed(cal)

    broken = google_event("pills", "Reminders", "2026-10-25T08:00:00+08:00", "2026-10-25T08:05:00+08:00")
    del broken["start"]
    fake.put_event(GCAL, broken)
    with caplog.at_level(logging.WARNING):
        res = (await h.engine.cycle()).rules["family"]
    assert res.status == "ok" and res.protected == 1
    assert res.plan is not None and res.plan.empty
    assert h.target.managed(cal) == before
    warnings = [r.getMessage() for r in caplog.records if "protect" in r.getMessage()]
    assert len(warnings) == 1 and "pills" in warnings[0] and "Reminders" not in warnings[0]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        again = (await h.engine.cycle()).rules["family"]
    assert again.protected == 1 and again.plan is not None and again.plan.empty
    assert not [r for r in caplog.records if "protect" in r.getMessage()]  # warned once per process


async def test_delete_guard_ignores_protected_actuals(tmp_path: Path) -> None:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for i in range(4):
        fake.put_event(
            GCAL, google_event(f"e{i}", f"E{i}", "2026-10-25T09:00:00+08:00", "2026-10-25T09:05:00+08:00")
        )
    guarded = rule(delete_guard={"max_ratio": 0.8, "max_count": 10})
    h = build_fake(tmp_path, fake, config_for([guarded]))
    await h.engine.cycle()
    broken = google_event("e0", "E0", "2026-10-25T09:00:00+08:00", "2026-10-25T09:05:00+08:00")
    del broken["start"]
    fake.put_event(GCAL, broken)
    for i in (1, 2, 3):
        fake.cancel_event(GCAL, f"e{i}")
    # 3 of 4 managed would pass max_ratio=0.8; 3 of the 3 deletable ones must not.
    res = (await h.engine.cycle()).rules["family"]
    assert res.status == "blocked" and res.protected == 1
    assert "3 of 3" in (res.error or "")


# ----- 3. per-calendar source isolation -------------------------------------


async def test_unknown_source_calendar_fails_only_its_rule(tmp_path: Path) -> None:
    rules = [rule("good", target="Good"), rule("bad", source="No such calendar", target="Bad")]
    h = build_fake(tmp_path, simple(), config_for(rules))
    result = await h.engine.cycle()
    good, bad = result.rules["good"], result.rules["bad"]
    assert good.status == "ok" and good.created == 2
    assert len(h.target.managed(h.target.calendars["Good"])) == 2
    assert bad.status == "failed" and bad.error_kind == "config" and "ConfigError" in (bad.error or "")
    assert {r.rule: r.status for r in h.store.latest_runs()} == {"bad": "failed", "good": "ok"}


async def test_missing_source_calendar_404_fails_only_its_rule(tmp_path: Path) -> None:
    fake = simple()
    fake.add_calendar("gone@group.calendar.google.com", "Gone")
    fake.put_event(
        "gone@group.calendar.google.com",
        google_event("nap", "Nap", "2026-10-26T13:00:00+08:00", "2026-10-26T14:00:00+08:00"),
    )
    rules = [rule("good", target="Good"), rule("bad", source="gone@group.calendar.google.com", target="Bad")]
    h = build_fake(tmp_path, fake, config_for(rules))
    first = await h.engine.cycle()
    assert first.ok
    fake.remove_calendar("gone@group.calendar.google.com")
    fake.put_event(
        GCAL, google_event("walk", "Walk", "2026-10-26T18:00:00+08:00", "2026-10-26T18:30:00+08:00")
    )
    result = await h.engine.cycle()
    good, bad = result.rules["good"], result.rules["bad"]
    assert good.status == "ok" and good.created == 1
    assert bad.status == "failed" and "NotFoundError" in (bad.error or "")
    assert len(h.target.managed(h.target.calendars["Bad"])) == 1  # a failed rule deletes nothing
    _, mirrors = h.store.load_source("me")
    assert set(mirrors["gone@group.calendar.google.com"]) == {"nap"}  # failed pull persisted nothing
    assert "walk" in mirrors[GCAL]


# ----- 4. conflict fallbacks when the resource is gone ----------------------


def once(op: str, action: Callable[[str], None]) -> Callable[[str, str], None]:
    """A before_write hook that runs ``action(href)`` on the first ``op`` only."""
    fired: list[str] = []

    def hook(kind: str, href: str) -> None:
        if kind == op and not fired:
            fired.append(href)
            action(href)

    return hook


async def test_delete_conflict_on_vanished_resource_counts_as_deleted(tmp_path: Path) -> None:
    fake = simple()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    fake.cancel_event(GCAL, "checkup")
    h.target.before_write = once("delete", lambda href: h.target.objects.pop(href))
    res = (await h.engine.cycle()).rules["family"]
    assert (res.status, res.deleted, res.failed) == ("ok", 1, 0)
    assert len(h.target.managed(h.target.calendars["Family"])) == 1
    assert (await h.engine.cycle()).rules["family"].plan.empty


async def test_update_conflict_on_vanished_resource_recreates_it(tmp_path: Path) -> None:
    fake = simple()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    fake.put_event(
        GCAL,
        google_event("checkup", "Checkup (moved)", "2026-10-29T10:00:00+08:00", "2026-10-29T11:00:00+08:00"),
    )
    h.target.before_write = once("update", lambda href: h.target.objects.pop(href))
    res = (await h.engine.cycle()).rules["family"]
    assert (res.status, res.updated, res.failed) == ("ok", 1, 0)
    bodies = h.target.managed(h.target.calendars["Family"]).values()
    assert any(b"Checkup (moved)" in b for b in bodies)
    assert (await h.engine.cycle()).rules["family"].plan.empty


async def test_create_conflict_on_missing_resource_retries_create(tmp_path: Path) -> None:
    h = build_fake(tmp_path, simple(), config_for([rule()]))

    def spurious(href: str) -> None:
        raise ConflictError(f"412 create {href}")

    h.target.before_write = once("create", spurious)
    res = (await h.engine.cycle()).rules["family"]
    assert (res.status, res.created, res.failed) == ("ok", 2, 0)
    assert len(h.target.managed(h.target.calendars["Family"])) == 2
    assert [op for op, _ in h.target.calls].count("create") == 3


async def test_create_conflict_on_existing_resource_updates_it(tmp_path: Path) -> None:
    h = build_fake(tmp_path, simple(), config_for([rule()]))
    h.target.before_write = once("create", lambda href: h.target.put(href, b"stale copy"))
    res = (await h.engine.cycle()).rules["family"]
    assert (res.status, res.created, res.failed) == ("ok", 2, 0)
    bodies = h.target.managed(h.target.calendars["Family"]).values()
    assert len(bodies) == 2 and b"stale copy" not in b"".join(bodies)
    assert "update" in [op for op, _ in h.target.calls]
    assert (await h.engine.cycle()).rules["family"].plan.empty


# ----- 5. delete-guard alert titles -----------------------------------------


def many(n: int) -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for i in range(n):
        fake.put_event(
            GCAL, google_event(f"e{i}", f"E{i}", "2026-10-25T09:00:00+08:00", "2026-10-25T09:05:00+08:00")
        )
    return fake


async def test_blocked_rule_lists_titles_with_one_fetch(tmp_path: Path) -> None:
    fake = many(4)
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    for i in range(4):
        fake.cancel_event(GCAL, f"e{i}")
    h.target.calls.clear()
    res = (await h.engine.cycle()).rules["family"]
    assert res.status == "blocked"
    assert sorted(res.blocked_titles) == ["E0", "E1", "E2", "E3"] and res.blocked_titles_complete
    assert [op for op, _ in h.target.calls] == ["changes", "fetch"]


async def test_unreadable_planned_delete_marks_titles_incomplete(tmp_path: Path) -> None:
    fake = many(4)
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    garbled = sorted(h.target.managed(h.target.calendars["Family"]))[0]
    h.target.put(garbled, b"not icalendar")
    for i in range(4):
        fake.cancel_event(GCAL, f"e{i}")
    res = (await h.engine.cycle()).rules["family"]
    assert res.status == "blocked" and not res.blocked_titles_complete
    assert len(res.blocked_titles) == 4 and res.blocked_titles.count("(unreadable)") == 1


async def test_title_lookup_failure_keeps_the_rule_blocked(tmp_path: Path) -> None:
    fake = many(4)
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    for i in range(4):
        fake.cancel_event(GCAL, f"e{i}")
    h.target.fail_fetch = TransientError("multiget timed out")
    res = (await h.engine.cycle()).rules["family"]
    assert res.status == "blocked" and res.error_kind == "guard" and res.blocked_titles == []
    assert not res.blocked_titles_complete
    assert len(h.target.objects) == 4


# ----- 6. minor ---------------------------------------------------------------


class ForgetfulSource:
    """Wraps a source; while ``forgetful`` its pulls silently omit every calendar."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.forgetful = False

    async def list_calendars(self) -> list[CalendarInfo]:
        return await self._inner.list_calendars()

    async def pull(self, calendar_refs: Sequence[str], state: Mapping[str, Any]) -> SourcePull:
        pull = await self._inner.pull(calendar_refs, state)
        return SourcePull({}, dict(state)) if self.forgetful else pull

    def to_series(self, raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Series]:
        return self._inner.to_series(raw_events, meta)

    def series_ids(self, raw_events: Iterable[RawEvent]) -> set[str]:
        return self._inner.series_ids(raw_events)


async def test_source_omitting_a_calendar_fails_the_rule_instead_of_reusing_data(tmp_path: Path) -> None:
    fake = simple()
    source = ForgetfulSource(
        GoogleSource(
            make_client(RequestLog(), transport=fake.transport()), StaticToken("t"), base_url=BASE_URL
        )
    )
    h = build_fake(tmp_path, fake, config_for([rule()]), sources={"me": source})
    assert (await h.engine.cycle()).rules["family"].created == 2
    fake.cancel_event(GCAL, "checkup")
    fake.cancel_event(GCAL, "pills")
    source.forgetful = True  # neither in calendars nor in errors: a contract violation
    res = (await h.engine.cycle()).rules["family"]
    assert res.status == "failed" and "contract" in (res.error or "")
    assert len(h.target.managed(h.target.calendars["Family"])) == 2


async def test_unfetchable_changed_href_leaves_cache_and_is_repaired(tmp_path: Path) -> None:
    h = build_fake(tmp_path, simple(), config_for([rule()]))
    await h.engine.cycle()
    cal = h.target.calendars["Family"]
    href = sorted(h.target.managed(cal))[0]
    h.target.put(href, b"edited elsewhere")
    h.target.hide_once = {href}  # e.g. a multiget that skips the resource this time
    res = (await h.engine.cycle()).rules["family"]
    assert res.status == "ok" and res.created == 1  # repaired via create -> 412 -> update
    assert h.target.objects[href][1] != b"edited elsewhere"
    assert (await h.engine.cycle()).rules["family"].plan.empty


async def test_partial_failure_keeps_first_error_and_counts_the_rest(tmp_path: Path) -> None:
    h = build_fake(tmp_path, many(4), config_for([rule()]))
    seen: list[str] = []

    def flaky(op: str, href: str) -> None:
        seen.append(href)
        if len(seen) > 1:
            raise TransientError("PUT timed out")

    h.target.before_write = flaky
    res = (await h.engine.cycle()).rules["family"]
    assert (res.status, res.created, res.failed) == ("partial", 1, 3)
    assert res.error == "TransientError: PUT timed out (+2 more)" and res.error_kind == "other"
    assert [r.status for r in h.store.latest_runs()] == ["partial"]


async def test_dry_run_against_missing_target_calendar(tmp_path: Path) -> None:
    h = build_fake(tmp_path, simple(), config_for([rule()]))
    res = (await h.engine.cycle(dry_run=True)).rules["family"]
    assert res.status == "failed" and "a real run creates it" in (res.error or "")
    assert h.target.calendars == {} and h.target.objects == {}
    assert h.store.latest_runs() == []


# ----- fix 2: unreadable hrefs in sync-collection mode ----------------------


def copy_href(h: FakeHarness, source_id: str, rule_name: str = "family") -> str:
    oid = object_id(rule_name, GCAL, source_id)
    return h.target.href_for(h.target.calendars["Family"], resource_name(rule_name, oid))


async def test_sync_mode_unreadable_href_is_repaired_and_not_orphaned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=FakeTarget(mode="sync"))
    assert (await h.engine.cycle()).rules["family"].created == 2
    href = copy_href(h, "checkup")
    h.target.put(href, b"edited elsewhere")
    h.target.hide_once = {href}
    with caplog.at_level(logging.WARNING):
        res = (await h.engine.cycle()).rules["family"]
    assert res.status == "ok"
    warnings = [r.getMessage() for r in caplog.records if "could not be read" in r.getMessage()]
    assert len(warnings) == 1 and "calsync-" not in warnings[0]
    for _ in range(2):
        assert (await h.engine.cycle()).rules["family"].plan.empty
    managed = h.target.managed(h.target.calendars["Family"])
    assert len(managed) == 2 and managed[href] != b"edited elsewhere"


async def test_sync_mode_unreadable_href_of_deleted_series_is_still_deleted(tmp_path: Path) -> None:
    fake = simple()
    h = build_fake(tmp_path, fake, config_for([rule()]), target=FakeTarget(mode="sync"))
    await h.engine.cycle()
    href = copy_href(h, "checkup")
    h.target.put(href, b"edited elsewhere")
    h.target.hide_once = {href}
    fake.cancel_event(GCAL, "checkup")
    deleted = 0
    for _ in range(3):
        res = (await h.engine.cycle()).rules["family"]
        assert res.status == "ok"
        deleted += res.deleted
    assert deleted == 1
    assert href not in h.target.objects
    assert len(h.target.managed(h.target.calendars["Family"])) == 1


# ----- fix 3: degraded series (an instance failed to parse) -----------------


def recurring(fake: FakeGoogle, *, broken: bool, title: str = "Reminders") -> None:
    fake.put_event(
        GCAL,
        google_event(
            "pills",
            title,
            "2026-10-24T08:00:00+08:00",
            "2026-10-24T08:05:00+08:00",
            recurrence=["RRULE:FREQ=DAILY;COUNT=30"],
        ),
    )
    late = google_event(
        "pills_20261026T000000Z",
        "Reminders (late)",
        "2026-10-26T10:00:00+08:00",
        "2026-10-26T10:05:00+08:00",
        recurringEventId="pills",
        originalStartTime={"dateTime": "2026-10-26T08:00:00+08:00", "timeZone": "Asia/Shanghai"},
    )
    if broken:
        del late["end"]
    fake.put_event(GCAL, late)


def calendar_with_pills(*, broken: bool) -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    recurring(fake, broken=broken)
    return fake


async def test_degraded_series_keeps_existing_copy(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    fake = calendar_with_pills(broken=False)
    h = build_fake(tmp_path, fake, config_for([rule()]))
    assert (await h.engine.cycle()).rules["family"].created == 1
    before = dict(h.target.objects)
    recurring(fake, broken=True, title="Reminders (new reminder)")  # a real change, but one instance is bad
    with caplog.at_level(logging.WARNING):
        res = (await h.engine.cycle()).rules["family"]
        again = (await h.engine.cycle()).rules["family"]
    assert res.status == "ok" and res.protected == 1 and res.plan is not None and res.plan.empty
    assert again.protected == 1 and again.plan is not None and again.plan.empty
    assert res.degraded == 1 and again.degraded == 1
    assert h.target.objects == before
    warnings = [r.getMessage() for r in caplog.records if "protecting" in r.getMessage()]
    assert len(warnings) == 1 and "pills" in warnings[0] and "Reminders" not in warnings[0]


async def test_degraded_series_without_copy_is_created_then_completed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = calendar_with_pills(broken=True)
    h = build_fake(tmp_path, fake, config_for([rule()]))
    with caplog.at_level(logging.WARNING):
        first = (await h.engine.cycle()).rules["family"]
    assert (first.status, first.created, first.protected, first.degraded) == ("ok", 1, 0, 1)
    assert len([r for r in caplog.records if "pills" in r.getMessage()]) == 1  # engine only, once
    (body,) = h.target.managed(h.target.calendars["Family"]).values()
    assert b"Reminders (late)" not in body
    recurring(fake, broken=False)
    fixed = (await h.engine.cycle()).rules["family"]
    assert (fixed.status, fixed.updated, fixed.protected, fixed.degraded) == ("ok", 1, 0, 0)
    (body,) = h.target.managed(h.target.calendars["Family"]).values()
    assert b"Reminders (late)" in body


async def test_degraded_copy_is_excluded_from_the_delete_guard(tmp_path: Path) -> None:
    fake = many(3)
    recurring(fake, broken=False)
    h = build_fake(tmp_path, fake, config_for([rule(delete_guard={"max_ratio": 0.8, "max_count": 10})]))
    assert (await h.engine.cycle()).rules["family"].created == 4
    recurring(fake, broken=True)
    for i in range(3):
        fake.cancel_event(GCAL, f"e{i}")
    # 3 of 4 managed would pass max_ratio=0.8; 3 of the 3 deletable (non-protected) ones must not.
    res = (await h.engine.cycle()).rules["family"]
    assert (res.status, res.deleted, res.protected) == ("blocked", 0, 1)
    assert "3 of 3" in (res.error or "")
    assert len(h.target.managed(h.target.calendars["Family"])) == 4


# ----- fix 4: degraded series vs. the create fallback, counts, warnings ------


async def test_create_fallback_never_overwrites_a_copy_with_a_degraded_series(tmp_path: Path) -> None:
    fake = calendar_with_pills(broken=False)
    h = build_fake(tmp_path, fake, config_for([rule()]), target=FakeTarget(mode="sync"))
    await h.engine.cycle()
    (href,) = h.target.managed(h.target.calendars["Family"])
    good = h.target.objects[href][1]
    assert b"Reminders (late)" in good
    recurring(fake, broken=True)  # one instance now malformed: degraded
    h.target.put(href, good)  # touched on the target (new etag), same body
    h.target.hide_once = {href}  # multiget omits it once: not in actual, so the plan creates
    res = (await h.engine.cycle()).rules["family"]
    assert (res.status, res.created, res.updated, res.protected, res.degraded) == ("ok", 0, 0, 1, 1)
    assert h.target.objects[href][1] == good
    again = (await h.engine.cycle()).rules["family"]
    assert again.protected == 1 and again.plan is not None and again.plan.empty
    assert h.target.objects[href][1] == good
    recurring(fake, broken=False, title="Reminders (new reminder)")
    fixed = (await h.engine.cycle()).rules["family"]
    assert (fixed.status, fixed.updated, fixed.protected, fixed.degraded) == ("ok", 1, 0, 0)
    assert b"Reminders (new reminder)" in h.target.objects[href][1]


async def test_unparsed_series_without_copy_is_degraded_not_protected(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = simple()
    broken = google_event("walk", "Walk", "2026-10-26T18:00:00+08:00", "2026-10-26T18:30:00+08:00")
    del broken["start"]
    fake.put_event(GCAL, broken)
    h = build_fake(tmp_path, fake, config_for([rule()]))
    with caplog.at_level(logging.WARNING):
        res = (await h.engine.cycle()).rules["family"]
    assert (res.status, res.created, res.protected, res.degraded) == ("ok", 2, 0, 1)
    warnings = [r.getMessage() for r in caplog.records if "walk" in r.getMessage()]
    assert len(warnings) == 1 and "Walk" not in warnings[0]


async def test_degraded_warning_rearms_after_the_series_parses_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = calendar_with_pills(broken=False)
    h = build_fake(tmp_path, fake, config_for([rule()]))
    with caplog.at_level(logging.WARNING):
        for broken in (True, True, False, True):
            recurring(fake, broken=broken)
            await h.engine.cycle()
    assert len([r for r in caplog.records if "pills" in r.getMessage()]) == 2


# ----- 403 is not an authentication failure --------------------------------


async def test_single_forbidden_write_is_not_an_auth_failure(tmp_path: Path) -> None:
    h = build_fake(tmp_path, simple(), config_for([rule()]))
    seen: list[str] = []

    def refuse_first(op: str, href: str) -> None:
        seen.append(href)
        if len(seen) == 1:
            raise ForbiddenError("PUT x.ics: HTTP 403 {urn:ietf:params:xml:ns:caldav}valid-calendar-data")

    h.target.before_write = refuse_first
    res = (await h.engine.cycle()).rules["family"]
    assert (res.status, res.created, res.failed) == ("partial", 1, 1)
    assert res.error_kind == "other" and "ForbiddenError" in (res.error or "")


async def test_forbidden_detection_is_not_an_auth_failure(tmp_path: Path) -> None:
    h = build_fake(tmp_path, simple(), config_for([rule()]))
    h.target.fail_changes["Family"] = ForbiddenError("REPORT /cal/: HTTP 403 {DAV:}need-privileges")
    await h.engine.cycle()  # the first cycle discovers the calendar and creates the copies
    h.target.fail_changes["Family"] = ForbiddenError("REPORT /cal/: HTTP 403 {DAV:}need-privileges")
    res = (await h.engine.cycle()).rules["family"]
    assert res.status == "failed" and res.error_kind == "other"


# ----- edits made on the target are reverted --------------------------------


async def test_manual_target_edit_is_repaired(tmp_path: Path) -> None:
    """Someone moves a reminder on the phone; the client keeps X-CALSYNC-HASH. The next cycle writes
    the source version back."""
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    await h.engine.cycle()
    cal = target.calendars["Family"]
    href, data = next(iter(target.managed(cal).items()))
    edited = data.replace(b"SUMMARY:Reminders", b"SUMMARY:Reminders MOVED").replace(
        b"SUMMARY:Checkup", b"SUMMARY:Checkup MOVED"
    )
    assert edited != data and b"X-CALSYNC-HASH" in edited
    target.put(href, edited)
    res = (await h.engine.cycle()).rules["family"]
    assert res.updated == 1
    assert b"MOVED" not in target.objects[href][1]
    assert res.reverted == 1
    assert (await h.engine.cycle()).rules["family"].updated == 0  # converged


MOVE_FROM, MOVE_TO = (
    b"DTSTART;TZID=Asia/Shanghai:20261025T080000",
    b"DTSTART;TZID=Asia/Shanghai:20261025T083000",
)


def move_pills_on_target(h: FakeHarness, rule_name: str = "family", source_id: str = "pills") -> str:
    """Move the start of the "pills" copy by 30 minutes, keeping our marker (like a phone)."""
    href = copy_href(h, source_id, rule_name)
    data = h.target.objects[href][1]
    edited = data.replace(MOVE_FROM, MOVE_TO)
    assert edited != data and b"X-CALSYNC-HASH" in edited
    h.target.put(href, edited)
    return href


async def test_copy_deleted_on_target_is_recreated(tmp_path: Path) -> None:
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    cal = target.calendars["Family"]
    href = next(iter(target.managed(cal)))
    target.remove(href)
    res = (await h.engine.cycle()).rules["family"]
    assert res.created == 1 and res.deleted == 0
    assert href in target.managed(cal)


def fetched_since(target: FakeTarget, mark: int) -> list[str]:
    """Hrefs whose bodies were requested from the target since ``mark`` (an index into calls)."""
    return [href for op, hrefs in target.calls[mark:] if op == "fetch" for href in hrefs.split(",") if href]


async def test_alarm_dismissed_on_phone_is_not_rewritten(tmp_path: Path) -> None:
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    cal = target.calendars["Family"]
    href, data = next(iter(target.managed(cal).items()))
    edited = data.replace(b"BEGIN:VALARM\r\n", b"BEGIN:VALARM\r\nACKNOWLEDGED:20261025T000500Z\r\n")
    assert edited != data
    target.put(href, edited)
    mark = len(target.calls)
    res = (await h.engine.cycle()).rules["family"]
    assert fetched_since(target, mark) == [href]  # the body was read and compared, not skipped
    assert res.updated == 0 and res.reverted == 0
    assert h.store.load_target_cache("dav", cal.href).objects[href].verify_digest is None
    assert (await h.engine.cycle()).rules["family"].updated == 0


def forget_state(h: FakeHarness, tmp_path: Path) -> None:
    """Lose the local state like a wiped state directory (WAL side files included)."""
    h.store.close()
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"state.db{suffix}").unlink(missing_ok=True)


async def test_lost_state_does_not_rewrite_unchanged_copies(tmp_path: Path) -> None:
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    forget_state(h, tmp_path)
    h2 = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    mark = len(target.calls)
    res = (await h2.engine.cycle()).rules["family"]
    managed = target.managed(target.calendars["Family"])
    assert len(managed) == 2 and sorted(fetched_since(target, mark)) == sorted(managed)
    assert (res.created, res.updated, res.reverted) == (0, 0, 0)


async def test_schema_change_cold_start_does_not_rewrite_unchanged_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new store schema rebuilds the state (a cold start): every body is read, none is written."""
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    h.store.close()
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", store_mod.SCHEMA_VERSION + 1)
    h2 = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    mark = len(target.calls)
    res = (await h2.engine.cycle()).rules["family"]
    managed = target.managed(target.calendars["Family"])
    assert len(managed) == 2 and sorted(fetched_since(target, mark)) == sorted(managed)
    assert (res.created, res.updated, res.reverted) == (0, 0, 0)
    assert not [op for op, _ in target.calls[mark:] if op in {"create", "update", "delete"}]


async def test_lost_state_reverts_a_copy_edited_meanwhile(tmp_path: Path) -> None:
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    href = move_pills_on_target(h)
    forget_state(h, tmp_path)
    h2 = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    res = (await h2.engine.cycle()).rules["family"]
    assert (res.created, res.updated, res.reverted) == (0, 1, 1)
    assert MOVE_FROM in target.objects[href][1]


@pytest.mark.parametrize("unreadable", ["fetched", "expected"])
async def test_unreadable_digest_is_not_treated_as_an_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreadable: str
) -> None:
    """If calsync cannot digest one side (the body read from the target, or the body it would
    write) it never starts rewriting the copy (no loops on parser limits)."""
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    href = move_pills_on_target(h)
    real = calsync.engine.content_digest
    # The edited body read from the target carries MOVE_TO, the rendered source version MOVE_FROM.
    blind = MOVE_TO if unreadable == "fetched" else MOVE_FROM
    calls: list[bool] = []

    def digest(data: bytes) -> str | None:
        calls.append(blind in data)
        return None if blind in data else real(data)

    monkeypatch.setattr("calsync.engine.content_digest", digest)
    mark = len(target.calls)
    res = (await h.engine.cycle()).rules["family"]
    assert fetched_since(target, mark) == [href]
    assert True in calls  # the unreadable side really was digested
    if unreadable == "expected":
        assert False in calls  # and the readable side too: only one side is unreadable
    assert (res.updated, res.reverted) == (0, 0)
    assert MOVE_TO in target.objects[href][1]
    cached = h.store.load_target_cache("dav", target.calendars["Family"].href).objects[href]
    assert cached.verify_digest is None and not cached.reverting


async def test_edit_on_a_protected_series_is_not_reverted(tmp_path: Path) -> None:
    fake = simple()
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, fake, config_for([rule()]), target=target)
    await h.engine.cycle()
    href = move_pills_on_target(h)
    broken = google_event("pills", "Reminders", "2026-10-25T08:00:00+08:00", "2026-10-25T08:05:00+08:00")
    del broken["start"]
    fake.put_event(GCAL, broken)
    res = (await h.engine.cycle()).rules["family"]
    assert res.protected == 1 and (res.updated, res.reverted, res.deleted) == (0, 0, 0)
    assert MOVE_TO in target.objects[href][1]
    again = (await h.engine.cycle()).rules["family"]  # still protected: still untouched
    assert again.protected == 1 and (again.updated, again.reverted) == (0, 0)
    assert MOVE_TO in target.objects[href][1]
    # The series parses again, unchanged: the edit read while it was protected is now reverted.
    fake.put_event(
        GCAL, google_event("pills", "Reminders", "2026-10-25T08:00:00+08:00", "2026-10-25T08:05:00+08:00")
    )
    mark = len(target.calls)
    fixed = (await h.engine.cycle()).rules["family"]
    assert fetched_since(target, mark) == []  # decided from the saved digest, no re-read
    assert (fixed.protected, fixed.updated, fixed.reverted) == (0, 1, 1)
    assert MOVE_FROM in target.objects[href][1]


async def test_source_change_wins_over_a_target_edit(tmp_path: Path) -> None:
    fake = simple()
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, fake, config_for([rule()]), target=target)
    await h.engine.cycle()
    href = move_pills_on_target(h)
    fake.put_event(
        GCAL, google_event("pills", "Reminders", "2026-10-25T09:00:00+08:00", "2026-10-25T09:05:00+08:00")
    )
    res = (await h.engine.cycle()).rules["family"]
    assert (res.updated, res.reverted) == (1, 0)  # an ordinary update, not a revert
    assert b"20261025T090000" in target.objects[href][1]


async def test_dry_run_plans_the_revert_and_the_next_run_writes_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    href = move_pills_on_target(h)
    with caplog.at_level(logging.INFO, logger="calsync.engine"):
        dry = (await h.engine.cycle(dry_run=True)).rules["family"]
    assert dry.plan is not None and len(dry.plan.update) == 1
    assert (dry.updated, dry.reverted) == (0, 0)
    assert MOVE_TO in target.objects[href][1]
    [line] = [r.getMessage() for r in caplog.records if "edited on the target" in r.getMessage()]
    assert "writing" not in line  # a dry run writes nothing, and the log must not claim it does
    # Detection consumed the change during the dry run; the edit must still be reverted, also by
    # a fresh process reading the saved state.
    h.store.close()
    h2 = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    res = (await h2.engine.cycle()).rules["family"]
    assert (res.updated, res.reverted) == (1, 1)
    assert MOVE_FROM in target.objects[href][1]
    assert (await h2.engine.cycle()).rules["family"].updated == 0


async def test_failed_revert_counts_as_failed_and_is_retried(tmp_path: Path) -> None:
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    href = move_pills_on_target(h)

    def refuse(op: str, path: str) -> None:
        raise TransientError("PUT timed out")

    h.target.before_write = refuse
    res = (await h.engine.cycle()).rules["family"]
    assert (res.status, res.updated, res.reverted, res.failed) == ("failed", 0, 0, 1)
    assert MOVE_TO in target.objects[href][1]
    h.target.before_write = None
    again = (await h.engine.cycle()).rules["family"]
    assert (again.status, again.updated, again.reverted) == ("ok", 1, 1)
    assert MOVE_FROM in target.objects[href][1]
    assert (await h.engine.cycle()).rules["family"].updated == 0


# ----- an edit read by detection is never lost before its rule classifies it ---


def fail_desired(h: FakeHarness, rule_name: str, times: int = 1) -> None:
    """Make the rule's desired-state step raise ``times`` times (after detection already ran)."""
    real = h.engine._desired
    left = [times]

    def flaky(r: Any) -> Any:
        if r.name == rule_name and left[0] > 0:
            left[0] -= 1
            raise TransientError("source mirror unavailable")
        return real(r)

    h.engine._desired = flaky  # type: ignore[method-assign]


async def test_edit_read_by_a_subset_cycle_is_reverted_by_its_own_rule(tmp_path: Path) -> None:
    """Two rules share one target calendar; a cycle that runs only rule a still reads the copy
    rule b owns. Rule b must revert it on its next run."""
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule("a"), rule("b")]), target=target)
    await h.engine.cycle()
    href = move_pills_on_target(h, "b")
    mark = len(target.calls)
    only_a = (await h.engine.cycle(rules=["a"])).rules
    assert set(only_a) == {"a"} and only_a["a"].updated == 0
    assert fetched_since(target, mark) == [href]  # rule a's detection consumed the change
    assert MOVE_TO in target.objects[href][1]
    res = (await h.engine.cycle()).rules
    assert (res["b"].updated, res["b"].reverted) == (1, 1) and res["a"].updated == 0
    assert MOVE_FROM in target.objects[href][1]
    assert (await h.engine.cycle()).rules["b"].updated == 0  # converged


async def test_edit_survives_a_failure_of_the_owning_rule(tmp_path: Path) -> None:
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    href = move_pills_on_target(h)
    fail_desired(h, "family")
    failed = (await h.engine.cycle()).rules["family"]
    assert failed.status == "failed" and "TransientError" in (failed.error or "")
    assert MOVE_TO in target.objects[href][1]
    mark = len(target.calls)
    res = (await h.engine.cycle()).rules["family"]
    assert fetched_since(target, mark) == []  # detection already consumed the change
    assert (res.status, res.updated, res.reverted) == ("ok", 1, 1)
    assert MOVE_FROM in target.objects[href][1]


class FlakySource:
    """Wraps a source; calendar refs in ``failing`` fail on their own (like a 5xx for one calendar)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.failing: set[str] = set()

    async def list_calendars(self) -> list[CalendarInfo]:
        return await self._inner.list_calendars()

    async def pull(self, calendar_refs: Sequence[str], state: Mapping[str, Any]) -> SourcePull:
        good = [ref for ref in calendar_refs if ref not in self.failing]
        pull = await self._inner.pull(good, state) if good else SourcePull({}, dict(state))
        bad = {ref: TransientError("events.list: HTTP 503") for ref in calendar_refs if ref in self.failing}
        return SourcePull(pull.calendars, pull.state, {**pull.errors, **bad})

    def to_series(self, raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Series]:
        return self._inner.to_series(raw_events, meta)

    def series_ids(self, raw_events: Iterable[RawEvent]) -> set[str]:
        return self._inner.series_ids(raw_events)


async def test_edit_survives_a_source_failure_of_the_owning_rule(tmp_path: Path) -> None:
    """Rule b's source fails while rule a, on the same target calendar, runs and reads b's copy."""
    fake = simple()
    source = FlakySource(
        GoogleSource(
            make_client(RequestLog(), transport=fake.transport()), StaticToken("t"), base_url=BASE_URL
        )
    )
    rules = [rule("a", source="Family"), rule("b", source=GCAL)]
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, fake, config_for(rules), target=target, sources={"me": source})
    first = await h.engine.cycle()
    assert first.ok and first.rules["b"].created == 2
    href = move_pills_on_target(h, "b")
    source.failing = {GCAL}
    mark = len(target.calls)
    broken = (await h.engine.cycle()).rules
    assert broken["b"].status == "failed" and broken["a"].status == "ok"
    assert fetched_since(target, mark) == [href]  # rule a's detection consumed the change
    source.failing = set()
    res = (await h.engine.cycle()).rules["b"]
    assert (res.status, res.updated, res.reverted) == ("ok", 1, 1)
    assert MOVE_FROM in target.objects[href][1]


async def test_edit_read_before_a_crash_is_reverted_by_a_fresh_process(tmp_path: Path) -> None:
    """The process stops after detection saved the cache but before the rule ran (simulated by a
    rule that fails right after detection). A new process on the same state file reverts it."""
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    href = move_pills_on_target(h)
    fail_desired(h, "family")
    assert (await h.engine.cycle()).rules["family"].status == "failed"
    h.store.close()
    h2 = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    mark = len(target.calls)
    res = (await h2.engine.cycle()).rules["family"]
    assert fetched_since(target, mark) == []  # nothing re-read: the saved state carries the edit
    assert (res.status, res.updated, res.reverted) == ("ok", 1, 1)
    assert MOVE_FROM in target.objects[href][1]
    assert (await h2.engine.cycle()).rules["family"].updated == 0
