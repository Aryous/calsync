"""Discovery against a mocked iCloud-like server: redirects and a home set on another host."""

import httpx
import pytest

from calsync.errors import CalendarNotListed, NotFoundError
from calsync.http import RequestLog, make_client
from calsync.targets.caldav import CalDavTarget

pytestmark = pytest.mark.anyio


def _multistatus(*responses: str) -> bytes:
    return (
        "<?xml version='1.0'?><multistatus xmlns='DAV:' xmlns:C='urn:ietf:params:xml:ns:caldav'>"
        + "".join(responses)
        + "</multistatus>"
    ).encode()


async def test_discovery_follows_redirects_and_home_on_other_host() -> None:
    """iCloud: discovery starts on caldav.icloud.com, calendars live on pNN-caldav.icloud.com."""
    seen: list[tuple[str, str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append((request.method, url, "authorization" in request.headers))
        if url == "https://dav.test/":
            return httpx.Response(301, headers={"Location": "https://dav.test/root/"})
        if url == "https://dav.test/root/":
            return httpx.Response(
                207,
                content=_multistatus(
                    "<response><href>/root/</href><propstat><prop><current-user-principal>"
                    "<href>/123/principal/</href></current-user-principal></prop>"
                    "<status>HTTP/1.1 200 OK</status></propstat></response>"
                ),
            )
        if url == "https://dav.test/123/principal/":
            return httpx.Response(
                207,
                content=_multistatus(
                    "<response><href>/123/principal/</href><propstat><prop><C:calendar-home-set>"
                    "<href>https://p07-dav.test/123/calendars/</href></C:calendar-home-set></prop>"
                    "<status>HTTP/1.1 200 OK</status></propstat></response>"
                ),
            )
        if url == "https://p07-dav.test/123/calendars/":
            return httpx.Response(
                207,
                content=_multistatus(
                    "<response><href>/123/calendars/home/</href><propstat><prop>"
                    "<resourcetype><collection/><C:calendar/></resourcetype><displayname>Home</displayname>"
                    "</prop><status>HTTP/1.1 200 OK</status></propstat></response>"
                ),
            )
        return httpx.Response(404)

    client = make_client(RequestLog(), transport=httpx.MockTransport(handler), auth=httpx.BasicAuth("u", "p"))
    target = CalDavTarget(client, "https://dav.test/")
    (cal,) = await target.list_calendars()
    assert cal.href == "https://p07-dav.test/123/calendars/home/" and cal.name == "Home"
    assert target.href_for(cal, "x.ics") == "https://p07-dav.test/123/calendars/home/x.ics"
    assert [m for m, _, _ in seen] == ["PROPFIND"] * 4  # redirect, root, principal, home
    assert all(has_auth for _, _, has_auth in seen)


def _discovery(home_status: int) -> httpx.MockTransport:
    """A server whose principal names ``/1/calendars/``; that home answers ``home_status``
    (207: one calendar, ``Home``)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/":
            return httpx.Response(
                207,
                content=_multistatus(
                    "<response><href>/</href><propstat><prop><current-user-principal>"
                    "<href>/1/principal/</href></current-user-principal></prop>"
                    "<status>HTTP/1.1 200 OK</status></propstat></response>"
                ),
            )
        if path == "/1/principal/":
            return httpx.Response(
                207,
                content=_multistatus(
                    "<response><href>/1/principal/</href><propstat><prop><C:calendar-home-set>"
                    "<href>/1/calendars/</href></C:calendar-home-set></prop>"
                    "<status>HTTP/1.1 200 OK</status></propstat></response>"
                ),
            )
        if path == "/1/calendars/" and home_status == 207:
            return httpx.Response(
                207,
                content=_multistatus(
                    "<response><href>/1/calendars/home/</href><propstat><prop>"
                    "<resourcetype><collection/><C:calendar/></resourcetype><displayname>Home</displayname>"
                    "</prop><status>HTTP/1.1 200 OK</status></propstat></response>"
                ),
            )
        return httpx.Response(home_status if path == "/1/calendars/" else 404)

    return httpx.MockTransport(handler)


async def test_a_calendar_missing_from_a_listing_is_not_listed() -> None:
    client = make_client(RequestLog(), transport=_discovery(207), auth=httpx.BasicAuth("u", "p"))
    target = CalDavTarget(client, "https://dav.test/")
    with pytest.raises(CalendarNotListed):
        await target.ensure_calendar("Family", create=False)


async def test_a_discovery_404_is_not_a_missing_calendar() -> None:
    """A 404 on the way to the listing says nothing about the calendar: not ``CalendarNotListed``
    (which lets a retiring rule finish)."""
    client = make_client(RequestLog(), transport=_discovery(404), auth=httpx.BasicAuth("u", "p"))
    target = CalDavTarget(client, "https://dav.test/")
    with pytest.raises(NotFoundError) as caught:
        await target.ensure_calendar("Family", create=False)
    assert not isinstance(caught.value, CalendarNotListed)
