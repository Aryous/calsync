import io
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from calsync.errors import AuthError
from calsync.http import RequestLog, make_client
from calsync.sources.google_oauth import (
    SCOPE,
    LoopbackReceiver,
    authorization_url,
    exchange_code,
    pkce_challenge,
    sign_in,
)

pytestmark = pytest.mark.anyio


def test_pkce_challenge_matches_rfc7636_example() -> None:
    assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_authorization_url_requests_offline_readonly_access() -> None:
    url = authorization_url("cid", "http://127.0.0.1:5000/", "chal", "st")
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q == {
        "client_id": "cid",
        "redirect_uri": "http://127.0.0.1:5000/",
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": "chal",
        "code_challenge_method": "S256",
        "state": "st",
    }


def test_authorization_url_asks_for_write_access_when_told() -> None:
    from calsync.sources.google_oauth import WRITE_SCOPES

    url = authorization_url("cid", "http://127.0.0.1:5000/", "chal", "st", scope=WRITE_SCOPES)
    scope = parse_qs(urlsplit(url).query)["scope"][0]
    assert scope.split() == [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    ]


async def _hit(url: str) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(url)


async def test_receiver_returns_code_and_ignores_other_paths() -> None:
    async with LoopbackReceiver() as rx:
        assert (await _hit(f"http://127.0.0.1:{rx.port}/favicon.ico")).status_code == 404
        page = await _hit(f"{rx.redirect_uri}?state=s1&code=abc")
        assert page.status_code == 200 and "close this tab" in page.text
        assert await rx.wait("s1", wait_seconds=5) == "abc"


async def test_receiver_rejects_state_mismatch_error_and_timeout() -> None:
    async with LoopbackReceiver() as rx:
        await _hit(f"{rx.redirect_uri}?state=other&code=abc")
        with pytest.raises(AuthError, match="state mismatch"):
            await rx.wait("s1", wait_seconds=5)
    async with LoopbackReceiver() as rx:
        await _hit(f"{rx.redirect_uri}?state=s1&error=access_denied")
        with pytest.raises(AuthError, match="access_denied"):
            await rx.wait("s1", wait_seconds=5)
    async with LoopbackReceiver() as rx:
        with pytest.raises(AuthError, match="no sign-in received"):
            await rx.wait("s1", wait_seconds=0.05)


def _token_endpoint(status: int, body: object, seen: list[dict[str, str]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


async def test_exchange_code_sends_pkce_and_returns_refresh_token() -> None:
    seen: list[dict[str, str]] = []
    client = make_client(
        RequestLog(), transport=_token_endpoint(200, {"refresh_token": "rt", "access_token": "a"}, seen)
    )
    token = await exchange_code(
        client,
        client_id="cid",
        client_secret="cs",
        code="abc",
        verifier="ver",
        redirect_uri="http://127.0.0.1:1/",
    )
    assert token == "rt"
    assert seen[0] == {
        "client_id": "cid",
        "client_secret": "cs",
        "code": "abc",
        "code_verifier": "ver",
        "redirect_uri": "http://127.0.0.1:1/",
        "grant_type": "authorization_code",
    }


@pytest.mark.parametrize(
    ("status", "body", "match"),
    [(400, {"error": "invalid_grant"}, "invalid_grant"), (200, {"access_token": "a"}, "no refresh_token")],
)
async def test_exchange_code_failures(status: int, body: object, match: str) -> None:
    client = make_client(RequestLog(), transport=_token_endpoint(status, body, []))
    with pytest.raises(AuthError, match=match):
        await exchange_code(
            client, client_id="c", client_secret="s", code="x", verifier="v", redirect_uri="u"
        )


async def test_sign_in_end_to_end_with_simulated_browser() -> None:
    client = make_client(RequestLog(), transport=_token_endpoint(200, {"refresh_token": "rt-final"}, []))
    opened: list[str] = []

    def browser(url: str) -> None:
        opened.append(url)
        q = parse_qs(urlsplit(url).query)
        redirect, state = q["redirect_uri"][0], q["state"][0]
        import asyncio

        asyncio.get_running_loop().create_task(_hit(f"{redirect}?state={state}&code=granted"))

    out = io.StringIO()
    token = await sign_in(
        client, client_id="cid", client_secret="cs", open_browser=browser, out=out, wait_seconds=5
    )
    assert token == "rt-final" and len(opened) == 1 and opened[0] in out.getvalue()
