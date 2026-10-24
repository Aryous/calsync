import xml.etree.ElementTree as ET

import httpx
import pytest

from calsync.errors import AuthError, CalsyncError, ConflictError, CursorExpired, NotFoundError
from calsync.http import RequestLog, make_client
from calsync.targets.dav import (
    CALDAV,
    CS,
    DAV,
    DavClient,
    calendar_query_etags_body,
    multiget_body,
    parse_multistatus,
    q,
    sync_collection_body,
)

pytestmark = pytest.mark.anyio

SYNC_RESPONSE = b"""<?xml version='1.0' encoding='utf-8'?>
<multistatus xmlns="DAV:"><sync-token>http://example.com/sync/2</sync-token>
<response><href>/u/cal/calsync-aaaaaaaa-bbbbbbbbbbbbbbbbbbbbbbbb.ics</href>
<propstat><prop><getetag>"e1"</getetag></prop><status>HTTP/1.1 200 OK</status></propstat></response>
<response><href>/u/cal/gone%20file.ics</href><status>HTTP/1.1 404 Not Found</status></response>
</multistatus>"""

PROPFIND_RESPONSE = b"""<?xml version='1.0' encoding='utf-8'?>
<multistatus xmlns="DAV:" xmlns:CS="http://calendarserver.org/ns/">
<response><href>/u/cal/</href>
<propstat><prop><displayname>Family</displayname><CS:getctag>"c1"</CS:getctag></prop><status>HTTP/1.1 200 OK</status></propstat>
<propstat><prop><sync-token/></prop><status>HTTP/1.1 404 Not Found</status></propstat>
</response></multistatus>"""


def test_parse_sync_collection() -> None:
    responses, token = parse_multistatus(SYNC_RESPONSE, "https://p01-dav.test/u/cal/")
    assert token == "http://example.com/sync/2"
    first, gone = responses
    assert first.status == 200 and first.text(q(DAV, "getetag")) == '"e1"'
    assert first.href == "https://p01-dav.test/u/cal/calsync-aaaaaaaa-bbbbbbbbbbbbbbbbbbbbbbbb.ics"
    assert (
        gone.href == "https://p01-dav.test/u/cal/gone%20file.ics" and gone.status == 404 and gone.props == {}
    )


def test_parse_skips_failed_propstat() -> None:
    (item,), token = parse_multistatus(PROPFIND_RESPONSE, "https://dav.test/")
    assert token is None
    assert item.text(q(DAV, "displayname")) == "Family"
    assert item.text(q(CS, "getctag")) == '"c1"'
    assert q(DAV, "sync-token") not in item.props


def test_request_bodies() -> None:
    initial = ET.fromstring(sync_collection_body(None))
    assert initial.tag == q(DAV, "sync-collection")
    assert initial.find(q(DAV, "sync-token")).text is None
    assert ET.fromstring(sync_collection_body("tok")).find(q(DAV, "sync-token")).text == "tok"
    body = ET.fromstring(multiget_body(["/a.ics", "/b.ics"]))
    assert [h.text for h in body.findall(q(DAV, "href"))] == ["/a.ics", "/b.ics"]
    assert body.find(f"{q(DAV, 'prop')}/{q(CALDAV, 'calendar-data')}") is not None


def client_with(status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> DavClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers or {})

    return DavClient(make_client(RequestLog(), transport=httpx.MockTransport(handler)), "https://dav.test/")


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, b"", AuthError),
        (403, b"", AuthError),
        (403, b"<error xmlns='DAV:'><valid-sync-token/></error>", CursorExpired),
        (409, b"<error xmlns='DAV:'><valid-sync-token/></error>", CursorExpired),
        (404, b"", NotFoundError),
        (412, b"", ConflictError),
    ],
)
async def test_error_mapping(status: int, body: bytes, error: type[Exception]) -> None:
    with pytest.raises(error):
        await client_with(status, body).report("/cal/", sync_collection_body("t"), kind="sync-collection")


