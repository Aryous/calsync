"""A real calsync daemon, in this process, for the browser tests.

``LiveDaemon`` runs ``calsync.daemon.Daemon`` in a background thread with its own asyncio loop:
the real scheduler, operations, local API and web UI (TCP on ``127.0.0.1:<free port>``), with
fake accounts behind them:

- two Google accounts (``personal``: 家庭日程, 孩子学校, 中国节假日 (no rule uses it); ``work``:
  工作), each a ``FakeGoogle`` reached through ``httpx.MockTransport``;
- two iCloud accounts (``family-icloud``: 家庭; ``my-icloud``: 我的日历), each a ``FakeTarget``;
- four rules, as in the concept: ``family-home`` 家庭日程 → 家庭, ``school-home`` 孩子学校 → 家庭,
  ``family-mine`` 家庭日程 → 我的日历 (busy only), ``work-mine`` 工作 → 我的日历;
- events in the week of ``NOW`` (Monday 2026-10-26 10:42 in Asia/Shanghai): weekly series (one
  with a moved instance), all-day events, timed events with a location, and one change pending
  delivery (``PENDING_TITLE``: delivered to 家庭 by ``family-home``, not yet to 我的日历 by
  ``family-mine``).

Time: the daemon's clock is ``LiveClock``, pinned to ``NOW`` (``advance`` moves it). The schedule
waits with the real ``asyncio.sleep``, but with a pinned clock and ``interval = "1h"`` no timed
cycle comes during a test run: cycles run at start and when requested (``立即同步``, ops). The
config file is watched as in production (every 2 s, real time).

Nothing here reaches the network: the Google fakes answer through ``MockTransport``, the targets
are in memory, and the web UI listens on loopback only. ``seal_session`` gives the daemon the
sealed environment's environment part (temporary ``HOME``, failing keyring, no proxy) for the
whole session; the network guard of ``tests.conftest.sealed`` applies during each test.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest

from calsync import configfile
from calsync.config import Config
from calsync.daemon import Daemon
from calsync.http import RequestLog, make_client
from calsync.ops import OpResult
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import StaticToken
from calsync.web import auth
from calsync.wiring import Adapters
from tests.conftest import Seal, SealedKeyring
from tests.factories import google_event
from tests.fakes.google import BASE_URL, FakeGoogle
from tests.fakes.target import FakeTarget

T = TypeVar("T")

ZONE = "Asia/Shanghai"
OFFSET = "+08:00"
# Monday 2026-10-26, 10:42 local (02:42 UTC): the concept's "now".
NOW = datetime(2026, 10, 26, 2, 42, tzinfo=UTC)

FAMILY = "family@group.calendar.google.com"
SCHOOL = "school@group.calendar.google.com"
HOLIDAYS = "zh.china#holiday@group.v.calendar.google.com"
WORK = "work@group.calendar.google.com"

PENDING_TITLE = "和朋友吃午饭"

CONFIG = """\
[google]
client_secret_file = "unused.json"

[runtime]
interval = "1h"
jitter = 0.0

[accounts.personal]
provider = "google"
credential = { backend = "env", ref = "UNUSED" }

[accounts.work]
provider = "google"
credential = { backend = "env", ref = "UNUSED" }

[accounts.family-icloud]
provider = "caldav"
server_url = "icloud"
username = "family@example.com"
credential = { backend = "env", ref = "UNUSED" }

[accounts.my-icloud]
provider = "caldav"
server_url = "icloud"
username = "me@example.com"
credential = { backend = "env", ref = "UNUSED" }

[[rules]]
name = "family-home"
source = { account = "personal", calendar = "家庭日程" }
target = { account = "family-icloud", calendar = "家庭" }

[[rules]]
name = "school-home"
source = { account = "personal", calendar = "孩子学校" }
target = { account = "family-icloud", calendar = "家庭" }

[[rules]]
name = "family-mine"
source = { account = "personal", calendar = "家庭日程" }
target = { account = "my-icloud", calendar = "我的日历" }
fields = { content = "busy" }

