"""iCloud region auto-detection: `server_url = "icloud"` tries global, then China mainland."""

import httpx
import pytest

from calsync.errors import AuthError, TransientError
from calsync.http import RequestLog, make_client
from calsync.targets.caldav import ICLOUD_SERVERS, CalDavTarget, server_candidates

pytestmark = pytest.mark.anyio

GLOBAL, CN = "caldav.icloud.com", "caldav.icloud.com.cn"


def _ms(*responses: str) -> bytes:
    return (
        "<?xml version='1.0'?><multistatus xmlns='DAV:' xmlns:C='urn:ietf:params:xml:ns:caldav'>"
        + "".join(responses)
        + "</multistatus>"
    ).encode()


def _discovery(host: str):  # a working iCloud-like server living on `host`
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/":
            return httpx.Response(
                207,
                content=_ms(
                    "<response><href>/</href><propstat><prop><current-user-principal>"
                    "<href>/42/principal/</href></current-user-principal></prop>"
                    "<status>HTTP/1.1 200 OK</status></propstat></response>"
                ),
            )
        if path == "/42/principal/":
            return httpx.Response(
                207,
                content=_ms(
                    "<response><href>/42/principal/</href><propstat><prop><C:calendar-home-set>"
                    f"<href>https://p01-{host}/42/calendars/</href></C:calendar-home-set></prop>"
                    "<status>HTTP/1.1 200 OK</status></propstat></response>"
                ),
            )
        if path == "/42/calendars/":
            return httpx.Response(
                207,
                content=_ms(
                    "<response><href>/42/calendars/home/</href><propstat><prop>"
                    "<resourcetype><collection/><C:calendar/></resourcetype><displayname>Home</displayname>"
                    "</prop><status>HTTP/1.1 200 OK</status></propstat></response>"
                ),
            )
        return httpx.Response(404)

    return handle


def _servers(routes: dict[str, object]) -> tuple[httpx.MockTransport, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        route = routes.get(request.url.host.removeprefix("p01-"))
        if route is None or route == 401:
            return httpx.Response(401)
        if route == "down":
            raise httpx.ConnectError("unreachable", request=request)
        return route(request)  # type: ignore[operator]

    return httpx.MockTransport(handler), seen


async def no_sleep(_: float) -> None:
    return None


def _target(transport: httpx.MockTransport) -> CalDavTarget:
    client = make_client(RequestLog(), transport=transport, auth=httpx.BasicAuth("u@icloud.com", "pw"))
    return CalDavTarget(client, "icloud", sleep=no_sleep)


def test_server_candidates() -> None:
    assert server_candidates("icloud") == ICLOUD_SERVERS == (f"https://{GLOBAL}/", f"https://{CN}/")
    assert server_candidates("https://dav.example.com/") == ("https://dav.example.com/",)


async def test_china_mainland_account_is_detected() -> None:
    transport, seen = _servers({GLOBAL: 401, CN: _discovery(CN)})
    (cal,) = await _target(transport).list_calendars()
    assert cal.href == f"https://p01-{CN}/42/calendars/home/"
    assert seen[0] == GLOBAL and seen[1] == CN


async def test_global_account_never_touches_china_endpoint() -> None:
    transport, seen = _servers({GLOBAL: _discovery(GLOBAL), CN: _discovery(CN)})
    (cal,) = await _target(transport).list_calendars()
    assert cal.href == f"https://p01-{GLOBAL}/42/calendars/home/"
    assert CN not in seen and f"p01-{CN}" not in seen


async def test_rejected_everywhere_names_both_regions() -> None:
    transport, _ = _servers({GLOBAL: 401, CN: 401})
    with pytest.raises(AuthError) as info:
        await _target(transport).list_calendars()
    assert GLOBAL in str(info.value) and CN in str(info.value)


async def test_network_failure_is_not_mistaken_for_wrong_region() -> None:
    transport, seen = _servers({GLOBAL: "down", CN: _discovery(CN)})
    with pytest.raises(TransientError):
        await _target(transport).list_calendars()
    assert CN not in seen