async def test_put_headers_and_etag() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, headers={"ETag": '"new"'})

    dav = DavClient(
        make_client(RequestLog(), transport=httpx.MockTransport(handler)), "https://dav.test/root/"
    )
    assert await dav.put("/root/cal/x.ics", b"ICS", create_only=True) == '"new"'
    assert await dav.put("/root/cal/x.ics", b"ICS", if_match='"old"') == '"new"'
    assert seen[0].headers["if-none-match"] == "*" and "if-match" not in seen[0].headers
    assert seen[1].headers["if-match"] == '"old"'
    assert str(seen[0].url) == "https://dav.test/root/cal/x.ics"


async def test_redirect_loop_and_scheme_change_are_refused() -> None:
    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": str(request.url)})

    def downgrade(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "http://dav.test/"})

    for handler, message in ((loop, "too many redirects"), (downgrade, "refusing redirect")):
        dav = DavClient(
            make_client(RequestLog(), transport=httpx.MockTransport(handler)), "https://dav.test/"
        )
        with pytest.raises(CalsyncError, match=message):
            await dav.propfind("", [q(DAV, "displayname")], depth=0)


MULTISTATUS_REL = b"""<?xml version='1.0' encoding='utf-8'?>
<multistatus xmlns="DAV:"><response><href>/root/cal/</href>
<propstat><prop><displayname>X</displayname></prop><status>HTTP/1.1 200 OK</status></propstat>
</response></multistatus>"""


@pytest.mark.parametrize(
    ("kind", "body", "depth", "expected"),
    [
        ("sync-collection", sync_collection_body(None), None, "0"),
        ("calendar-query", calendar_query_etags_body(), None, "1"),
        ("calendar-multiget", multiget_body(["/a.ics"]), None, "1"),
        ("calendar-query", calendar_query_etags_body(), 0, "0"),
        ("sync-collection", sync_collection_body(None), 1, "1"),
    ],
)
async def test_report_depth(kind: str, body: bytes, depth: int | None, expected: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(207, content=MULTISTATUS_REL)

    dav = DavClient(make_client(RequestLog(), transport=httpx.MockTransport(handler)), "https://dav.test/")
    await dav.report("/cal/", body, kind=kind, **({} if depth is None else {"depth": depth}))
    assert seen[0].headers["depth"] == expected


async def test_invalid_multistatus_xml_is_typed_error() -> None:
    dav = client_with(207, b"<html><body>captive portal</html>")
    with pytest.raises(CalsyncError, match="invalid multistatus XML"):
        await dav.propfind("/cal/", [q(DAV, "displayname")], depth=0)
    with pytest.raises(CalsyncError, match="invalid multistatus XML"):
        await dav.report("/cal/", sync_collection_body(None), kind="sync-collection")


async def test_https_upgrade_redirect_is_allowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"Location": "https://dav.test/root/"})
        return httpx.Response(207, content=MULTISTATUS_REL)

    dav = DavClient(make_client(RequestLog(), transport=httpx.MockTransport(handler)), "http://dav.test/")
    (item,) = await dav.propfind("", [q(DAV, "displayname")], depth=0)
    assert item.href == "https://dav.test/root/cal/"


async def test_cross_host_redirect_keeps_auth_and_resolves_hrefs() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "caldav.test":
            return httpx.Response(301, headers={"Location": "https://p07-caldav.test/root/"})
        return httpx.Response(207, content=MULTISTATUS_REL)

    client = make_client(RequestLog(), transport=httpx.MockTransport(handler), auth=httpx.BasicAuth("u", "p"))
    dav = DavClient(client, "https://caldav.test/")
    (item,) = await dav.propfind("", [q(DAV, "displayname")], depth=0)
    assert [r.url.host for r in seen] == ["caldav.test", "p07-caldav.test"]
    assert seen[1].headers["authorization"].startswith("Basic ")
    assert seen[1].headers["depth"] == "0"
    assert item.href == "https://p07-caldav.test/root/cal/"