[[rules]]
name = "work-mine"
source = { account = "work", calendar = "工作" }
target = { account = "my-icloud", calendar = "我的日历" }
"""


def _at(day: str, hm: str) -> str:
    return f"{day}T{hm}:00{OFFSET}"


def _timed(eid: str, title: str, day: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
    return google_event(eid, title, _at(day, start), _at(day, end), tz=ZONE, **extra)


def _all_day(eid: str, title: str, day: str, next_day: str) -> dict[str, Any]:
    return {
        "id": eid,
        "status": "confirmed",
        "summary": title,
        "start": {"date": day},
        "end": {"date": next_day},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 540}]},
    }


def seed_personal(fake: FakeGoogle) -> None:
    fake.add_calendar(FAMILY, "家庭日程")
    fake.add_calendar(SCHOOL, "孩子学校")
    fake.add_calendar(HOLIDAYS, "中国节假日", access_role="reader")
    # 晨跑: Mondays, Wednesdays and Fridays since 5 October; this Wednesday's run is half an hour later.
    fake.put_event(
        FAMILY,
        _timed(
            "run", "晨跑", "2026-10-05", "07:00", "07:45", recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"]
        ),
    )
    fake.put_event(
        FAMILY,
        _timed(
            "run_20261027T230000Z",
            "晨跑",
            "2026-10-28",
            "07:30",
            "08:15",
            recurringEventId="run",
            originalStartTime={"dateTime": _at("2026-10-28", "07:00"), "timeZone": ZONE},
        ),
    )
    fake.put_event(FAMILY, _timed("call", "家庭视频通话", "2026-10-26", "19:30", "20:15"))
    fake.put_event(FAMILY, _timed("dentist", "社区活动", "2026-10-27", "14:00", "15:00", location="社区中心"))
    fake.put_event(FAMILY, _all_day("fees", "交物业费", "2026-10-29", "2026-10-30"))
    fake.put_event(FAMILY, _timed("dinner", "家庭聚餐", "2026-10-30", "19:00", "21:00", location="外婆家"))
    fake.put_event(
        SCHOOL,
        _timed(
            "piano",
            "钢琴课",
            "2026-10-06",
            "18:30",
            "19:30",
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=TU,TH"],
            location="少年宫",
        ),
    )
    fake.put_event(SCHOOL, _timed("meeting", "家长会", "2026-10-28", "17:00", "18:00"))
    fake.put_event(SCHOOL, _all_day("outing", "秋游", "2026-10-31", "2026-11-01"))


def seed_work(fake: FakeGoogle) -> None:
    fake.add_calendar(WORK, "工作")
    fake.put_event(
        WORK,
        _timed("weekly", "周会", "2026-10-05", "09:30", "10:30", recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"]),
    )
    fake.put_event(WORK, _timed("review", "设计评审", "2026-10-27", "10:00", "11:30", location="3 号会议室"))
    fake.put_event(WORK, _timed("client", "客户电话", "2026-10-28", "14:00", "15:00"))


# The seeded accounts without a single line (the map's empty state).
CONFIG_WITHOUT_LINES = CONFIG.split("[[rules]]", 1)[0]


def pending_event() -> dict[str, Any]:
    return _timed("lunch", PENDING_TITLE, "2026-10-26", "12:30", "13:30", location="国贸")


@dataclass
class LiveClock:
    """The daemon's clock: pinned to ``now`` until a test moves it."""

    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass
class World:
    """The fake accounts: one ``FakeGoogle`` per Google account, one ``FakeTarget`` per CalDAV
    account. Their state outlives adapter rebuilds."""

    google: dict[str, FakeGoogle] = field(default_factory=dict)
    targets: dict[str, FakeTarget] = field(default_factory=dict)
    # While set and not open, every Google request waits for it (``LiveDaemon.hold_cycles``): a
    # cycle stays in progress, and the daemon's loop (its web UI too) keeps running meanwhile.
    gate: asyncio.Event | None = None
    # Google accounts whose every request is answered 401 (``LiveDaemon.refuse``): their rules'
    # next runs fail on the credential, as when a token was revoked.
    refused: set[str] = field(default_factory=set)

    @classmethod
    def seeded(cls) -> World:
        personal, work = FakeGoogle(), FakeGoogle()
        seed_personal(personal)
        seed_work(work)
        home, mine = FakeTarget(), FakeTarget()
        home.add_calendar("家庭")
        mine.add_calendar("我的日历")
        return cls(
            google={"personal": personal, "work": work}, targets={"family-icloud": home, "my-icloud": mine}
        )

    async def factory(self, config: Config, names: set[str], *, log: RequestLog | None = None) -> Adapters:
        adapters = Adapters(log or RequestLog())
        for name in sorted(names):
            account = config.accounts[name]
            if account.provider == "caldav":
                adapters.targets[name] = self.targets.setdefault(name, FakeTarget())
                continue
            fake = self.google.setdefault(name, FakeGoogle())
            client = make_client(adapters.log, transport=httpx.MockTransport(self._gated(name, fake)))
            adapters.clients.append(client)
            adapters.sources[name] = GoogleSource(client, StaticToken("t"), base_url=BASE_URL)
        return adapters

    def _gated(
        self, name: str, fake: FakeGoogle
    ) -> Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]:
        async def handler(request: httpx.Request) -> httpx.Response:
            if self.gate is not None:
                await self.gate.wait()
            if name in self.refused:
                return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
            return fake.handler(request)

        return handler


