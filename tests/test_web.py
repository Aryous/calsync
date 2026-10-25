"""The web UI over TCP (``calsync.web``): key-link pairing, the cookie, Host and Origin checks.

Requests go through ``httpx.ASGITransport`` (from 127.0.0.1); the client's ``base_url`` sets the
``Host`` header. The TCP listener of the daemon is tested at the end, over a real socket.
"""

import asyncio
import errno
import hashlib
import hmac
import logging
import socket
import stat
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from calsync import api
from calsync.config import Config, WebConfig
from calsync.daemon import Daemon, DaemonInfo
from calsync.errors import ConfigError, ListenError
from calsync.web import auth
from calsync.web.app import create_web_app
from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, NOW, build_fake, config_for, rule

INVALID = (
    '<p class="notice" role="alert">This link is not valid any more. Run <code>calsync web pair</code> '
    "on the computer running calsync to get a new one.</p>"
)
TOO_MANY = '<p class="notice" role="alert">Too many attempts. Try again in a few minutes.</p>'
MARKER = "<!-- calsync:notice -->"
pytestmark = pytest.mark.anyio

UNPAIRED = {"error": {"kind": "unpaired", "message": "This browser is not paired."}}


def cookie_header(token: str, *, secure: bool = False) -> str:
    return f"calsync_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=34560000" + (
        "; Secure" if secure else ""
    )


class FakeDaemon:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def request_cycle(self, rule: str | None = None) -> bool:
        self.calls.append(rule)
        return True

    def snapshot(self) -> DaemonInfo:
        return DaemonInfo(True, True, 4242, NOW, NOW, NOW + timedelta(seconds=60), 1)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def state(tmp_path: Path) -> Path:
    """A state directory after one cycle (no pairing key yet)."""
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(
        GCAL, google_event("lunch", "和朋友吃午饭", "2026-10-25T12:00:00+08:00", "2026-10-25T13:00:00+08:00")
    )
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    h.store.close()
    return tmp_path


def web_config(*allowed: str) -> Config:
    return replace(config_for([rule()]), web=WebConfig(allowed_hosts=allowed))


def client(
    state_dir: Path,
    *,
    base_url: str = "http://127.0.0.1:8766",
    listen_host: str = "127.0.0.1",
    allowed: tuple[str, ...] = (),
    daemon: Any = None,
    clock: Any = lambda: NOW,
) -> httpx.AsyncClient:
    config = web_config(*allowed)
    app = create_web_app(
        config_provider=lambda: config,
        state_dir=state_dir,
        daemon=daemon,
        listen_host=listen_host,
        clock=clock,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


async def paired(state_dir: Path, **kwargs: Any) -> httpx.AsyncClient:
    c = client(state_dir, **kwargs)
    key = auth.load_or_create_key(state_dir)
    assert (await c.get("/", params={"k": key})).status_code == 303
    return c


# ----- the key ---------------------------------------------------------------------


async def test_key_file_is_private_and_stable(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    key = auth.load_or_create_key(state_dir)
    path = state_dir / "web.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert len(key) == 43 and path.read_text().strip() == key  # secrets.token_urlsafe(32)
    assert auth.load_or_create_key(state_dir) == key
    new = auth.reset_key(state_dir)
    assert new != key and auth.load_or_create_key(state_dir) == new
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o602, 0o644])
async def test_a_key_file_other_users_can_reach_is_refused(tmp_path: Path, mode: int) -> None:
    state_dir = tmp_path / "state"
    key = auth.load_or_create_key(state_dir)
    path = state_dir / "web.key"
    path.chmod(mode)
    with pytest.raises(auth.InsecureKeyFile) as info:
        auth.load_or_create_key(state_dir)
    assert str(info.value) == (
        f"other users can access {path} (mode {mode:04o}); run `chmod 600 {path}`, "
        "or replace the key with `calsync web reset-key`"
    )
    with pytest.raises(auth.InsecureKeyFile):
        auth.Keys(state_dir).matches(key)
    assert path.read_text().strip() == key  # left alone
    new = auth.reset_key(state_dir)  # recovers without reading it
    assert new != key and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.load_or_create_key(state_dir) == new


async def test_the_server_refuses_a_key_file_others_can_read(
    state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    key = auth.load_or_create_key(state)
    (state / "web.key").chmod(0o644)
    caplog.set_level(logging.DEBUG)
    c = client(state)
    response = await c.get("/", params={"k": key})
    assert response.status_code == 500 and "set-cookie" not in response.headers
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("calsync")]
    assert ours == ["an API request failed (InsecureKeyFile)"]  # the client's own httpx log aside


