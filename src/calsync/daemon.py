"""`calsync daemon`: sync cycles on a jittered schedule in one long-lived process.

With sync, the daemon holds the run lock for its whole life, keeps one ``Store``, one set of
adapters and one ``Engine`` (whose in-memory mirrors and target caches then carry from cycle to
cycle), and runs a cycle at start, then after every wait of ``interval × (1 ± jitter)``.

- ``request_cycle()`` cuts the wait short. Requests made during a cycle collapse into one
  follow-up cycle; a request names one rule or means every rule. A cycle for named rules keeps
  the timer's deadline; a timed cycle or one for every rule restarts it. With ``wait`` it also
  returns a future of the first cycle that starts after the request and runs the rule (every
  unpaused rule, for a request of every rule): a request made during a cycle is answered by the
  follow-up. Once the scheduler has stopped, a request is refused (``DaemonStopping``).
- A rule that keeps failing is skipped for ``BACKOFF`` minutes after its latest failure (by
  the number of failures in a row; a blocked result neither adds to nor resets the count). A
  requested rule runs regardless; a skipped rule records no run.
- A rule that failed for authentication, or because an account could not be set up for a
  config reason (a credential file's mode, a missing client secret file), gets its accounts'
  adapters rebuilt before its next attempt, which picks up a credential stored meanwhile by
  ``calsync auth`` or a file fixed meanwhile.

Beside the scheduler, the daemon serves the local API (``calsync.api``) on
``<state_dir>/calsync.sock`` and, given ``listen``, the web UI (``calsync.web.app``) over TCP,
with and without sync. Each service catches its own failure, logs it and sets ``stop``:
a failing server never cancels a cycle in progress. Once every service has ended (the cycle
finished), ``run`` raises the first failure.

Operations (``calsync.ops``) posted to the socket run inside the daemon, under ``_cycle_lock``:
never during a cycle, and the store keeps one writer.

Hot reload: the daemon remembers the config file it runs (its ``Snapshot``). Between cycles it
checks the file every ``WATCH_INTERVAL`` seconds (a ``stat`` first; the bytes are read only when
size or mtime changed) and once more right before each cycle. An edit made outside calsync is
validated: an invalid file keeps the running config (logged once per text, counts only); a
valid one is applied and recorded as a ``config_change`` with source ``external``. A config an
operation writes is applied when the operation returns and is never seen as external. Applying
swaps the rules, rebuilds or closes only the accounts whose definition changed, and hands the
running engine the new config (``Engine.replace_config``), all under ``_cycle_lock``. A changed
``[web]`` address needs a restart (logged); ``allowed_hosts`` take effect at once.

Approving blocked deletes (``ops`` ``approve``): the approval (rule -> fingerprint of the blocked
set) is kept in memory for the rule's next cycle, which is requested at once and consumes it
whatever its outcome; the engine lets through only a set with that fingerprint. An operation
that waits for that cycle (``wait``) waits after the lock is released, so the cycle can run.
Waiters (``approve`` and ``trigger`` with ``wait``) are answered by the first cycle that starts
after they were registered and includes their rule (``_record``; None when that cycle failed),
and refused when the rule is removed or paused first, or when the daemon stops. ``trigger``
never takes ``_cycle_lock``: it only requests and waits.

Paused rules never run (``request_cycle`` refuses them with ``RulePaused``). A retiring rule
(``retire = true``) deletes its copies (the delete guard applies) and never creates its target
calendar; after a cycle in which it ended ``ok`` with no copy left (and detection read every
changed copy), the daemon removes it from the config (``source="daemon"``).

A reload that fails for an unexpected reason (a bug) is contained: logged by type, the running
config stays, the scheduler goes on.

The calendar-list cache (``account_calendar``, for ``calsync calendars`` and the GUI's map) is
refreshed for every configured account at start, before the first cycle, and then every
``CALENDAR_REFRESH``, between cycles under ``_cycle_lock``; an account no rule uses gets a
temporary adapter. A failing account keeps its old rows (logged by type). Operations that talk to
the accounts (``calendars.refresh``, ``rule.preview``) borrow the daemon's adapters the same way.
Everything that waits on the network under ``_cycle_lock`` outside a cycle is bounded
(``ops.REFRESH_TIMEOUT``, ``ops.PREVIEW_TIMEOUT``), so a server that never answers cannot stop the
sync.

Without sync (``--no-sync``) the daemon takes no lock, reads no credential, opens no store and
runs no cycle: it only serves the API (and sends ``notify.test``, which needs only the
config). It cannot run beside another daemon in the same state directory (that one's socket
is live: exit 4), only beside ``calsync run``; to show a running
daemon's data, point it at a copy of the state directory.
"""

import asyncio
import contextlib
import logging
import os
import random
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, overload

from calsync import api, debug, notify, ops, wiring
from calsync.config import Config
from calsync.configfile import Snapshot, read_snapshot, validate_text
from calsync.cycle import counts_line, remove_retired, retired, run_cycle
from calsync.engine import Approval, CycleResult, Engine, RuleResult
from calsync.errors import CalsyncError, ConfigError, RulePaused
from calsync.http import RequestLog
from calsync.lock import RunLock
from calsync.store import Store
from calsync.web import auth as web_auth
from calsync.web.app import create_web_app

log = logging.getLogger(__name__)

