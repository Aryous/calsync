"""Edits made directly on a CalDAV server are reverted to the source."""

from collections import Counter
from pathlib import Path

import pytest

from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, Harness, build, config_for, rule

pytestmark = [pytest.mark.anyio, pytest.mark.e2e]

# The rendered DTSTART of the one event; DTSTAMP (20261024T080000Z) must not match these.
START, MOVED = b"20261025T080000", b"20261025T083000"
ONE_IDLE = Counter({"google-calendarlist": 1, "google-events": 1, "sync-collection": 1})


def one_event() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(GCAL, google_event("p1", "Walk", "2026-10-25T08:00:00+08:00", "2026-10-25T08:05:00+08:00"))
    return fake


async def edit_on_server(h: Harness, old: bytes, new: bytes) -> str:
    """Change the one managed copy directly on Radicale, keeping our marker (like a phone)."""
    cal = await h.target.ensure_calendar("Family", create=False)
    hrefs = list(h.store.load_target_cache("dav", cal.href).objects)
    [obj] = await h.target.fetch(cal, hrefs)
    edited = obj.data.replace(old, new)
    assert edited != obj.data and b"X-CALSYNC-HASH" in edited
    await h.target.update(obj.href, obj.etag, edited)
    return obj.href


async def test_phone_style_edit_is_reverted_then_idle(
    tmp_path: Path, radicale_url: str, dav_user: str
) -> None:
    h = build(tmp_path, radicale_url, dav_user, one_event(), config_for([rule()]))
    await h.engine.cycle()
    href = await edit_on_server(h, START, MOVED)  # move the start by 30 minutes

    res = (await h.engine.cycle()).rules["family"]
    assert res.updated == 1 and res.reverted == 1
    cal = await h.target.ensure_calendar("Family", create=False)
    [back] = await h.target.fetch(cal, [href])
    assert START in back.data and MOVED not in back.data

    mark = h.log.mark()
    res = (await h.engine.cycle()).rules["family"]
    assert res.updated == 0
    assert Counter(e.kind for e in h.log.since(mark)) == ONE_IDLE


async def test_edit_of_ignored_properties_only_is_not_rewritten(
    tmp_path: Path, radicale_url: str, dav_user: str
) -> None:
    """A client that only bumps SEQUENCE / adds X- props changes the etag but not the content."""
    h = build(tmp_path, radicale_url, dav_user, one_event(), config_for([rule()]))
    await h.engine.cycle()
    await edit_on_server(h, b"END:VEVENT", b"SEQUENCE:3\r\nX-EXAMPLE-SEEN:1\r\nEND:VEVENT")

    mark = h.log.mark()
    res = (await h.engine.cycle()).rules["family"]
    assert (res.updated, res.reverted) == (0, 0)
    assert "put" not in Counter(e.kind for e in h.log.since(mark))
    mark = h.log.mark()
    await h.engine.cycle()
    assert Counter(e.kind for e in h.log.since(mark)) == ONE_IDLE