async def test_session_token() -> None:
    expected = hmac.new(b"k3y", b"calsync-session-v1", hashlib.sha256).hexdigest()
    assert auth.session_token("k3y") == expected


# ----- pairing ----------------------------------------------------------------------


async def test_a_pair_link_sets_the_cookie_and_drops_the_query(state: Path) -> None:
    key = auth.load_or_create_key(state)
    c = client(state)
    response = await c.get("/", params={"k": key, "lang": "en"})
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert response.headers["set-cookie"] == cookie_header(auth.session_token(key))
    page = await c.get("/")
    assert page.status_code == 200 and '<script type="module" src="/static/app.js">' in page.text
    assert page.headers["cache-control"] == "no-cache"
    assert page.headers["content-type"].startswith("text/html")


async def test_the_cookie_is_secure_over_https(state: Path) -> None:
    key = auth.load_or_create_key(state)
    response = await client(state, base_url="https://localhost:8766").get("/", params={"k": key})
    assert response.headers["set-cookie"] == cookie_header(auth.session_token(key), secure=True)


async def test_without_a_cookie_the_root_is_the_pair_page(state: Path) -> None:
    auth.load_or_create_key(state)
    response = await client(state).get("/")
    assert response.status_code == 200
    assert 'action="/pair"' in response.text and MARKER in response.text  # no notice
    assert response.headers["cache-control"] == "no-cache" and "set-cookie" not in response.headers


async def test_a_bad_link_is_403_with_a_notice(state: Path) -> None:
    auth.load_or_create_key(state)
    response = await client(state).get("/", params={"k": "not-the-key"})
    assert response.status_code == 403 and "set-cookie" not in response.headers
    assert INVALID in response.text and MARKER not in response.text
    assert 'action="/pair"' in response.text


async def test_without_a_key_file_nothing_pairs(tmp_path: Path) -> None:
    c = client(tmp_path)
    assert (await c.get("/", params={"k": ""})).status_code == 403
    assert (await c.post("/pair", data={"key": ""})).status_code == 403
    assert (await c.get("/api/status")).status_code == 401
    assert not (tmp_path / "web.key").exists()  # only `calsync web pair` creates it


async def test_the_pair_form(state: Path) -> None:
    key = auth.load_or_create_key(state)
    c = client(state)
    wrong = await c.post("/pair", data={"key": "nope"})
    assert wrong.status_code == 403 and INVALID in wrong.text
    response = await c.post("/pair", data={"key": f"  {key}\n"})  # pasted with whitespace
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert response.headers["set-cookie"] == cookie_header(auth.session_token(key))
    assert (await c.get("/api/status")).status_code == 200


async def test_the_pair_form_takes_a_whole_link(state: Path) -> None:
    key = auth.load_or_create_key(state)
    response = await client(state).post("/pair", data={"key": f"http://localhost:8766/?k={key}"})
    assert response.status_code == 303


async def test_reset_key_unpairs_every_browser(state: Path) -> None:
    c = await paired(state)
    assert (await c.get("/api/status")).status_code == 200
    new = auth.reset_key(state)
    assert (await c.get("/api/status")).status_code == 401  # no restart needed
    assert 'action="/pair"' in (await c.get("/")).text
    assert (await c.get("/", params={"k": new})).status_code == 303
    assert (await c.get("/api/status")).status_code == 200


async def test_too_many_wrong_keys_are_429(state: Path) -> None:
    key = auth.load_or_create_key(state)
    clock = Clock()
    c = client(state, clock=clock)
    for _ in range(5):
        assert (await c.post("/pair", data={"key": "wrong"})).status_code == 403
    for _ in range(5):  # a wrong link counts too
        assert (await c.get("/", params={"k": "wrong"})).status_code == 403
    limited = await c.post("/pair", data={"key": key})  # even the right key, until the window passes
    assert limited.status_code == 429 and TOO_MANY in limited.text
    assert int(limited.headers["retry-after"]) == 600
    assert (await c.get("/", params={"k": key})).status_code == 429
    clock.now += timedelta(minutes=10, seconds=1)
    assert (await c.post("/pair", data={"key": key})).status_code == 303


# ----- the API -----------------------------------------------------------------------


