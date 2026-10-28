"""Pre-approved copy deletes: a person deleted originals in the web UI, so their copies' deletes
pass the mass-delete guard; any other delete in the same cycle is judged as always."""

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from calsync.daemon import COVER_TTL
from calsync.engine import Approval
from calsync.naming import object_id
from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, build_fake
from tests.test_approve import COUNT, Running, family_config, seed
from tests.test_daemon import until

pytestmark = pytest.mark.anyio


def copies(ids: list[str]) -> frozenset[str]:
    return frozenset(object_id("family", GCAL, i) for i in ids)


async def synced(tmp_path: Path) -> tuple[object, FakeGoogle, list[str]]:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    ids = seed(fake, 30)
    h = build_fake(tmp_path, fake, family_config(tmp_path))
    await h.engine.cycle()
    return h, fake, ids


async def test_covered_deletes_pass_the_guard_and_are_recorded(tmp_path: Path) -> None:
    h, fake, ids = await synced(tmp_path)
    for event_id in ids[:COUNT]:
        fake.cancel_event(GCAL, event_id)
    result = (await h.engine.cycle(approvals={"family": Approval(None, "ui", copies(ids[:COUNT]))})).rules[
        "family"
    ]  # type: ignore[attr-defined]
    assert result.status == "ok" and len(result.plan.delete) == COUNT
    assert result.released_covers == copies(ids[:COUNT])
    assert result.approved_by is None  # no push: the person who deleted them knows
    approved = [c for c in result.changes if c.action == "approved"]
    assert [c.title for c in approved] == [f"{COUNT} deletes approved (ui)"]


async def test_other_deletes_are_judged_alone(tmp_path: Path) -> None:
    h, fake, ids = await synced(tmp_path)
    for event_id in ids[:24]:
        fake.cancel_event(GCAL, event_id)
    # 12 covered, 12 not: the 12 others are over max_count (10) and block the rule.
    result = (await h.engine.cycle(approvals={"family": Approval(None, "ui", copies(ids[:12]))})).rules[
        "family"
    ]  # type: ignore[attr-defined]
    assert result.status == "blocked" and result.released_covers == frozenset()
    # With only 5 others the guard lets everything through.
    h2, fake2, ids2 = await synced(tmp_path / "b")
    for event_id in ids2[:17]:
        fake2.cancel_event(GCAL, event_id)
    ok = (await h2.engine.cycle(approvals={"family": Approval(None, "ui", copies(ids2[:12]))})).rules[
        "family"
    ]  # type: ignore[attr-defined]
    assert ok.status == "ok" and len(ok.plan.delete) == 17 and ok.released_covers == copies(ids2[:12])


async def test_covers_naming_nothing_planned_change_nothing(tmp_path: Path) -> None:
    h, fake, ids = await synced(tmp_path)
    fake.cancel_event(GCAL, ids[0])
    result = (await h.engine.cycle(approvals={"family": Approval(None, "ui", copies(["zz", ids[0]]))})).rules[
        "family"
    ]  # type: ignore[attr-defined]
    assert result.status == "ok" and result.released_covers == copies([ids[0]])
    assert not [c for c in result.changes if c.action == "approved"]  # the guard would not have blocked one


async def started(tmp_path: Path) -> Running:
    """A running daemon whose first cycle made the copies (nothing blocked)."""
    d = Running(tmp_path)
    d.task = asyncio.create_task(d.daemon.run(d.world.stop))
    await until(lambda: len(d.world.delays) == 1)
    return d


async def test_the_daemon_keeps_covers_until_a_cycle_deletes_them(tmp_path: Path) -> None:
    d = await started(tmp_path)
    d.daemon.preapprove("family", copies(d.ids))
    assert d.daemon._covers  # pyright: ignore[reportPrivateUsage]
    for event_id in d.ids:
        d.world.fake.cancel_event(GCAL, event_id)
    await d.cycle()
    assert d.latest().status == "ok" and d.family_copies() == 0
    assert d.daemon._covers == {}  # pyright: ignore[reportPrivateUsage]
    await d.stop()


async def test_covers_expire(tmp_path: Path) -> None:
    d = await started(tmp_path)
    d.daemon.preapprove("family", copies(d.ids))
    d.world.now += COVER_TTL + timedelta(seconds=1)
    for event_id in d.ids:
        d.world.fake.cancel_event(GCAL, event_id)
    await d.cycle()
    assert d.latest().status == "blocked"  # the covers were gone: the guard held the deletes
    await d.stop()


async def test_an_unrelated_event_is_not_affected(tmp_path: Path) -> None:
    h, fake, ids = await synced(tmp_path)
    fake.put_event(
        GCAL, google_event("x1", "Extra", "2026-10-28T09:00:00+08:00", "2026-10-28T09:30:00+08:00")
    )
    result = (await h.engine.cycle(approvals={"family": Approval(None, "ui", copies(ids))})).rules["family"]  # type: ignore[attr-defined]
    assert result.status == "ok" and len(result.plan.create) == 1 and result.released_covers == frozenset()


async def test_the_daemon_stores_the_scopes_a_token_refresh_reported(tmp_path: Path) -> None:
    d = await started(tmp_path)
    source = d.daemon._adapters.sources["me"]  # pyright: ignore[reportPrivateUsage]
    source._token.scopes = frozenset({"s.readonly", "s.events"})  # type: ignore[attr-defined]  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    await d.cycle()
    from calsync.store import Store

    with Store(tmp_path / "state.db") as store:
        assert store.scopes("me") == frozenset({"s.readonly", "s.events"})
    await d.stop()