# Minutes a rule is skipped after 1, 2, 3, 4 and 5 or more failures in a row.
BACKOFF = (1, 2, 4, 8, 15)
BACKOFF_CAP = timedelta(minutes=15)
# Seconds between two checks of the config file while the daemon waits for the next cycle.
WATCH_INTERVAL = 2.0
EXTERNAL_SUMMARY = "edited outside calsync"
# How often the daemon fetches every account's calendar list into the cache.
CALENDAR_REFRESH = timedelta(hours=6)

Sleep = Callable[[float], Awaitable[None]]


class AdaptersFactory(Protocol):
    """``wiring.build_adapters``: adapters for ``names``, recording requests in ``log``."""

    def __call__(
        self, config: Config, names: set[str], *, log: RequestLog | None = None
    ) -> Awaitable[wiring.Adapters]: ...


def backoff_for(failures: int) -> timedelta:
    """How long a rule is skipped after ``failures`` (≥ 1) failures in a row."""
    return min(timedelta(minutes=BACKOFF[min(failures, len(BACKOFF)) - 1]), BACKOFF_CAP)


def _counts_failure(result: RuleResult) -> bool:
    """A result that backs the rule off: failed, or partial with failed items (not blocked)."""
    return result.status == "failed" or (result.status == "partial" and result.failed > 0)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DaemonInfo:
    running: bool
    sync: bool
    pid: int
    started_at: datetime
    last_cycle_at: datetime | None
    next_cycle_at: datetime | None
    cycles: int
    cycle_started_at: datetime | None = None  # while a cycle runs: when it started (the GUI animates it)

    def to_json(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "running": self.running,
            "sync": self.sync,
            "pid": self.pid,
            "started_at": self.started_at.isoformat(),
            "last_cycle_at": iso(self.last_cycle_at),
            "next_cycle_at": iso(self.next_cycle_at),
            "cycles": self.cycles,
            "cycle_started_at": iso(self.cycle_started_at),
        }


