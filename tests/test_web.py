"""The web UI over TCP (``calsync.web``): key-link pairing, the cookie, Host and Origin checks.

Requests go through ``httpx.ASGITransport`` (from 127.0.0.1); the client's ``base_url`` sets the
``Host`` header. The TCP listener of the daemon is tested at the end, over a real socket.
"""

import asyncio
import errno
import hashlib
import hmac
import json
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
MAX_FAILURES = 10
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
    clock = Clock()
    c = client(state, clock=clock)
    auth.load_or_create_key(state)
    for _ in range(5):
        assert (await c.post("/pair", data={"key": "wrong"})).status_code == 403
    for _ in range(5):  # a wrong link counts too
        assert (await c.get("/", params={"k": "wrong"})).status_code == 403
    limited = await c.post("/pair", data={"key": "wrong"})
    assert limited.status_code == 429 and TOO_MANY in limited.text
    assert int(limited.headers["retry-after"]) == 600
    clock.now += timedelta(minutes=5)
    limited = await c.get("/", params={"k": "wrong"})
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) == 300  # not extended
    clock.now += timedelta(minutes=5, seconds=1)
    assert (await c.post("/pair", data={"key": "wrong"})).status_code == 403  # the window passed


async def test_the_right_key_pairs_whatever_the_rate_limit(state: Path) -> None:
    """A cross-site page can make a browser load ``/?k=wrong`` ten times (an image needs no
    Origin): that must not lock its owner out of pairing with the real key."""
    key = auth.load_or_create_key(state)
    c = client(state, clock=Clock())
    for _ in range(10):
        response = await c.get(
            "/",
            params={"k": "x"},
            headers={"sec-fetch-site": "cross-site", "referer": "https://evil.example/"},
        )
        assert response.status_code == 403
    assert (await c.get("/", params={"k": "x"})).status_code == 429
    paired = await c.get("/", params={"k": key})
    assert paired.status_code == 303 and paired.headers["set-cookie"].startswith("calsync_session=")
    assert (await c.post("/pair", data={"key": key})).status_code == 303


async def test_the_failure_table_is_bounded_and_cheap() -> None:
    import time

    from calsync.web.app import MAX_CLIENTS, Attempts

    clock = Clock()
    attempts = Attempts(clock)
    started = time.perf_counter()
    for i in range(5000):
        attempts.fail(f"10.0.{i // 256}.{i % 256}")
    elapsed = time.perf_counter() - started
    table = attempts._failures  # pyright: ignore[reportPrivateUsage]
    assert MAX_CLIENTS == 1024 and len(table) == 1024
    assert "10.0.0.0" not in table and "10.0.19.135" in table  # the oldest went first
    assert elapsed < 1.0, elapsed
    for _ in range(MAX_FAILURES):
        attempts.fail("10.9.9.9")
    assert attempts.retry_after("10.9.9.9") == 600
    assert len(table["10.9.9.9"]) == MAX_FAILURES
    clock.now += timedelta(minutes=11)
    attempts.fail("10.8.8.8")  # every other address is quiet again: pruned
    assert list(table) == ["10.8.8.8"]


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
    for path in ("/static/theme.js", "/static/pair.css", "/static/favicon.svg"):  # the pair page's
        response = await c.get(path)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-cache", path
    assert (await c.get("/static/nope.js")).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/static/index.html",
        "/static/pair.html",
        "/static/INDEX.HTML",  # APFS: any case names the same file
        "/static/Index.Html",
        "/static/./index.html",
        "/static/vendor/../index.html",
        "/static/%69ndex.html",
    ],
)
async def test_pages_are_not_served_from_static(state: Path, path: str) -> None:
    c = await paired(state)  # not even to a paired browser: the app page is served at /
    response = await c.get(path)
    assert response.status_code == 404, path
    assert "<html" not in response.text.lower()


async def test_html_responses_refuse_frames_and_set_a_csp(state: Path) -> None:
    key = auth.load_or_create_key(state)
    c = client(state, clock=Clock())
    pages = [await c.get("/"), await c.get("/", params={"k": "wrong"})]
    for _ in range(9):
        await c.post("/pair", data={"key": "wrong"})
    pages.append(await c.post("/pair", data={"key": "wrong"}))  # 429
    assert [p.status_code for p in pages] == [200, 403, 429]
    await c.get("/", params={"k": key})
    pages.append(await c.get("/"))  # the app page, paired
    assert '<div id="app">' in pages[-1].text
    for page in pages:
        assert page.headers["x-frame-options"] == "DENY"
        csp = [d.strip() for d in page.headers["content-security-policy"].split(";")]
        assert "frame-ancestors 'none'" in csp and "default-src 'self'" in csp
        assert "form-action 'self'" in csp and "base-uri 'none'" in csp and "object-src 'none'" in csp
        assert not any("unsafe" in d for d in csp)


