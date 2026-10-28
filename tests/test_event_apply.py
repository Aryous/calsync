"""events.apply: many writes to one calendar in batches, idempotent by key."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.harness import GCAL
from tests.test_event_ops import Live

pytestmark = pytest.mark.anyio


@pytest.fixture
async def live(tmp_path: Path) -> AsyncIterator[Live]:
    d = await Live(tmp_path).start()
    yield d
    await d.stop()


def reminder(n: int, day: int = 1, hm: str = "08:00", title: str | None = None) -> dict[str, Any]:
    return {
        "key": f"plan-a/{n:03d}",
        "title": title or f"Reminder ({n}/40)",
        "start": f"2026-11-{day:02d}T{hm}",
        "end": f"2026-11-{day:02d}T{hm[:3]}05",
        "alarms": [0],
    }


def plan(n: int) -> list[dict[str, Any]]:
    return [reminder(i, 1 + i // 4, ["08:00", "12:00", "16:00", "20:00"][i % 4]) for i in range(n)]


def keyed(live: Live) -> dict[str, dict[str, Any]]:
    out = {}
    for e in live.world.fake.events(GCAL):
        key = ((e.get("extendedProperties") or {}).get("private") or {}).get("calsync_key")
        if key and e.get("status") != "cancelled":
            out[key] = e
    return out


async def apply(live: Live, events: list[dict[str, Any]], **extra: Any) -> Any:
    return await live.op("events.apply", {"account": "me", "calendar": "Family", "events": events, **extra})


async def test_a_batch_creates_in_requests_of_fifty_and_a_rerun_does_nothing(live: Live) -> None:
    result = await apply(live, plan(120), wait=True)
    assert result.data["counts"]["create"] == 120 and result.data["counts"]["failed"] == 0
    assert live.world.fake.batches == 3
    assert len(keyed(live)) == 120
    assert result.data["cycle"]["ran"] is True
    [row] = live.writes()
    assert row.op == "apply" and row.detail["counts"]["create"] == 120 and row.event_id is None
    # The same list again, before or after a cycle: nothing to write.
    again = await apply(live, plan(120))
    assert again.data["counts"] == {
        "create": 0,
        "update": 0,
        "delete": 0,
        "unchanged": 120,
        "absent": 0,
        "failed": 0,
    }
    assert not again.changed and live.world.fake.batches == 3


async def test_a_rerun_before_the_cycle_sees_its_own_writes(live: Live) -> None:
    await apply(live, plan(5))  # no wait: the mirror does not know them yet
    again = await apply(live, plan(5))
    assert again.data["counts"]["unchanged"] == 5 and len(keyed(live)) == 5


async def test_updates_deletes_and_prune(live: Live) -> None:
    await apply(live, plan(8), wait=True)
    events = plan(6)
    events[0] = reminder(0, title="Reminder (1/40, evening)")
    events[1] = {"key": "plan-a/001", "delete": True}
    result = await apply(live, events, prune_prefix="plan-a/")
    counts = result.data["counts"]
    assert (counts["update"], counts["delete"], counts["unchanged"]) == (1, 3, 4)  # 001 + 006, 007 pruned
    assert sorted(keyed(live)) == ["plan-a/000", "plan-a/002", "plan-a/003", "plan-a/004", "plan-a/005"]
    assert keyed(live)["plan-a/000"]["summary"] == "Reminder (1/40, evening)"
    updated = next(i for i in result.data["items"] if i["action"] == "update")
    assert updated["changes"] == [
        {"field": "title", "before": "Reminder (0/40)", "after": "Reminder (1/40, evening)"}
    ]


async def test_items_by_id_and_a_missing_one_fails_alone(live: Live) -> None:
    lunch = live.event_id("Lunch")
    result = await apply(live, [{"id": lunch, "delete": True}, {"id": "bm9wZQ", "delete": True}, reminder(1)])
    by_index = {i["index"]: i for i in result.data["items"]}
    assert by_index[0]["status"] == "ok" and by_index[1] == {
        "index": 1,
        "id": "bm9wZQ",
        "action": "delete",
        "status": "failed",
        "reason": "no such event",
    }
    assert by_index[2]["action"] == "create" and by_index[2]["status"] == "ok"
    assert live.world.fake.event(GCAL, "lunch")["status"] == "cancelled"
    assert live.writes()[0].status == "partial"


async def test_a_failing_call_is_reported_and_the_rest_is_written(live: Live) -> None:
    await apply(live, plan(3), wait=True)
    target = keyed(live)["plan-a/001"]["id"]
    live.world.fake.fail[target] = 500
    events = [reminder(i, title=f"New {i}") for i in range(3)]
    events = [{**e, "start": d["start"], "end": d["end"]} for e, d in zip(events, plan(3), strict=True)]
    result = await apply(live, events)
    statuses = [(i["action"], i["status"]) for i in result.data["items"]]
    assert statuses == [("update", "ok"), ("update", "failed"), ("update", "ok")]
    again = await apply(live, events)
    assert again.data["counts"]["update"] == 1 and again.data["counts"]["unchanged"] == 2


async def test_a_bad_item_refuses_the_whole_call(live: Live) -> None:
    with pytest.raises(Exception, match=r"events\[1\]"):
        await apply(live, [reminder(0), {"title": "no key"}, reminder(2)])
    with pytest.raises(Exception, match="appears twice"):
        await apply(live, [reminder(0), reminder(0)])
    with pytest.raises(Exception, match="end: must be after the start"):
        await apply(live, [{**reminder(0), "end": "2026-11-01T07:00"}])
    assert live.world.fake.writes == []


async def test_a_dry_run_plans_without_writing(live: Live) -> None:
    result = await apply(live, plan(4))
    dry = await live.op(
        "events.apply",
        {"account": "me", "calendar": "Family", "events": [*plan(4), reminder(9)], "prune_prefix": "plan-a/"},
        dry_run=True,
    )
    assert result.changed and dry.data["counts"]["create"] == 1 and dry.data["would_change"]
    assert len(live.world.fake.writes) == 4  # only the first call's


async def test_without_write_access(live: Live) -> None:
    live.world.fake.write_scope = False
    result = await apply(live, plan(2))
    assert result.needs_human is not None and result.needs_human["command"] == "calsync auth me --write"
    assert keyed(live) == {}


async def test_repeating_items(live: Live) -> None:
    item = {**reminder(0), "repeat": ["RRULE:FREQ=DAILY;COUNT=14"]}
    await apply(live, [item], wait=True)
    assert keyed(live)["plan-a/000"]["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=14"]
    again = await apply(live, [item])
    assert again.data["counts"]["unchanged"] == 1


async def test_invitations_are_refused_per_item(live: Live) -> None:
    from tests.factories import google_event

    fake = live.world.fake
    fake.put_event(
        GCAL,
        google_event(
            "inv",
            "Invite",
            "2026-10-25T15:00:00+08:00",
            "2026-10-25T16:00:00+08:00",
            organizer={"email": "x@example.com"},
        ),
    )
    await live.cycle()
    result = await apply(live, [{"id": live.event_id("Invite"), "delete": True}])
    assert (
        result.data["items"][0]["reason"] == "invitation" and fake.event(GCAL, "inv")["status"] == "confirmed"
    )


async def test_an_occurrence_id_needs_scope_series_and_no_new_times(live: Live) -> None:
    drops = live.event_id("Reminder")
    bare = await apply(live, [{"id": drops, "title": "x"}])
    assert bare.data["items"][0]["status"] == "failed" and "scope" in bare.data["items"][0]["reason"]
    timed = await apply(
        live, [{"id": drops, "scope": "series", "start": "2026-10-25T10:00", "end": "2026-10-25T10:05"}]
    )
    assert "event edit --series" in timed.data["items"][0]["reason"]
    assert live.world.fake.event(GCAL, "drops")["start"]["dateTime"] == "2026-10-24T08:00:00+08:00"
    renamed = await apply(live, [{"id": drops, "scope": "series", "title": "Eye drops"}])
    assert (
        renamed.data["counts"]["update"] == 1
        and live.world.fake.event(GCAL, "drops")["summary"] == "Eye drops"
    )


async def test_expect_updated_per_item(live: Live) -> None:
    lunch = live.world.fake.event(GCAL, "lunch")
    live.world.fake.touch(GCAL, "lunch", location="Elsewhere")
    result = await apply(
        live, [{"id": live.event_id("Lunch"), "delete": True, "expect_updated": lunch["updated"]}]
    )
    assert (
        result.data["items"][0]["reason"] == "conflict"
        and live.world.fake.event(GCAL, "lunch")["status"] == "confirmed"
    )


async def test_a_moved_occurrence_of_a_keyed_series_does_not_stand_for_it(live: Live) -> None:
    item = {**reminder(0), "repeat": ["RRULE:FREQ=DAILY;COUNT=5"]}
    await apply(live, [item], wait=True)
    master = keyed(live)["plan-a/000"]
    # A person moved one occurrence in Google: the exception carries the series' properties.
    exception = {
        **{k: v for k, v in master.items() if k not in ("recurrence", "etag", "updated")},
        "id": f"{master['id']}_20261102T000000Z",
        "recurringEventId": master["id"],
        "originalStartTime": {"dateTime": "2026-11-02T08:00:00+08:00"},
        "start": {"dateTime": "2026-11-02T09:00:00+08:00", "timeZone": "Asia/Shanghai"},
        "end": {"dateTime": "2026-11-02T09:05:00+08:00", "timeZone": "Asia/Shanghai"},
    }
    live.world.fake.put_event(GCAL, exception)
    await live.cycle()
    renamed = await apply(live, [{**item, "title": "Reminder (renamed)"}])
    assert [i["source_id"] for i in renamed.data["items"]] == [master["id"]]
    assert live.world.fake.event(GCAL, master["id"])["summary"] == "Reminder (renamed)"