@contextlib.contextmanager
def seal_session(root: Path) -> Iterator[Seal]:
    """The environment part of ``tests.conftest.sealed``, for a session-scoped daemon (which no
    function-scoped fixture can serve): a temporary ``HOME`` and ``CALSYNC_*``, a keyring that
    refuses every call, no proxy, and the local zone ``Asia/Shanghai``. Refusals fail the session's
    teardown."""
    from calsync import credentials

    seal = Seal()
    home = root / "home"
    home.mkdir()
    saved_tz = os.environ.get("TZ")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        mp.setenv("CALSYNC_CONFIG", str(home / "config.toml"))
        mp.setenv("CALSYNC_STATE_DIR", str(home / "state"))
        mp.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
        mp.setattr(credentials, "_keyring", lambda: SealedKeyring(seal))
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            mp.delenv(name, raising=False)
        mp.setenv("NO_PROXY", "*")
        mp.setenv("no_proxy", "*")
        os.environ["TZ"] = ZONE
        time.tzset()
        try:
            yield seal
        finally:
            if saved_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved_tz
            time.tzset()
    if seal.refused:
        pytest.fail("the sealed environment refused: " + "; ".join(seal.refused), pytrace=False)


class LiveDaemon:
    """A running daemon over a synthetic config, its fake accounts and its web UI.

    ``start()`` returns once the web UI answers, the first cycle has run and the pending change
    is staged. The first cycle that runs every rule (``立即同步``) delivers that change: tests that
    trigger cycles or change the config should use their own daemon (the ``fresh_live``
    fixture), so the session's stays as seeded.
    """

    def __init__(self, root: Path, *, config: str = CONFIG, now: datetime = NOW, stage: bool = True) -> None:
        self.root = root
        self.stage = stage  # stage the pending change (needs the seeded rules)
        self.state_dir = root  # short (under /tmp): the API socket's path must fit 104 bytes
        self.config_path = root / "config.toml"
        self.config_path.write_text(config, encoding="utf-8")
        self.clock = LiveClock(now)
        self.world = World.seeded()
        self.key = auth.load_or_create_key(self.state_dir)
        parsed = configfile.validate_text(config, root)
        self.daemon = Daemon(
            parsed,
            self.state_dir,
            listen=("127.0.0.1", 0),
            clock=self.clock,
            rng=lambda: 0.5,
            adapters_factory=self.world.factory,
            config_path=self.config_path,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._started = threading.Event()

    # ----- lifecycle --------------------------------------------------------------------
    def start(self, timeout: float = 20) -> LiveDaemon:
        self._thread = threading.Thread(target=self._main, name="calsync-live-daemon", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            raise RuntimeError("the daemon's loop did not start")
        # The first cycle runs the seeded lines; a config without lines has none to wait for.
        first = 1 if self.stage else 0
        self._until(lambda: self.daemon.web_address is not None and self.cycles() >= first, timeout)
        self._until(self._answers, timeout)
        if self.stage:
            self._stage_pending()
        return self

    def _main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._stop = asyncio.Event()
        self._started.set()
        try:
            loop.run_until_complete(self.daemon.run(self._stop))
        except BaseException as exc:  # reported by the next wait or stop
            self._failure = exc
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def hold_cycles(self) -> None:
        """From now on a cycle waits at its first Google request until ``release_cycles``. Holding
        again while held keeps the one gate (a new one would strand the requests on the old)."""

        def close() -> None:
            if self.world.gate is None:
                self.world.gate = asyncio.Event()

        self.in_loop(close)

    def release_cycles(self) -> None:
        def open_gate() -> None:
            if self.world.gate is not None:
                self.world.gate.set()
                self.world.gate = None

        if self._loop is not None and not self._loop.is_closed():
            self.in_loop(open_gate)

    def stop(self, timeout: float = 15) -> None:
        with contextlib.suppress(RuntimeError, TimeoutError):  # a held cycle must not block the stop
            self.release_cycles()
        if self._loop is not None and self._stop is not None and not self._loop.is_closed():
            with contextlib.suppress(RuntimeError):  # the loop closed meanwhile
                self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError("the daemon did not stop")
        if self._failure is not None:
            raise RuntimeError("the daemon failed") from self._failure

    def _until(self, condition: Callable[[], bool], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if self._failure is not None:
                raise RuntimeError("the daemon failed") from self._failure
            if time.monotonic() > deadline:
                raise TimeoutError("the daemon did not get ready")
            time.sleep(0.02)

    def _answers(self) -> bool:
        try:
            return httpx.get(f"{self.url}/static/favicon.svg", timeout=2, trust_env=False).status_code == 200
        except httpx.TransportError:
            return False

    def _stage_pending(self) -> None:
        """One change pending delivery: a new event in 家庭日程, delivered by ``family-home`` only,
        so ``family-mine`` still owes it to 我的日历."""
        self.in_loop(lambda: self.world.google["personal"].put_event(FAMILY, pending_event()))
        result = self.op("trigger", {"rule": "family-home", "wait": True, "timeout": 30})
        assert result.data.get("ran") is True, result.to_json()

    # ----- staging what needs a person -------------------------------------------------
    def trigger(self, rule: str) -> dict[str, Any]:
        """One cycle of ``rule`` (waited for); the trigger's data."""
        result = self.op("trigger", {"rule": rule, "wait": True, "timeout": 30})
        assert result.data.get("ran") is True, result.to_json()
        return result.data

    def pause(self, rule: str) -> None:
        result = self.op("rule.pause", {"name": rule})
        assert result.changed, result.to_json()

    def stage_auth(self, account: str = "work", rule: str = "work-mine") -> None:
        """``account``'s Google credential stops working (every request 401) and ``rule`` runs on
        it: the rule's health is ``auth``, an ``auth`` attention names the account."""
        self.in_loop(lambda: self.world.refused.add(account))
        self.trigger(rule)

    def stage_blocked(self) -> None:
        """Every 孩子学校 event is cancelled in Google and ``school-home`` runs: deleting all three
        copies is over the delete guard (max_ratio 0.5), so the run is ``blocked``."""

        def cancel() -> None:
            google = self.world.google["personal"]
            for event_id in ("piano", "meeting", "outing"):
                google.cancel_event(SCHOOL, event_id)

        self.in_loop(cancel)
        assert self.trigger("school-home")["rules"]["school-home"]["status"] == "blocked"

    def stage_failing(
        self, rule: str = "family-mine", account: str = "my-icloud", calendar: str = "我的日历"
    ) -> None:
        """The target calendar fails once (a 503-like error) while ``rule`` runs: ``failing``."""
        from calsync.errors import TransientError

        def fail() -> None:
            self.world.targets[account].fail_changes[calendar] = TransientError("CalDAV 503 (staged)")

        self.in_loop(fail)
        assert self.trigger(rule)["rules"][rule]["status"] == "failed"

    # ----- access -----------------------------------------------------------------------
    @property
    def url(self) -> str:
        address = self.daemon.web_address
        assert address is not None, "the daemon is not serving"
        return f"http://127.0.0.1:{address[1]}"

    @property
    def pair_url(self) -> str:
        return f"{self.url}/?k={self.key}"

    def cycles(self) -> int:
        return self.daemon.snapshot().cycles

    def run(self, coro: Coroutine[Any, Any, T], timeout: float = 60) -> T:
        """Run ``coro`` on the daemon's loop and return its result."""
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def in_loop(self, fn: Callable[[], T], timeout: float = 10) -> T:
        """Call ``fn`` on the daemon's loop thread (changes to the fakes, between its awaits)."""

        async def call() -> T:
            return fn()

        return self.run(call(), timeout)

    def op(self, name: str, args: dict[str, Any] | None = None, *, dry_run: bool = False) -> OpResult:
        """An operation, as the CLI would post it (source ``cli``)."""
        return self.run(self.daemon.run_op(name, args or {}, dry_run, source="cli"))

    def get(self, path: str, **params: Any) -> Any:
        """``GET /api/...`` as a paired browser, the JSON body."""
        cookies = {auth.COOKIE: auth.session_token(self.key)}
        with httpx.Client(base_url=self.url, cookies=cookies, trust_env=False, timeout=10) as client:
            response = client.get(path, params=params)
        response.raise_for_status()
        return response.json()


@contextlib.contextmanager
def live_daemon(**kwargs: Any) -> Iterator[LiveDaemon]:
    """A started ``LiveDaemon`` in a fresh short directory, stopped and removed afterwards."""
    root = Path(tempfile.mkdtemp(prefix="calsync-live-", dir="/tmp"))
    live = LiveDaemon(root, **kwargs)
    try:
        live.start()
        yield live
    finally:
        try:
            live.stop()
        finally:
            shutil.rmtree(root, ignore_errors=True)
