"""CalDavTarget against a real local Radicale server."""

from dataclasses import replace

import httpx
import pytest

from calsync.errors import ConflictError, NotFoundError
from calsync.http import RequestLog, make_client
from calsync.naming import resource_name
from calsync.targets.caldav import CalDavTarget, Limiter

pytestmark = [pytest.mark.anyio, pytest.mark.e2e]

OID_A = "a" * 24
OID_B = "b" * 24


def ics(uid: str, summary: str = "x") -> bytes:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:20261024T000000Z\r\nDTSTART:20261025T010000Z\r\n"
        f"DTEND:20261025T020000Z\r\nSUMMARY:{summary}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


@pytest.fixture
def log() -> RequestLog:
    return RequestLog()


@pytest.fixture
def target(radicale_url: str, dav_user: str, log: RequestLog) -> CalDavTarget:
    client = make_client(log, auth=httpx.BasicAuth(dav_user, "pw"))
    return CalDavTarget(client, radicale_url, max_concurrency=2)


async def test_ensure_creates_and_finds_calendar(target: CalDavTarget) -> None:
    with pytest.raises(NotFoundError):
        await target.ensure_calendar("Family", create=False)
    cal = await target.ensure_calendar("Family", create=True)
    assert cal.name == "Family" and cal.mode == "sync"
    assert (await target.ensure_calendar("Family", create=False)).href == cal.href
    assert (await target.ensure_calendar(cal.href, create=False)).href == cal.href


async def test_write_detect_fetch_cycle(target: CalDavTarget, log: RequestLog) -> None:
    cal = await target.ensure_calendar("Family", create=True)
    initial = await target.changes(cal, None)
    assert initial.full and initial.changed == {}

    href_a = target.href_for(cal, resource_name("r", OID_A))
    etag = await target.create(href_a, ics(f"{OID_A}@calsync"))
    assert etag
    with pytest.raises(ConflictError):
        await target.create(href_a, ics(f"{OID_A}@calsync"))
    await target.create(target.href_for(cal, "foreign-event.ics"), ics("foreign"))

    delta = await target.changes(cal, initial.cursor)
    assert not delta.full and delta.changed == {href_a: etag}  # foreign resource is invisible

    mark = log.mark()
    idle = await target.changes(cal, delta.cursor)
    assert idle.changed == {} and idle.deleted == ()
    assert [e.kind for e in log.since(mark)] == ["sync-collection"]

    with pytest.raises(ConflictError):
        await target.update(href_a, '"stale"', ics(f"{OID_A}@calsync", "y"))
    new_etag = await target.update(href_a, etag, ics(f"{OID_A}@calsync", "y"))
    (fetched,) = await target.fetch(cal, [href_a])
    assert fetched.etag == new_etag and b"SUMMARY:y" in fetched.data

    await target.delete(href_a, new_etag)
    await target.delete(href_a, new_etag)  # already gone: no error
    gone = await target.changes(cal, idle.cursor)
    assert gone.deleted == (href_a,)


async def test_invalid_sync_token_falls_back_to_full(target: CalDavTarget) -> None:
    cal = await target.ensure_calendar("Family", create=True)
    href = target.href_for(cal, resource_name("r", OID_B))
    await target.create(href, ics(f"{OID_B}@calsync"))
    delta = await target.changes(cal, "http://radicale.org/ns/sync/deadbeef")
    assert delta.full and list(delta.changed) == [href]


async def test_ctag_and_full_modes(target: CalDavTarget, log: RequestLog) -> None:
    cal = await target.ensure_calendar("Family", create=True)
    href = target.href_for(cal, resource_name("r", OID_A))
    await target.create(href, ics(f"{OID_A}@calsync"))

    ctag_cal = replace(cal, mode="ctag")
    first = await target.changes(ctag_cal, None)
    assert first.full and list(first.changed) == [href]
    mark = log.mark()
    same = await target.changes(ctag_cal, first.cursor)
    assert same.changed == {} and not same.full
    assert [e.kind for e in log.since(mark)] == ["propfind"]

    full = await target.changes(replace(cal, mode="full"), None)
    assert full.full and list(full.changed) == [href]


async def test_limiter_throttle_and_reset() -> None:
    limiter = Limiter(3)
    limiter.throttle()
    assert limiter.limit == 1
    limiter.reset()
    assert limiter.limit == 3
    async with limiter:
        pass


def test_same_href_matches_a_url_or_its_path() -> None:
    """One matcher for ``CalDavTarget.ensure_calendar`` and the calendar cache (``ops``)."""
    from calsync.naming import same_href

    href = "https://dav.example.com/home/family/"
    assert same_href(href, "https://dav.example.com/home/family")  # the URL, trailing slash ignored
    assert same_href(href, "/home/family/")  # just the path
    assert not same_href(href, "https://other.example.com/home/family/")
    assert not same_href(href, "Family")  # a name is not a path
