"""Interactive Google sign-in for `calsync auth`: OAuth 2.0 for installed apps.

Flow (Google "loopback IP address" redirect + PKCE, RFC 7636): open the
consent page in the browser, receive the authorization code on a one-shot
HTTP listener bound to 127.0.0.1, then exchange it for a refresh token.
"""

import asyncio
import base64
import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import TextIO
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from calsync.errors import AuthError, TransientError
from calsync.http import request_with_retry
from calsync.sources.google_auth import TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
SIGN_IN_TIMEOUT = 300.0

_DONE_PAGE = (
    "<!doctype html><meta charset='utf-8'><title>calsync</title>"
    "<p>calsync: sign-in received. You can close this tab.</p>"
)


@dataclass(frozen=True, slots=True)
class Pkce:
    verifier: str
    challenge: str


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_pkce() -> Pkce:
    verifier = secrets.token_urlsafe(64)
    return Pkce(verifier, pkce_challenge(verifier))


def authorization_url(
    client_id: str, redirect_uri: str, challenge: str, state: str, *, auth_url: str = AUTH_URL
) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",  # we need a refresh token
            "prompt": "consent",  # always return a refresh token, even on re-auth
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{auth_url}?{query}"


class LoopbackReceiver:
    """One-shot HTTP listener on 127.0.0.1 that captures the OAuth redirect."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._result: asyncio.Future[dict[str, list[str]]] | None = None
        self.port = 0

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    async def __aenter__(self) -> "LoopbackReceiver":
        self._result = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await reader.readline()).decode("latin-1")
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass  # drain headers
            parts = request_line.split()
            target = parts[1] if len(parts) >= 2 else "/"
            params = parse_qs(urlsplit(target).query)
            if "code" in params or "error" in params:
                body, status = _DONE_PAGE, "200 OK"
                if self._result is not None and not self._result.done():
                    self._result.set_result(params)
            else:
                body, status = "not found", "404 Not Found"  # e.g. /favicon.ico
            payload = body.encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        finally:
            writer.close()

    async def wait(self, state: str, wait_seconds: float = SIGN_IN_TIMEOUT) -> str:
        assert self._result is not None, "use as an async context manager"
        try:
            params = await asyncio.wait_for(self._result, wait_seconds)
        except TimeoutError:
            raise AuthError(f"no sign-in received within {int(wait_seconds)} seconds") from None
        if params.get("state", [""])[0] != state:
            raise AuthError("sign-in response did not match this request (state mismatch)")
        if "error" in params:
            raise AuthError(f"Google sign-in was not completed: {params['error'][0]}")
        return params["code"][0]


async def exchange_code(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    token_url: str = TOKEN_URL,
) -> str:
    """Exchange an authorization code for a refresh token."""
    response = await request_with_retry(
        client,
        "POST",
        token_url,
        kind="google-token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    try:
        data = response.json()
    except ValueError:
        raise TransientError(
            f"Google token endpoint returned non-JSON (HTTP {response.status_code})"
        ) from None
    if response.status_code >= 400 or not isinstance(data, dict):
        reason = data.get("error") if isinstance(data, dict) else None
        raise AuthError(f"Google refused the authorization code: {reason or response.status_code}")
    token = data.get("refresh_token")
    if not isinstance(token, str) or not token:
        raise AuthError("Google returned no refresh_token; run `calsync auth` again")
    return token


async def sign_in(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    open_browser: Callable[[str], object],
    out: TextIO,
    wait_seconds: float = SIGN_IN_TIMEOUT,
    auth_url: str = AUTH_URL,
    token_url: str = TOKEN_URL,
) -> str:
    """Run the browser flow and return a refresh token."""
    pkce = new_pkce()
    state = secrets.token_urlsafe(24)
    async with LoopbackReceiver() as receiver:
        url = authorization_url(client_id, receiver.redirect_uri, pkce.challenge, state, auth_url=auth_url)
        out.write(f"Opening your browser to sign in to Google. If it does not open, visit:\n{url}\n")
        out.flush()
        open_browser(url)
        code = await receiver.wait(state, wait_seconds)
    return await exchange_code(
        client,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        verifier=pkce.verifier,
        redirect_uri=receiver.redirect_uri,
        token_url=token_url,
    )
