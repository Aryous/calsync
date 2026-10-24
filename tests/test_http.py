import httpx
import pytest

from calsync.errors import TransientError
from calsync.http import USER_AGENT, RequestLog, make_client, request_with_retry

pytestmark = pytest.mark.anyio


async def no_sleep(_: float) -> None:
    return None


def scripted(*statuses: int, headers: dict[str, str] | None = None) -> httpx.MockTransport:
    queue = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(queue.pop(0), headers=headers or {}, request=request)

    return httpx.MockTransport(handler)


async def test_client_headers_and_log() -> None:
    log = RequestLog()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with make_client(log, transport=httpx.MockTransport(handler)) as client:
        await request_with_retry(client, "GET", "https://x.test/a", kind="probe", sleep=no_sleep)
    assert seen[0].headers["user-agent"] == USER_AGENT and "gzip" in USER_AGENT
    assert seen[0].headers["accept-encoding"] == "gzip"
    assert log.kinds() == ["probe"] and log.count("probe") == 1


async def test_retries_then_succeeds_and_reports_throttle() -> None:
    log = RequestLog()
    throttled: list[bool] = []
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with make_client(log, transport=scripted(503, 429, 200, headers={"Retry-After": "2"})) as client:
        response = await request_with_retry(
            client,
            "GET",
            "https://x.test/a",
            kind="k",
            sleep=record_sleep,
            on_throttle=lambda: throttled.append(True),
        )
    assert response.status_code == 200
    assert log.count() == 3 and sleeps == [2.0, 2.0] and len(throttled) == 2


async def test_gives_up_with_transient_error() -> None:
    async with make_client(RequestLog(), transport=scripted(500, 500, 500, 500)) as client:
        with pytest.raises(TransientError, match="HTTP 500"):
            await request_with_retry(client, "GET", "https://x.test/a", kind="k", sleep=no_sleep)


async def test_client_errors_are_returned_not_retried() -> None:
    log = RequestLog()
    async with make_client(log, transport=scripted(412)) as client:
        response = await request_with_retry(client, "PUT", "https://x.test/a", kind="k", sleep=no_sleep)
    assert response.status_code == 412 and log.count() == 1


async def test_transport_errors_are_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200)

    async with make_client(RequestLog(), transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(client, "GET", "https://x.test/a", kind="k", sleep=no_sleep)
    assert response.status_code == 200 and calls["n"] == 2