class Daemon:
    def __init__(
        self,
        config: Config,
        state_dir: Path,
        *,
        sync: bool = True,
        serve: bool = True,
        listen: tuple[str, int] | None = None,
        clock: Callable[[], datetime] = _now,
        sleep: Sleep = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        adapters_factory: AdaptersFactory | None = None,
        config_path: Path | None = None,
    ) -> None:
        self._config = config
        self._config_path = config_path  # the file operations edit and the daemon watches; None: neither
        # The file the running config came from, read when sync starts; what the watcher
        # compares with. ``_stat``: its (size, mtime_ns) when last checked (None: missing).
        self._snapshot: Snapshot | None = None
        self._stat: tuple[int, int] | None = None
        self._rejected: str | None = None  # fingerprint of an invalid edit, logged once
        self._written: Snapshot | None = None  # written by an operation, applied when it returns
        self._before_written: Snapshot | None = None  # ``_snapshot`` before that write
        self._state_dir = state_dir
        self._db_path, self._lock_path = wiring.state_paths(state_dir)
        self._sync = sync
        self._serve_api = serve  # False: no socket and no TCP (tests of the schedule)
        self._listen = listen  # the web UI's (host, port); None: no TCP listener
        self.web_address: tuple[str, int] | None = None  # bound (host, port), while serving
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._factory = adapters_factory  # None: wiring.build_adapters, looked up when used
        self._rules = [r.name for r in config.rules]
        self._started_at = clock()
        self._running = False
        self._cycles = 0
        self._last_cycle_at: datetime | None = None
        self._next_cycle_at: datetime | None = None
        self._cycle_started_at: datetime | None = None
        # Requests not yet taken by a cycle; ``_wake`` is set while there are any.
        self._wake = asyncio.Event()
        self._pending_all = False
        self._pending: set[str] = set()
        self._failures: dict[str, tuple[int, datetime]] = {}  # rule -> (failures in a row, last)
        self._reauth: set[str] = set()  # rules whose accounts are rebuilt before their next attempt
        # One log for every adapters build, cleared each cycle; ``_adapters`` merges the
        # per-account builds in ``_parts`` (the engine holds its dicts, so swaps reach it).
        self._log = RequestLog()
        self._adapters = wiring.Adapters(self._log)
        self._parts: dict[str, wiring.Adapters] = {}
        self._store: Store | None = None
        self._engine: Engine | None = None
        # The one mutual-exclusion point: held by a whole cycle (recording included) and by
        # every operation, so nothing changes the config, the adapters or the store under a
        # running cycle, and the store has one writer.
        self._cycle_lock = asyncio.Lock()
        # Approved blocked sets (rule -> fingerprint), consumed by the rule's next cycle; and
        # the operations waiting for the next cycle of some rules (None: of any rule).
        self._approvals: dict[str, Approval] = {}
        self._scheduling = False  # the scheduler runs: something will run the cycles requested
        self._scheduled = False  # the scheduler has ended: nothing will run a cycle any more
        self._calendars_due: datetime | None = None  # next calendar-list refresh; None: now (at start)
        # Cycles started so far (a cycle's number is taken under ``_cycle_lock`` as it starts); a
        # waiter is answered only by a cycle numbered above the count when it was registered.
        self._started_cycles = 0
        self._waiters: list[_Waiter] = []
        # Pushes in flight (``notify``): sent in the background, drained when the daemon stops.
        self._outbox = notify.Outbox()
        self._last_test_push: float | None = None  # time.monotonic() of the last notify.test

    # ----- interface ----------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        """Run until ``stop`` is set; a cycle in progress finishes first.

        The services, the scheduler (with sync) and the API server beside it, run as tasks of
        one group and end when ``stop`` is set. A failing service sets ``stop`` instead of
        cancelling the others; its error is raised once they have all ended.
        """
        self._running = True
        failures: list[BaseException] = []
        try:
            async with self._resources() as (sock, tcp), asyncio.TaskGroup() as group:
                if self._sync:
                    group.create_task(self._service("scheduler", self._scheduler(stop), stop, failures))
                if sock is not None:
                    group.create_task(self._service("api server", self._serve(sock, stop), stop, failures))
                if tcp is not None:
                    group.create_task(self._service("web server", self._serve_web(tcp, stop), stop, failures))
                group.create_task(stop.wait())
        finally:
            self._running = False
            self.web_address = None
            await self._outbox.drain()
        if failures:
            raise failures[0]

    @staticmethod
    async def _service(
        name: str, work: Coroutine[Any, Any, None], stop: asyncio.Event, failures: list[BaseException]
    ) -> None:
        try:
            await work
        except (Exception, SystemExit) as exc:  # SystemExit too: nothing may cancel a cycle mid-write
            # The type only: an error's text can carry a request URL.
            log.error("the %s failed (%s); stopping", name, type(exc).__name__)
            debug.details(log, f"the {name} failed", exc)
            failures.append(exc)
            stop.set()

    async def _serve(self, sock: socket.socket, stop: asyncio.Event) -> None:
        app = api.create_app(
            config_provider=lambda: self._config, state_dir=self._state_dir, daemon=self, clock=self._clock
        )
        await api.serve(app, sock, stop)

    async def _serve_web(self, sock: socket.socket, stop: asyncio.Event) -> None:
        assert self._listen is not None
        app = create_web_app(
            config_provider=lambda: self._config,
            state_dir=self._state_dir,
            daemon=self,
            listen_host=self._listen[0],
            clock=self._clock,
        )
        host, port = self._listen[0], sock.getsockname()[1]
        shown = f"[{host}]" if ":" in host else host
        log.info("web UI on http://%s:%d/ (pair a browser: calsync web pair)", shown, port)  # no key
        _check_web_key(self._state_dir)
        await api.serve(app, sock, stop)

    @overload
    def request_cycle(self, rule: str | None = None, *, wait: Literal[False] = False) -> bool: ...

    @overload
    def request_cycle(
        self, rule: str | None = None, *, wait: Literal[True]
    ) -> "asyncio.Future[CycleResult | None]": ...

    def request_cycle(
        self, rule: str | None = None, *, wait: bool = False
    ) -> "bool | asyncio.Future[CycleResult | None]":
        """Run a cycle now (after the current one, if one is running): ``rule`` only, or every
        rule. The backoff does not apply to a requested rule.

        True if accepted; False without sync (nothing runs cycles); ``ValueError`` for a rule
        that is not configured, ``RulePaused`` (a ``ValueError``) for a paused one; once the
        scheduler has stopped, ``DaemonStopping``. Every rule means every rule that is not paused.

        With ``wait``: a future of the first cycle that starts after this request and runs
        ``rule`` (every unpaused rule, for None), resolved with its result (None: that cycle
        failed); ``OpsUnavailable`` without sync. Cancelling the future only stops the waiting."""
        if rule is not None and rule not in self._rules:
            raise ValueError(f"unknown rule {rule!r}")
        if rule is not None and rule in self._paused():
            raise RulePaused(rule)
        if not self._sync:
            if wait:
                raise ops.OpsUnavailable("The daemon runs with --no-sync: it runs no sync cycles.")
            return False
        if self._scheduled:
            raise ops.DaemonStopping()
        future = self._wait_for_cycle(None if rule is None else {rule}) if wait else None
        if rule is None:
            self._pending_all = True
        else:
            self._pending.add(rule)
        self._wake.set()
        return future if future is not None else True

    async def run_op(
        self, name: str, args: dict[str, Any], dry_run: bool, *, source: ops.Source
    ) -> ops.OpResult:
        """Run one operation between cycles (an operation that arrives during a cycle waits for
        it). ``OpsUnavailable`` without sync (no lock, no store) or without a config path.

        The operation runs on the event loop's thread: the store's sqlite connection belongs to
        it, and an operation is one small file write and one row; the lock keeps cycles out."""
        if name == "notify.test" and not dry_run:
            self._throttle_test_push()
        if self._store is None:
            if name == "notify.test":  # needs only the config: a viewer daemon can test its pushes
                return await ops.notify_test(self._config, args, dry_run)
            does = "runs no sync cycles" if name == "trigger" else "changes no configuration"
            raise ops.OpsUnavailable(f"The daemon runs with --no-sync: it {does}.")
        if self._config_path is None:
            raise ops.OpsUnavailable("The daemon was started without a config file path.")
        if name in ops.UNLOCKED_OPS:  # only asks for a cycle: never waits for one to finish first
            ctx = self._context(source)
            result = await ops.run_op_async(ctx, name, args, dry_run)
            return await ctx.followup() if ctx.followup is not None else result
        async with self._cycle_lock:
            if self._store is None:  # stopped while waiting
                raise ops.DaemonStopping()
            # An edit made by hand just before is applied and recorded first, as its own change
            # (the operation would otherwise fold it into its own row).
            await self._check_config()
            ctx = self._context(source)
            result = await self._op(ctx, name, args, dry_run)
        # Outside the lock: what the operation waits for (a cycle) needs it.
        if ctx.followup is not None:
            return await ctx.followup()
        return result

    def _throttle_test_push(self) -> None:
        """At most one ``notify.test`` per ``NOTIFY_TEST_COOLDOWN`` (``TooSoon`` otherwise)."""
        now = time.monotonic()
        if self._last_test_push is not None and now - self._last_test_push < ops.NOTIFY_TEST_COOLDOWN:
            raise ops.TooSoon()
        self._last_test_push = now

    async def _op(self, ctx: ops.OpContext, name: str, args: dict[str, Any], dry_run: bool) -> ops.OpResult:
        """Run one operation and apply the config it wrote. The caller holds ``_cycle_lock``."""
        try:
            return await ops.run_op_async(ctx, name, args, dry_run)
        finally:
            await self._apply_written()

    @contextlib.asynccontextmanager
    async def borrow_adapters(self, config: Config, names: set[str]) -> AsyncIterator[wiring.Adapters]:
        """``ops.DaemonHooks.borrow_adapters``: the daemon's adapters for the accounts it runs, a
        temporary build (closed afterwards) for the others. The caller holds ``_cycle_lock``, so
        no cycle uses the daemon's adapters meanwhile."""
        borrowed = wiring.Adapters(self._log)
        # An account whose setup failed is built again: its credential may have been stored since
        # (``calsync auth`` before ``calsync calendars --refresh``).
        own = {n for n in names if n in self._parts and n not in self._adapters.errors}
        mine = self._adapters
        borrowed.sources.update({n: a for n, a in mine.sources.items() if n in own})
        borrowed.targets.update({n: a for n, a in mine.targets.items() if n in own})
        borrowed.errors.update({n: e for n, e in mine.errors.items() if n in own})
        extra: wiring.Adapters | None = None
        if names - own:
            factory: AdaptersFactory = self._factory or wiring.build_adapters
            extra = await factory(config, names - own, log=self._log)
            borrowed.sources.update(extra.sources)
            borrowed.targets.update(extra.targets)
            borrowed.errors.update(extra.errors)
        try:
            yield borrowed
        finally:
            if extra is not None:
                await self._close("temporary", extra)

    async def _refresh_calendars(self) -> None:
        """Fetch every configured account's calendar list into the cache when it is due (at
        start, before the first cycle, then every ``CALENDAR_REFRESH``). The caller holds
        ``_cycle_lock``; each listing waits at most ``ops.REFRESH_TIMEOUT``. A failure (a timeout
        included) is logged by type and never stops the scheduler."""
        now = self._clock()
        if self._store is None or (self._calendars_due is not None and now < self._calendars_due):
            return
        self._calendars_due = now + CALENDAR_REFRESH
        names = sorted(self._config.accounts)
        try:
            async with self.borrow_adapters(self._config, set(names)) as adapters:
                await ops.refresh_calendars(self._store, adapters, names, now)  # logs each failure
        except Exception as exc:  # the store or a bug; the next refresh tries again
            log.warning("refreshing the calendar lists failed (%s)", type(exc).__name__)
            debug.details(log, "refreshing the calendar lists failed", exc)

    def _context(self, source: ops.Source) -> ops.OpContext:
        assert self._store is not None and self._config_path is not None
        return ops.OpContext(
            config_path=self._config_path,
            state_dir=self._state_dir,
            store=self._store,
            source=source,
            clock=self._clock,
            on_config_written=self._config_written,
            daemon=self,
        )

    # ----- approvals ----------------------------------------------------------
    def approve(
        self, rule: str, fingerprint: str, count: int, origin: ops.Source, *, wait: bool
    ) -> "asyncio.Future[CycleResult | None] | None":
        """``ops.DaemonHooks.approve``: keep the approval for the rule's next cycle and request
        it. The caller holds ``_cycle_lock`` (no cycle runs). ``ValueError`` (``RulePaused``)
        for a rule that is unknown or paused, ``OpsUnavailable`` once the scheduler has stopped
        (no cycle would consume it); nothing is stored then."""
        if not self._scheduling:
            raise ops.DaemonStopping()
        self.request_cycle(rule)
        self._approvals[rule] = Approval(fingerprint, origin)
        if self._store is not None:  # the approval is this block's push: its key closes now
            notify.approving(self._store, rule)
        self._on_approved(rule, count, origin)
        return self._wait_for_cycle({rule}) if wait else None

    def _on_approved(self, rule: str, count: int, origin: ops.Source) -> None:
        """Blocked deletes were approved: logged and pushed (``approved``, every time)."""
        log.info("rule %s: %d deletes approved (%s)", rule, count, origin)
        self._notify([notify.approved(rule, count, origin, self._config, self._clock())])

    def push(self, alerts: list[notify.Alert]) -> None:
        """``ops.DaemonHooks.push``: an operation's pushes (a loosened delete guard)."""
        self._notify(alerts)

    def _notify(self, alerts: list[notify.Alert]) -> None:
        """Send ``alerts`` in the background: a slow channel never holds up the schedule."""
        self._outbox.post(self._config.notify, alerts)

    def _wait_for_cycle(self, rules: set[str] | None) -> "asyncio.Future[CycleResult | None]":
        """A future of the first cycle that starts from now on and runs any of ``rules`` (None:
        every rule not paused then): its result, or None if that cycle failed."""
        future: asyncio.Future[CycleResult | None] = asyncio.get_running_loop().create_future()
        self._waiters = [w for w in self._waiters if not w.future.done()]  # given up meanwhile
        self._waiters.append(_Waiter(rules, self._started_cycles, future))
        return future

    def _resolve_waiters(self, cycle: int, ran: Iterable[str], result: CycleResult | None) -> None:
        """Answer the waiters that cycle number ``cycle`` (which ran ``ran``) satisfies."""
        ran = set(ran)
        unpaused = set(self._rules) - self._paused()
        keep: list[_Waiter] = []
        for waiter in self._waiters:
            if waiter.future.done():  # given up (a timeout, a client gone): forgotten
                continue
            wanted = unpaused if waiter.rules is None else waiter.rules
            if waiter.after < cycle and (ran >= wanted if waiter.rules is None else bool(wanted & ran)):
                waiter.future.set_result(result)
            else:
                keep.append(waiter)
        self._waiters = keep

    def _abandon(self, rules: set[str], error: Exception) -> None:
        """Drop the approvals of ``rules`` and refuse the waiters that wait only for them."""
        for name in rules:
            self._approvals.pop(name, None)
        keep: list[_Waiter] = []
        for waiter in self._waiters:
            if waiter.rules is not None and waiter.rules <= rules:
                if not waiter.future.done():
                    waiter.future.set_exception(error)
            else:
                keep.append(waiter)
        self._waiters = keep

    def _config_written(self, snapshot: Snapshot) -> None:
        """``on_config_written``: the file now holds ``snapshot``. Remembered at once, so the
        watcher never takes the daemon's own write for an external edit; applied (async) when
        the operation returns."""
        if self._written is None:
            self._before_written = self._snapshot
        self._snapshot, self._rejected = snapshot, None
        self._written = snapshot

    async def _apply_written(self) -> None:
        snapshot, self._written = self._written, None
        before, self._before_written = self._before_written, None
        if snapshot is None:
            return
        try:
            config = validate_text(snapshot.text, snapshot.path.parent)  # write_config validated it
        except ConfigError as exc:  # cannot happen short of a bug; keep running what runs
            log.error("the written config could not be applied (%d problems)", len(exc.problems))
            self._unremember(before)
            return
        try:
            await self._apply(config)
        except Exception as exc:  # a bug: the running config stays (``_apply`` rolled back)
            log.error("config reload failed (%s)", type(exc).__name__)
            debug.details(log, "config reload failed", exc)
            self._unremember(before)
            return
        self._reload_ok()
        log.info("config changed; applied")

    def _unremember(self, before: Snapshot | None) -> None:
        """The written file is not what runs: the watcher remembers what runs again, and reads
        the file at its next poll, which retries (or reports) the written config."""
        self._snapshot, self._stat = before, None

    @property
    def config(self) -> Config:
        """The config the daemon runs now."""
        return self._config

    def snapshot(self) -> DaemonInfo:
        return DaemonInfo(
            running=self._running,
            sync=self._sync,
            pid=os.getpid(),
            started_at=self._started_at,
            last_cycle_at=self._last_cycle_at,
            next_cycle_at=self._next_cycle_at,
            cycles=self._cycles,
            cycle_started_at=self._cycle_started_at,
        )

    # ----- resources ----------------------------------------------------------
    @contextlib.asynccontextmanager
    async def _resources(self) -> AsyncIterator[tuple[socket.socket | None, socket.socket | None]]:
        """The lock (with sync), the API socket and the TCP socket (when serving; yielded),
        then the store, the adapters and the engine (with sync), closed in reverse."""
        async with contextlib.AsyncExitStack() as stack:
            if self._sync:
                stack.enter_context(RunLock(self._lock_path))
            sock: socket.socket | None = None
            if self._serve_api:
                self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                sock = stack.enter_context(api.unix_socket(wiring.socket_path(self._state_dir)))
            tcp: socket.socket | None = None
            if self._serve_api and self._listen is not None:
                tcp = stack.enter_context(api.tcp_socket(*self._listen))
                host, port = tcp.getsockname()[:2]
                self.web_address = (host, port)
            if self._sync:
                store = Store(self._db_path)
                stack.callback(store.close)
                stack.push_async_callback(self._release)
                await self._build(sorted(wiring.accounts_in_use(self._config)))
                self._store = store
                self._engine = wiring.build_engine(self._config, self._adapters, store, clock=self._clock)
                self._remember_file()
            yield sock, tcp

    def _remember_file(self) -> None:
        """The watcher's starting point: the file as it is now holds the running config (it
        was loaded from it a moment ago)."""
        if self._config_path is None:
            return
        try:
            self._stat = _stat(self._config_path)
            self._snapshot = read_snapshot(self._config_path)
        except (OSError, ConfigError) as exc:  # no baseline: the first readable edit applies
            log.warning("the config file could not be read (%s)", type(exc).__name__)
            self._stat, self._snapshot = None, None

    async def _release(self) -> None:
        self._engine = self._store = None
        await self._close_adapters()

    async def _build(self, names: Iterable[str]) -> None:
        """Build (or rebuild) each account's adapters; a replaced build's clients are closed."""
        factory: AdaptersFactory = self._factory or wiring.build_adapters
        merged = self._adapters
        for name in names:
            fresh = await factory(self._config, {name}, log=self._log)
            old, self._parts[name] = self._parts.get(name), fresh
            for table in (merged.sources, merged.targets, merged.errors):
                table.pop(name, None)
            if name in fresh.sources:
                merged.sources[name] = fresh.sources[name]
            if name in fresh.targets:
                merged.targets[name] = fresh.targets[name]
            if name in fresh.errors:
                merged.errors[name] = fresh.errors[name]
            if old is not None:
                await self._close(name, old)

    async def _drop(self, name: str) -> None:
        """Close one account's adapters (no rule uses it any more)."""
        old = self._parts.pop(name, None)
        for table in (self._adapters.sources, self._adapters.targets, self._adapters.errors):
            table.pop(name, None)
        if old is not None:
            await self._close(name, old)

    async def _close_adapters(self) -> None:
        parts, self._parts = self._parts, {}
        for name, part in parts.items():
            await self._close(name, part)

    @staticmethod
    async def _close(name: str, adapters: wiring.Adapters) -> None:
        try:
            await adapters.aclose()
        except Exception as exc:
            log.warning("closing the %s clients failed: %s", name, type(exc).__name__)

    # ----- schedule -----------------------------------------------------------
    async def _scheduler(self, stop: asyncio.Event) -> None:
        self._scheduling = True
        try:
            await self._schedule(stop)
        finally:  # nothing runs cycles any more: nobody waits for one
            self._scheduling = False
            self._scheduled = True
            self._approvals.clear()
            waiters, self._waiters = self._waiters, []
            for waiter in waiters:
                if not waiter.future.done():
                    waiter.future.set_exception(ops.DaemonStopping())

    async def _schedule(self, stop: asyncio.Event) -> None:
        due = True  # the first cycle starts at once
        deadline = self._clock()
        while not stop.is_set():
            async with self._cycle_lock:  # right before each cycle, so it runs the file as it is
                await self._check_config()
                # When due (at start, before the first cycle: the GUI's map must not wait for a
                # long first sync; then every CALENDAR_REFRESH). Not while stopping.
                if not stop.is_set():
                    await self._refresh_calendars()
            if stop.is_set():  # set during the refresh: no cycle starts after it
                break
            rules, every = self._select(due)
            if rules:
                await self._cycle(rules)
            if every:
                # A timed cycle, or one requested for every rule, restarts the timer. A cycle
                # for named rules keeps the deadline, so requests for one rule never starve
                # the others.
                deadline = self._clock() + timedelta(seconds=self._delay())
            if stop.is_set():
                break
            if self._wake.is_set():  # requested during the cycle: one follow-up, at once
                due = False
                continue
            delay = (deadline - self._clock()).total_seconds()
            if delay <= 0:  # the deadline passed during a requested cycle
                due = True
                continue
            self._next_cycle_at = deadline
            try:
                due = await self._wait(delay, stop)
            finally:
                self._next_cycle_at = None

    def _delay(self) -> float:
        runtime = self._config.runtime
        return runtime.interval.total_seconds() * (1 + runtime.jitter * (2 * self._rng() - 1))

    async def _wait(self, delay: float, stop: asyncio.Event) -> bool:
        """Wait ``delay`` seconds, or less if a cycle is requested or ``stop`` is set; True if
        the time ran out. While a config file is watched, the wait is cut into polls of
        ``WATCH_INTERVAL`` seconds, each followed by a check of the file."""
        woken = asyncio.ensure_future(self._wake.wait())
        stopped = asyncio.ensure_future(stop.wait())
        remaining = delay
        try:
            while True:
                step = min(remaining, WATCH_INTERVAL) if self._watching() else remaining
                timer = asyncio.ensure_future(self._sleep(step))
                try:
                    done, _ = await asyncio.wait({timer, woken, stopped}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    timer.cancel()
                    await asyncio.gather(timer, return_exceptions=True)
                if timer not in done:
                    return False
                timer.result()  # a failing sleep fails the daemon
                remaining -= step
                if remaining <= 1e-9:
                    return True
                if stop.is_set() or self._wake.is_set():
                    return False
                async with self._cycle_lock:  # an operation may be running
                    await self._check_config()
        finally:
            for task in (woken, stopped):
                task.cancel()
            await asyncio.gather(woken, stopped, return_exceptions=True)

    def _watching(self) -> bool:
        return self._config_path is not None and self._store is not None

    # ----- hot reload ---------------------------------------------------------
    async def _check_config(self) -> None:
        """Apply an edit made to the config file outside calsync. The caller holds
        ``_cycle_lock``. Cheap when nothing changed: one ``stat``. An unexpected failure is
        logged by type and the running config stays: it never stops the scheduler."""
        try:
            await self._reload()
        except Exception as exc:  # the text could quote a value; the type only
            log.error("config reload failed (%s)", type(exc).__name__)
            debug.details(log, "config reload failed", exc)

    async def _reload(self) -> None:
        path = self._config_path
        if path is None or self._store is None:
            return
        stat: tuple[int, int] | None
        try:
            stat = _stat(path)
        except FileNotFoundError:
            stat = None
        except OSError as exc:  # no permission, …: logged once until it can be checked again
            if self._stat != _UNCHECKABLE:
                log.warning("the config file could not be checked (%s)", type(exc).__name__)
            self._stat = _UNCHECKABLE
            return
        if stat == self._stat:
            return
        self._stat = stat
        if stat is None:
            self._reload_failed("", ["the config file is missing"])
            return
        try:
            snapshot = read_snapshot(path)
        except ConfigError as exc:  # not UTF-8
            self._reload_failed(f"unreadable:{stat}", exc.problems)
            return
        except OSError as exc:
            log.warning("the config file could not be read (%s)", type(exc).__name__)
            self._stat = None  # try again at the next poll
            return
        if not snapshot.text.strip():
            # Valid TOML for "no accounts, no rules", but far more likely an editor caught
            # between truncating and writing: never drop every rule because of it.
            self._reload_failed(snapshot.fingerprint, ["the config file is empty"])
            return
        previous = self._snapshot
        if previous is not None and snapshot.fingerprint == previous.fingerprint:
            self._rejected = None  # touched, or edited back to what runs
            self._reload_ok()
            return
        if snapshot.fingerprint == self._rejected:
            return
        try:
            config = validate_text(snapshot.text, path.parent)
        except ConfigError as exc:
            self._reload_failed(snapshot.fingerprint, exc.problems)
            return
        try:
            await self._apply(config)
        except Exception:
            self._rejected = snapshot.fingerprint  # logged once (by the caller), not every poll
            raise
        self._snapshot, self._rejected = snapshot, None
        self._reload_ok()
        log.info("config reloaded (%s)", EXTERNAL_SUMMARY)
        try:
            self._store.record_config_change(
                at=self._clock(),
                source="external",
                summary=EXTERNAL_SUMMARY,
                before=previous.text if previous is not None else None,
                after_fingerprint=snapshot.fingerprint,
            )
        except Exception as exc:  # applied all the same
            log.warning("config history not recorded (%s)", type(exc).__name__)

    def _reload_failed(self, fingerprint: str, problems: list[str]) -> None:
        if fingerprint == self._rejected:
            return
        self._rejected = fingerprint
        self._on_reload_failed(fingerprint, problems)

    def _on_reload_failed(self, fingerprint: str, problems: list[str]) -> None:
        """An edit made outside calsync is invalid; the running config stays. The count only:
        a problem can quote a value (a push URL with a device key in it). Pushed once per text
        (``reload_failed``, keyed by ``fingerprint``) until a config applies."""
        log.warning("config reload failed (%d problems)", len(problems))
        if self._store is not None:
            self._notify(
                notify.reload_failed(self._store, fingerprint, len(problems), self._config, self._clock())
            )

    def _reload_ok(self) -> None:
        """The file holds a config that runs again: an open ``reload_failed`` alert closes."""
        if self._store is not None:
            notify.reload_ok(self._store, self._clock())

    async def _apply(self, config: Config) -> None:
        """Run ``config`` from now on. The caller holds ``_cycle_lock`` (never during a cycle).

        If it fails (a bug), the old config is restored before the error is raised: the
        accounts rebuilt for the new one are rebuilt again before their rules' next attempt,
        and accounts the new config no longer uses are closed only once it is in place."""
        old, self._config = self._config, config  # first: ``_build`` reads it
        self._rules = [r.name for r in config.rules]
        before, after = wiring.accounts_in_use(old), wiring.accounts_in_use(config)
        secret_changed = old.google_client_secret_file != config.google_client_secret_file
        rebuild = sorted(
            name
            for name in after
            if name not in before
            or old.accounts.get(name) != config.accounts.get(name)
            or (secret_changed and config.accounts[name].provider == "google")
        )
        try:
            for name in rebuild:
                try:
                    await self._build([name])
                except Exception as exc:  # its rules fail on their own (run_cycle); the rest applies
                    log.warning("setting up account %s failed (%s)", name, type(exc).__name__)
                    debug.details(log, f"setting up account {name} failed", exc)
                    self._adapters.errors[name] = CalsyncError(
                        f"account {name} could not be set up ({type(exc).__name__})"
                    )
            if self._engine is not None:
                self._engine.replace_config(config)
        except Exception:
            self._config, self._rules = old, [r.name for r in old.rules]
            if self._engine is not None:
                with contextlib.suppress(Exception):
                    self._engine.replace_config(old)
            rebuilt = set(rebuild)
            self._reauth |= {r.name for r in old.rules if {r.source.account, r.target.account} & rebuilt}
            raise
        for name in sorted(before - after):
            await self._drop(name)
        names = set(self._rules)
        self._pending &= names
        self._reauth &= names
        gone = {n for n in self._approvals if n not in names}
        held = {r.name for r in config.rules if r.paused} & set(self._approvals)
        for name in sorted(gone | held):
            why = "removed" if name in gone else "paused"
            self._abandon({name}, ConfigError([f'rule "{name}" was {why} before its approved deletes ran']))
        # The other waiters of a rule removed or paused meanwhile (``trigger``): no cycle runs it.
        waited = {n for w in self._waiters if w.rules is not None for n in w.rules}
        paused = {r.name for r in config.rules if r.paused}
        for name in sorted(waited - names):
            self._abandon({name}, ConfigError([f'rule "{name}" was removed before its cycle ran']))
        for name in sorted(waited & paused):
            self._abandon({name}, RulePaused(name))
        for name in [n for n in self._failures if n not in names]:
            del self._failures[name]
        # The listener is bound at start (from the config or a ``--listen`` override, so the
        # effective address is ``_listen``); the address is config, not a URL of anyone's data.
        if (wanted := _web_address(config)) != _web_address(old) and wanted != self._listen:
            if wanted is None:
                log.info("restart the daemon to turn the web UI off")
            else:
                host = f"[{wanted[0]}]" if ":" in wanted[0] else wanted[0]
                log.info("restart the daemon to listen on %s:%d", host, wanted[1])

    def _paused(self) -> set[str]:
        return {r.name for r in self._config.rules if r.paused}

    def _select(self, due: bool) -> tuple[list[str], bool]:
        """This cycle's rules, in config order: the requested ones and, when the wait ran out,
        every rule not backed off. Takes the pending requests. The flag: a cycle for every
        rule (timed, or requested for all)."""
        every = due or self._pending_all
        requested = set(self._rules) if self._pending_all else set(self._pending)
        self._pending_all = False
        self._pending.clear()
        self._wake.clear()
        now = self._clock()
        paused = self._paused()  # never run, not even when requested
        rules = [
            r
            for r in self._rules
            if r not in paused and (r in requested or (due and not self._backed_off(r, now)))
        ]
        return rules, every

    def _backed_off(self, rule: str, now: datetime) -> bool:
        entry = self._failures.get(rule)
        if entry is None:
            return False
        failures, last = entry
        return now < last + backoff_for(failures)

    # ----- one cycle ----------------------------------------------------------
    async def _cycle(self, rules: list[str]) -> None:
        async with self._cycle_lock:
            self._started_cycles += 1  # a waiter registered from now on waits for the next one
            self._cycle_started_at = self._clock()
            try:
                await self._locked_cycle(self._started_cycles, rules)
            finally:
                self._cycle_started_at = None

    async def _locked_cycle(self, cycle: int, rules: list[str]) -> None:
        assert self._engine is not None and self._store is not None
        # This cycle consumes the approvals of its rules, whatever its outcome.
        approvals = {r: self._approvals.pop(r) for r in rules if r in self._approvals}
        try:
            reauth = [r for r in rules if r in self._reauth]
            if reauth:
                await self._build(sorted(wiring.accounts_in_use(self._config, reauth)))
                self._reauth.difference_update(reauth)
            self._log.clear()  # the per-cycle count uses marks; the entries need not pile up
            result = await run_cycle(
                self._engine,
                self._store,
                self._config,
                self._adapters,
                rules,
                dry_run=False,
                allow_deletes=False,
                approvals=approvals,
            )
        except Exception as exc:
            # The state database or a bug; the next cycle tries again. The type only: the text
            # (and a traceback's locals) can carry a title or a request URL. CALSYNC_DEBUG=1
            # adds the traceback, at DEBUG.
            log.error("sync cycle failed (%s)", type(exc).__name__)
            debug.details(log, "sync cycle failed", exc)
            self._resolve_waiters(cycle, rules, None)
            return
        self._record(cycle, result)
        await self._remove_retired(result)

    async def _remove_retired(self, result: CycleResult) -> None:
        """Remove the retiring rules this cycle finished (still under ``_cycle_lock``). A failed
        removal is logged and tried again after the next cycle."""
        if self._config_path is None or self._store is None:
            return
        if not retired(self._config, result, self._store):
            return
        await self._check_config()  # a hand edit since the cycle started is recorded first
        # Judged again against the config as it is now: a hand edit may have kept the rule, or
        # pointed it at another target (then the next cycle, run against that one, decides).
        for name in retired(self._config, result, self._store):
            try:
                if remove_retired(self._context("daemon"), name) is not None:
                    log.info(
                        "rule %s retired: its copies are deleted and it is removed from the config", name
                    )
            finally:
                await self._apply_written()

    def _record(self, cycle: int, result: CycleResult) -> None:
        now = self._clock()
        self._cycles += 1
        self._last_cycle_at = now
        parts: list[str] = []
        for name, r in result.rules.items():
            if _counts_failure(r):
                failures = self._failures.get(name, (0, now))[0] + 1
                self._failures[name] = (failures, now)
            elif r.status != "blocked":  # blocked is neither a failure nor a success
                self._failures.pop(name, None)
            if _needs_rebuild(r, self._adapters):
                self._reauth.add(name)
            parts.append(counts_line(name, r) + (f" error_kind={r.error_kind}" if r.error_kind else ""))
        parts.append(f"requests={result.requests}")
        log.info("%s", "; ".join(parts))
        if self._store is not None:  # the runs are recorded: what needs a person is pushed
            self._notify(notify.evaluate(result.rules, store=self._store, config=self._config, now=now))
        self._resolve_waiters(cycle, result.rules, result)


@dataclass(frozen=True, slots=True)
class _Waiter:
    """An operation waiting for a cycle: of any of ``rules`` (None: of every unpaused rule),
    started after cycle number ``after``."""

    rules: set[str] | None
    after: int
    future: "asyncio.Future[CycleResult | None]"


_UNCHECKABLE = (-1, -1)


def _needs_rebuild(result: RuleResult, adapters: wiring.Adapters) -> bool:
    """Whether the rule's accounts are rebuilt before its next attempt: after an auth failure,
    and after a config-kind failure to set an account up (the build reads local files that a
    person may fix meanwhile). A config error raised in the cycle (a calendar not found) is
    not about the build: a rebuild would not change it."""
    if result.error_kind == "auth":
        return True
    return result.error_kind == "config" and result.error_account in adapters.errors


def _check_web_key(state_dir: Path) -> None:
    """One warning at start when ``web.key`` exists but cannot be used (unreadable, or open to
    other users): no browser can pair until it is replaced. The type only, never the key."""
    try:
        web_auth.read_key(state_dir)
    except (OSError, ValueError, CalsyncError) as exc:  # ValueError: not UTF-8 text
        log.warning(
            "the web UI's key file cannot be used (%s); replace it with `calsync web reset-key`",
            type(exc).__name__,
        )


def _web_address(config: Config) -> tuple[str, int] | None:
    """Where ``[web]`` asks the web UI to listen; None: off."""
    return (config.web.host, config.web.port) if config.web.enabled else None


def _stat(path: Path) -> tuple[int, int]:
    """The cheap change check: (size, mtime_ns) of the file (through a symlink)."""
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)