async def test_the_api_needs_the_cookie(state: Path) -> None:
    auth.load_or_create_key(state)
    c = client(state)
    for path in ("/api/status", "/api/upcoming", "/api/nope"):
        response = await c.get(path)
        assert response.status_code == 401 and response.json() == UNPAIRED
    assert (await c.post("/api/trigger")).status_code == 401


async def test_the_api_with_the_cookie(state: Path) -> None:
    daemon = FakeDaemon()
    c = await paired(state, daemon=daemon)
    status = await c.get("/api/status")
    assert status.status_code == 200 and status.json()["daemon"]["pid"] == 4242
    upcoming = (await c.get("/api/upcoming", params={"hours": 48})).json()
    assert [e["title"] for e in upcoming["events"]] == ["和朋友吃午饭"]
    assert (await c.post("/api/trigger")).status_code == 202 and daemon.calls == [None]


async def test_a_forged_cookie_is_refused(state: Path) -> None:
    auth.load_or_create_key(state)
    c = client(state)
    c.cookies.set("calsync_session", auth.session_token("another key"))
    assert (await c.get("/api/status")).status_code == 401


async def test_static_files_are_public(state: Path) -> None:
    c = client(state)
    for path in ("/static/app.js", "/static/styles.css", "/static/vendor/htm-preact-standalone.mjs"):
        response = await c.get(path)
        assert response.status_code == 200, path
    page = await c.get("/static/pair.html")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    assert (await c.get("/static/nope.js")).status_code == 404


# ----- Host and Origin ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "listen_host", "allowed", "code"),
    [
        ("http://127.0.0.1:8766", "127.0.0.1", (), 200),
        ("http://localhost:8766", "127.0.0.1", (), 200),
        ("http://[::1]:8766", "127.0.0.1", (), 200),
        ("http://evil.example:8766", "127.0.0.1", (), 400),
        ("http://mac-mini.local:8766", "127.0.0.1", ("mac-mini.local",), 200),
        ("http://MAC-MINI.local:8766", "127.0.0.1", ("mac-mini.local",), 200),
        ("http://192.168.1.5:8766", "127.0.0.1", (), 400),  # an IP needs a wildcard listen host
        ("http://192.168.1.5:8766", "0.0.0.0", (), 200),
        ("http://[fd00::5]:8766", "::", (), 200),
        ("http://nas.lan:8766", "0.0.0.0", (), 400),  # a name still needs allowed_hosts
        ("http://192.168.1.5:8766", "192.168.1.5", (), 200),  # the concrete listen host
        ("http://192.168.1.6:8766", "192.168.1.5", (), 400),
    ],
)
async def test_the_host_check(
    state: Path, base_url: str, listen_host: str, allowed: tuple[str, ...], code: int
) -> None:
    c = client(state, base_url=base_url, listen_host=listen_host, allowed=allowed)
    for path in ("/", "/static/app.js"):
        assert (await c.get(path)).status_code == code, path
    assert (await c.get("/api/status")).status_code == (401 if code == 200 else 400)


async def test_a_request_without_a_host_is_400(state: Path) -> None:
    c = client(state)
    assert (await c.get("/", headers={"host": ""})).status_code == 400


async def test_a_cross_origin_post_is_403(state: Path) -> None:
    daemon = FakeDaemon()
    c = await paired(state, daemon=daemon)
    for origin in ("http://evil.example", "http://127.0.0.1:9999", "null"):
        response = await c.post("/api/trigger", headers={"origin": origin})
        assert response.status_code == 403, origin
    key = auth.load_or_create_key(state)
    assert (
        await c.post("/pair", data={"key": key}, headers={"origin": "http://evil.example"})
    ).status_code == 403
    assert daemon.calls == []
    assert (await c.post("/api/trigger", headers={"origin": "http://127.0.0.1:8766"})).status_code == 202
    assert (await c.post("/api/trigger")).status_code == 202  # no Origin: not a browser's cross-site request
    assert (await c.get("/api/status", headers={"origin": "http://evil.example"})).status_code == 200


# ----- errors outside the endpoints ----------------------------------------------------

INTERNAL = {"error": {"kind": "internal", "message": "Internal error."}}


def assert_type_only(caplog: pytest.LogCaptureFixture, kind: str, *secrets: str) -> None:
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == [f"an API request failed ({kind})"]
    assert all(r.exc_info is None for r in caplog.records)
    assert "Traceback" not in caplog.text
    for text in secrets:
        assert text not in caplog.text


