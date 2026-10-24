"""Google OAuth access tokens.

Access tokens live only in memory and are refreshed 5 minutes before expiry.
The interactive browser consent flow (loopback + PKCE) is added by the
runtime plan; this module only exchanges a stored refresh token.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from calsync.errors import AuthError, ConfigError, TransientError
from calsync.http import request_with_retry

TOKEN_URL = "https://oauth2.googleapis.com/token"
REFRESH_MARGIN = 300.0

# A provider may also define ``invalidate() -> bool``: drop the cached token and
# report whether a fresh one can be obtained (used to retry once after a 401).
TokenProvider = Callable[[], Awaitable[str]]


class StaticToken:
    def __init__(self, token: str) -> None:
        self._token = token

    async def __call__(self) -> str:
        return self._token

    def invalidate(self) -> bool:
        """A static token cannot be refreshed, so a retry would be futile."""
        return False


class RefreshTokenProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_url: str = TOKEN_URL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._form = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        self._url = token_url
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._token is not None and self._clock() < self._expires_at - REFRESH_MARGIN

    def invalidate(self) -> bool:
        """Drop the cached token (e.g. after a 401); True: the next call fetches a fresh one."""
        self._token = None
        self._expires_at = 0.0
        return True

    async def __call__(self) -> str:
        if self._fresh():
            assert self._token is not None
            return self._token
        async with self._lock:
            if self._fresh():
                assert self._token is not None
                return self._token
            response = await request_with_retry(
                self._client, "POST", self._url, kind="google-token", data=self._form
            )
            if response.status_code in (400, 401):
                raise AuthError(f"Google refused the refresh token: {response.text[:200]}")
            if response.status_code >= 400:
                raise TransientError(f"Google token endpoint: HTTP {response.status_code}")
            try:
                data = response.json()
                token = data["access_token"]
            except (ValueError, KeyError, TypeError):
                raise AuthError("Google token response has no usable access_token") from None
            if not isinstance(token, str) or not token:
                raise AuthError("Google token response has an empty access_token")
            self._token = token
            self._expires_at = self._clock() + float(data.get("expires_in", 3600))
            return token


def load_client_secret(path: Path) -> tuple[str, str]:
    """Read client_id / client_secret from the JSON downloaded from Google Cloud Console."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError([f"google client secret file not found: {path}"]) from None
    except json.JSONDecodeError as exc:
        raise ConfigError([f"{path}: invalid JSON ({exc})"]) from None
    section = data.get("installed") or data.get("web") or data
    try:
        return section["client_id"], section["client_secret"]
    except KeyError:
        raise ConfigError([f"{path}: missing client_id/client_secret"]) from None


def parse_refresh_token(secret: str) -> str:
    """Stored Google credentials are JSON: {"refresh_token": "..."}."""
    try:
        token = json.loads(secret)["refresh_token"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise AuthError("stored Google credential is not valid JSON with a refresh_token") from None
    if not isinstance(token, str) or not token:
        raise AuthError("stored Google credential has an empty refresh_token")
    return token