async def test_every_response_says_nosniff(state: Path) -> None:
    c = await paired(state)
    responses = [
        await c.get("/api/status"),
        await c.post("/api/trigger"),  # no daemon: an error, still JSON
        await c.get("/api/nope"),
        await c.get("/"),
        await c.get("/static/app.js"),
        await c.get("/api/status", headers={"host": "evil.example.com"}),  # refused by the checks
        await client(state).get("/api/status"),  # unpaired
    ]
    assert [r.status_code for r in responses][-2:] == [400, 401]
    for response in responses:
        assert response.headers["x-content-type-options"] == "nosniff", response.url


async def test_the_api_socket_is_created_private(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bind itself makes the socket 0600 (no window before the chmod), and the umask is restored."""
    import os

    path = short_dir / "t.sock"
    old = os.umask(0o022)
    try:
        monkeypatch.setattr(os, "chmod", lambda *_, **__: None)  # leaves only the mode bind gave it
        with api.unix_socket(path):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(old)


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


KEY_WARNING = "the web UI's key file cannot be used (%s); replace it with `calsync web reset-key`"


@pytest.mark.parametrize(
    ("mode", "kind"), [(0o644, "InsecureKeyFile"), (0o200, "PermissionError"), (None, None), (0o600, None)]
)
async def test_the_daemon_warns_once_at_start_about_an_unusable_key(
    state: Path, short_dir: Path, caplog: pytest.LogCaptureFixture, mode: int | None, kind: str | None
) -> None:
    import shutil

    caplog.set_level(logging.INFO, logger="calsync.daemon")
    shutil.copy(state / "state.db", short_dir / "state.db")
    key = None
    if mode is not None:
        key = auth.load_or_create_key(short_dir)
        auth.key_path(short_dir).chmod(mode)
    stop = asyncio.Event()
    daemon = Daemon(web_config(), short_dir, sync=False, listen=("127.0.0.1", 0), clock=lambda: NOW)
    task = asyncio.create_task(daemon.run(stop))
    try:
        for _ in range(500):  # the start's lines are logged once the listener serves
            if any(r.getMessage().startswith("web UI on") for r in caplog.records) or task.done():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
        auth.key_path(short_dir).unlink(missing_ok=True)
    warnings = [r for r in caplog.records if r.name == "calsync.daemon" and r.levelno == logging.WARNING]
    expected = [KEY_WARNING % kind] if kind else []
    assert [r.getMessage() for r in warnings] == expected
    assert key is None or all(key not in r.getMessage() for r in caplog.records)


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


async def test_an_unknown_host_name_is_a_listen_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def unknown(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")

    monkeypatch.setattr(socket, "getaddrinfo", unknown)
    with pytest.raises(ListenError) as info, api.tcp_socket("nas.invalid", 8766):
        pass
    assert isinstance(info.value, ConfigError)  # exit code 2, like every other listen problem
    assert info.value.problems == [
        'cannot listen on nas.invalid:8766: no such host name; set [web] listen to another address or "off"'
    ]


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


# ----- operations over TCP ------------------------------------------------------------------

SAME_ORIGIN = {"origin": "http://127.0.0.1:8766"}


class OpsDaemon(FakeDaemon):
    """Records the operations it is asked to run, and their source."""

    def __init__(self) -> None:
        super().__init__()
        self.ops: list[tuple[str, dict[str, Any], bool, str]] = []

    async def run_op(self, name: str, args: dict[str, Any], dry_run: bool, *, source: str) -> Any:
        from calsync.ops import OpResult

        self.ops.append((name, args, dry_run, source))
        return OpResult(name, True, "done")


async def test_an_op_over_tcp_runs_with_source_ui(state: Path) -> None:
    daemon = OpsDaemon()
    c = await paired(state, daemon=daemon)
    response = await c.post("/api/ops/rule.pause", json={"args": {"name": "family"}}, headers=SAME_ORIGIN)
    assert response.status_code == 200 and response.json()["summary"] == "done"
    assert daemon.ops == [("rule.pause", {"name": "family"}, False, "ui")]


async def test_an_op_over_tcp_needs_the_cookie(state: Path) -> None:
    auth.load_or_create_key(state)
    daemon = OpsDaemon()
    c = client(state, daemon=daemon)
    response = await c.post("/api/ops/rule.pause", json={"args": {"name": "family"}}, headers=SAME_ORIGIN)
    assert response.status_code == 401 and response.json() == UNPAIRED
    assert daemon.ops == []


@pytest.mark.parametrize(
    "headers",
    [{"origin": "http://evil.example"}, {"origin": "http://127.0.0.1:9999"}, {"origin": "null"}, {}],
)
async def test_an_op_over_tcp_needs_this_origin(state: Path, headers: dict[str, str]) -> None:
    daemon = OpsDaemon()
    c = await paired(state, daemon=daemon)
    response = await c.post("/api/ops/rule.pause", json={"args": {"name": "family"}}, headers=headers)
    assert response.status_code == 403 and response.json()["error"]["kind"] == "cross_origin"
    assert daemon.ops == []


async def test_deleting_a_credential_over_tcp_needs_the_terminal(state: Path) -> None:
    daemon = OpsDaemon()
    c = await paired(state, daemon=daemon)
    args = {"name": "kid-dav", "delete_credential": True}
    response = await c.post("/api/ops/account.remove", json={"args": args}, headers=SAME_ORIGIN)
    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "kind": "needs_human",
            "message": "credentials are managed from the terminal",
            "needs_human": True,
            "command": "calsync account remove kid-dav --delete-credential",
            "reason": "credentials are managed from the terminal",
        }
    }
    assert daemon.ops == []
    kept = await c.post("/api/ops/account.remove", json={"args": {"name": "kid-dav"}}, headers=SAME_ORIGIN)
    assert kept.status_code == 200 and daemon.ops == [("account.remove", {"name": "kid-dav"}, False, "ui")]


async def test_a_large_op_body_is_413(state: Path) -> None:
    daemon = OpsDaemon()
    c = await paired(state, daemon=daemon)
    from calsync.api import MAX_BODY

    body = json.dumps({"args": {"name": "x" * MAX_BODY}})
    response = await c.post(
        "/api/ops/rule.pause",
        content=body,
        headers={**SAME_ORIGIN, "content-type": "application/json"},
    )
    assert response.status_code == 413 and response.json()["error"]["kind"] == "too_large"
    assert daemon.ops == []
    small = await c.post("/api/ops/rule.pause", json={"args": {"name": "x" * 1000}}, headers=SAME_ORIGIN)
    assert small.status_code == 200


async def test_ops_and_trigger_over_the_daemons_tcp_listener(short_dir: Path) -> None:
    """A real daemon: an operation over TCP is recorded with source ``ui``; ``trigger`` waits."""
    from calsync.views import StateReader
    from tests.test_daemon import eventually
    from tests.test_trigger import Running

    d = Running(short_dir, serve=True, listen=("127.0.0.1", 0))
    await d.start()
    await eventually(lambda: d.daemon.web_address is not None)
    assert d.daemon.web_address is not None
    port = d.daemon.web_address[1]
    key = auth.load_or_create_key(short_dir)
    origin = {"origin": f"http://127.0.0.1:{port}"}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as c:
        assert (await c.get("/", params={"k": key})).status_code == 303
        paused = await c.post("/api/ops/rule.pause", json={"args": {"name": "work"}}, headers=origin)
        waited = await c.post(
            "/api/ops/trigger", json={"args": {"rule": "family", "wait": True, "timeout": 5}}, headers=origin
        )
    assert paused.status_code == 200 and paused.json()["summary"] == 'rule "work" paused'
    with StateReader(short_dir / "state.db") as reader:
        assert [(c.source, c.summary) for c in reader.config_changes()] == [("ui", 'rule "work" paused')]
    assert waited.status_code == 200, waited.text
    data = waited.json()["data"]
    assert data["ran"] is True and list(data["rules"]) == ["family"]
    await d.stop()


# ----- what an operation's diff shows over TCP --------------------------------------------------

MASKED_ACCOUNTS = """\
[google]
client_secret_file = "unused.json"

[runtime]
interval = "30s"
jitter = 0.0

[accounts.me]
provider = "google"
credential = { backend = "env", ref = "UNUSED" }

[accounts.dav]
provider = "caldav"
server_url = "http://unused/"
username = "someone@example.com"
credential = { backend = "env", ref = "UNUSED" }

[notify]
bark_url = "https://api.day.app/DEVICEKEY"
failure_threshold = 3
"""


async def test_an_op_diff_over_tcp_is_masked_and_the_socket_sees_the_file(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/config`` masks the push URL's key and a username: a dry run's (or a write's)
    diff over TCP masks them the same way, while the CLI's socket shows the file."""
    from calsync import credentials
    from tests.test_daemon import eventually
    from tests.test_daemon_reload import FAMILY
    from tests.test_ops import FakeKeyring
    from tests.test_trigger import Running, uds

    keychain = FakeKeyring()  # no entry for the new account: the web UI may add it
    monkeypatch.setattr(credentials, "_keyring", lambda: keychain)
    d = Running(short_dir, MASKED_ACCOUNTS + FAMILY, serve=True, listen=("127.0.0.1", 0))
    await d.start()
    await eventually(lambda: d.daemon.web_address is not None and (short_dir / "calsync.sock").is_socket())
    assert d.daemon.web_address is not None
    port = d.daemon.web_address[1]
    key = auth.load_or_create_key(short_dir)
    origin = {"origin": f"http://127.0.0.1:{port}"}
    threshold = {"args": {"key": "notify.failure_threshold", "value": "5"}, "dry_run": True}
    added = {
        "args": {
            "name": "new",
            "provider": "caldav",
            "server_url": "https://dav.example.com/principals/SECRETPATH/",
            "username": "another@example.com",
        },
        "dry_run": True,
    }
    secrets = ("DEVICEKEY", "someone@example.com", "another@example.com", "SECRETPATH")
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as c:
        assert (await c.get("/", params={"k": key})).status_code == 303
        tcp = [
            await c.post("/api/ops/config.set", json=threshold, headers=origin),
            await c.post("/api/ops/account.add", json=added, headers=origin),
        ]
        written = await c.post("/api/ops/account.add", json={"args": added["args"]}, headers=origin)
    async with uds(short_dir) as s:
        sock = [
            await s.post("/api/ops/config.set", json=threshold),
            await s.post("/api/ops/account.add", json={**added, "args": {**added["args"], "name": "newer"}}),
        ]
    await d.stop()
    for response in [*tcp, written, *sock]:
        assert response.status_code == 200, response.text
    set_diff, add_diff = (r.json()["diff"] for r in tcp)
    assert "-failure_threshold = 3\n+failure_threshold = 5\n" in set_diff
    assert ' bark_url = "https://api.day.app"\n' in set_diff  # context, cut to scheme and host
    assert '+username = "(hidden)"\n' in add_diff and '+server_url = "https://dav.example.com"\n' in add_diff
    assert '+credential = {backend = "keychain", ref = "(hidden)"}\n' in add_diff
    assert ' username = "(hidden)"\n' in add_diff  # the existing account, in the context
    for text in (*(r.text for r in tcp), written.text):
        for secret in secrets:
            assert secret not in text
    assert written.json()["changed"] is True
    assert "another@example.com" in (short_dir / "config.toml").read_text()  # the file has it all
    sock_set, sock_add = (r.json()["diff"] for r in sock)
    assert "DEVICEKEY" in sock_set
    assert "another@example.com" in sock_add and "SECRETPATH" in sock_add


async def test_adding_an_account_over_tcp_never_re_points_a_stored_credential(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keychain entry left by an account removed without ``--delete-credential``: a browser
    adding an account of that name could point the stored password at a server it chose."""
    from calsync import credentials
    from tests.test_ops import FakeKeyring

    keychain = FakeKeyring()
    keychain.store[("calsync:old-dav", credentials.KEYRING_USER)] = "app-password"
    monkeypatch.setattr(credentials, "_keyring", lambda: keychain)
    daemon = OpsDaemon()
    c = await paired(state, daemon=daemon)
    args = {"name": "old-dav", "provider": "caldav", "server_url": "https://evil.example/", "username": "a b"}
    response = await c.post("/api/ops/account.add", json={"args": args}, headers=SAME_ORIGIN)
    assert response.status_code == 403
    reason = "the keychain already holds a credential for this account; add it from the terminal"
    assert response.json() == {
        "error": {
            "kind": "needs_human",
            "message": reason,
            "needs_human": True,
            "command": "calsync account add old-dav --provider caldav --server-url https://evil.example/ "
            "--username 'a b'",
            "reason": reason,
        }
    }
    dry = await c.post("/api/ops/account.add", json={"args": args, "dry_run": True}, headers=SAME_ORIGIN)
    assert dry.status_code == 403
    assert daemon.ops == []
    assert "app-password" not in response.text
    fresh = {"name": "new-dav", "provider": "caldav"}
    allowed = await c.post("/api/ops/account.add", json={"args": fresh}, headers=SAME_ORIGIN)
    assert allowed.status_code == 200 and daemon.ops == [("account.add", fresh, False, "ui")]


async def test_adding_an_account_over_tcp_when_the_keychain_cannot_be_asked(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyring.errors

    from calsync import credentials

    class Locked:
        def get_password(self, service: str, user: str) -> str | None:
            raise keyring.errors.KeyringLocked("locked")

    monkeypatch.setattr(credentials, "_keyring", lambda: Locked())
    daemon = OpsDaemon()
    c = await paired(state, daemon=daemon)
    args = {"name": "dav", "provider": "caldav"}
    response = await c.post("/api/ops/account.add", json={"args": args}, headers=SAME_ORIGIN)
    assert response.status_code == 403
    assert (
        response.json()["error"]["reason"]
        == "the keychain could not be checked; add the account from the terminal"
    )
    assert response.json()["error"]["command"] == "calsync account add dav --provider caldav"
    assert daemon.ops == []