async def test_an_unreadable_key_file_in_the_cookie_check_is_a_plain_500(
    state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    key = auth.load_or_create_key(state)
    c = client(state)  # ASGITransport re-raises whatever escapes the app
    c.cookies.set("calsync_session", auth.session_token(key))
    caplog.set_level(logging.DEBUG)
    (state / "web.key").chmod(0)
    try:
        response = await c.get("/api/status")
    finally:
        (state / "web.key").chmod(0o600)
    assert response.status_code == 500 and response.json() == INTERNAL
    assert_type_only(caplog, "PermissionError", "web.key", str(state))


async def test_a_failing_config_in_the_host_check_is_a_plain_500(
    state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> Config:
        raise RuntimeError("和朋友吃午饭 at https://calendar.example.invalid/e/123")

    app = create_web_app(config_provider=broken, state_dir=state, daemon=None, listen_host="127.0.0.1")
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nas.lan") as c:
        response = await c.get("/static/favicon.svg")  # nas.lan: not loopback, so the config is read
    assert response.status_code == 500 and response.json() == INTERNAL
    assert "和朋友吃午饭" not in response.text
    assert_type_only(caplog, "RuntimeError", "和朋友吃午饭", "example.invalid")


# ----- the daemon's TCP listener ----------------------------------------------------------


async def test_a_no_sync_daemon_serves_tcp(state: Path, short_dir: Path) -> None:
    import shutil

    shutil.copy(state / "state.db", short_dir / "state.db")  # the socket goes in the short directory
    key = auth.load_or_create_key(short_dir)
    stop = asyncio.Event()
    daemon = Daemon(web_config(), short_dir, sync=False, listen=("127.0.0.1", 0), clock=lambda: NOW)
    task = asyncio.create_task(daemon.run(stop))
    try:
        for _ in range(500):
            if daemon.web_address is not None or task.done():
                break
            await asyncio.sleep(0.01)
        assert daemon.web_address is not None
        host, port = daemon.web_address
        assert host == "127.0.0.1" and port > 0
        # trust_env=False: a system proxy (macOS settings) must not stand in for the daemon
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as c:
            static = await c.get("/static/favicon.svg")
            unpaired = await c.get("/api/status")
            pair = await c.get("/", params={"k": key})
            status = await c.get("/api/status")
        assert static.status_code == 200 and static.headers["content-type"].startswith("image/svg")
        assert unpaired.status_code == 401 and unpaired.json() == UNPAIRED
        assert pair.status_code == 303
        assert status.status_code == 200 and status.json()["daemon"]["sync"] is False
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    with pytest.raises(httpx.ConnectError):  # the port is closed again
        async with httpx.AsyncClient(trust_env=False) as c:
            await c.get(f"http://127.0.0.1:{port}/")


BUSY = 'cannot listen on {}: address already in use; set [web] listen to another port or "off"'


@pytest.fixture
def busy_port() -> Any:
    """A port on 127.0.0.1 that another socket listens on."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        yield blocker.getsockname()[1]
    finally:
        blocker.close()


async def test_a_port_in_use_is_a_listen_error(busy_port: int) -> None:
    with pytest.raises(ListenError) as info, api.tcp_socket("127.0.0.1", busy_port):
        pass
    assert isinstance(info.value, ConfigError)  # exit code 2
    assert info.value.problems == [BUSY.format(f"127.0.0.1:{busy_port}")]


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (errno.EADDRINUSE, BUSY.format("[::1]:8080")),
        (
            errno.EACCES,
            'cannot listen on [::1]:8080: permission denied; set [web] listen to another port or "off"',
        ),
        (
            errno.EADDRNOTAVAIL,
            "cannot listen on [::1]:8080: this computer has no such address; "
            'set [web] listen to another address or "off"',
        ),
        (errno.ENOBUFS, None),
    ],
)
async def test_bind_problems(code: int, message: str | None) -> None:
    assert api.bind_problem("::1", 8080, OSError(code, "x")) == message


async def test_a_daemon_whose_port_is_in_use_stops_before_any_cycle(
    state: Path, short_dir: Path, busy_port: int
) -> None:
    daemon = Daemon(web_config(), short_dir, sync=False, listen=("127.0.0.1", busy_port), clock=lambda: NOW)
    with pytest.raises(ListenError):
        await asyncio.wait_for(daemon.run(asyncio.Event()), 5)
    assert not (short_dir / "calsync.sock").exists()  # the unix socket was closed and removed
