"""Edge cases of the CalDAV target and DAV client using MockTransport."""

import asyncio

import httpx
import pytest

from calsync.errors import CalsyncError
from calsync.http import RequestLog, make_client
from calsync.naming import resource_name
from calsync.targets.base import TargetCalendar
from calsync.targets.caldav import CalDavTarget, Limiter
from calsync.targets.dav import DavClient

pytestmark = pytest.mark.anyio

OID = "c" * 24
CAL = TargetCalendar("https://dav.test/cal/", "Cal", "sync")


def _multistatus(*responses: str) -> bytes:
    return (
        "<?xml version='1.0'?><multistatus xmlns='DAV:' xmlns:C='urn:ietf:params:xml:ns:caldav'>"
        + "".join(responses)
        + "</multistatus>"
    ).encode()


def _target(handler: object) -> CalDavTarget:
    client = make_client(RequestLog(), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return CalDavTarget(client, "https://dav.test/")


async def test_writes_without_etag_send_if_match_star() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    dav = DavClient(make_client(RequestLog(), transport=httpx.MockTransport(handler)), "https://dav.test/")
    await dav.put("/cal/x.ics", b"ICS", if_match=None)
    await dav.delete("/cal/x.ics", if_match=None)
    assert seen[0].headers["if-match"] == "*" and "if-none-match" not in seen[0].headers
    assert seen[1].headers["if-match"] == "*"


async def test_truncated_initial_sync_is_not_full() -> None:
    name = resource_name("r", OID)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            207,
            content=_multistatus(
                "<response><href>/cal/</href><status>HTTP/1.1 507 Insufficient Storage</status></response>",
                f'<response><href>/cal/{name}</href><propstat><prop><getetag>"e"</getetag></prop>'
                "<status>HTTP/1.1 200 OK</status></propstat></response>",
                "<sync-token>t1</sync-token>",
            ),
        )

    delta = await _target(handler).changes(CAL, None)
    assert not delta.full
    assert list(delta.changed) == [f"https://dav.test/cal/{name}"]


async def test_empty_propfind_raises_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(207, content=_multistatus())

    with pytest.raises(CalsyncError, match="unexpected PROPFIND response"):
        await _target(handler).list_calendars()
    with pytest.raises(CalsyncError, match="unexpected PROPFIND response"):
        await _target(handler).changes(TargetCalendar(CAL.href, "Cal", "ctag"), None)


def test_limiter_rejects_zero() -> None:
    with pytest.raises(ValueError):
        Limiter(0)


async def test_limiter_blocks_then_reset_wakes_waiters() -> None:
    limiter = Limiter(2)
    limiter.throttle()
    events: list[str] = []
    release = asyncio.Event()
    b_in = asyncio.Event()

    async def holder(name: str) -> None:
        async with limiter:
            events.append(f"in:{name}")
            if name == "b":
                b_in.set()
            await release.wait()
        events.append(f"out:{name}")

    first = asyncio.create_task(holder("a"))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(holder("b"))
    await asyncio.sleep(0.05)
    assert events == ["in:a"]  # limit 1: b is blocked while a holds

    limiter.reset()  # sync call must wake the waiter
    await asyncio.wait_for(b_in.wait(), 1)
    assert events[:2] == ["in:a", "in:b"]  # both inside at once
    release.set()
    await asyncio.gather(first, second)
