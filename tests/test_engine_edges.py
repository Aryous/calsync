"""Engine edge cases against FakeGoogle + an in-memory FakeTarget (no Radicale)."""

from pathlib import Path

import pytest

from calsync.errors import TransientError
from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, build_fake, config_for, rule

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
