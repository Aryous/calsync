"""`calsync daemon`: sync cycles on a jittered schedule in one long-lived process.

With sync, the daemon holds the run lock for its whole life, keeps one ``Store``, one set of
adapters and one ``Engine`` (whose in-memory mirrors and target caches then carry from cycle to
cycle), and runs a cycle at start, then after every wait of ``interval × (1 ± jitter)``.

- ``request_cycle()`` cuts the wait short. Requests made during a cycle collapse into one
  follow-up cycle; a request names one rule or means every rule. A cycle for named rules keeps
  the timer's deadline; a timed cycle or one for every rule restarts it.
- A rule that keeps failing is skipped for ``BACKOFF`` minutes after its latest failure (by
  the number of failures in a row; a blocked result neither adds to nor resets the count). A
  requested rule runs regardless; a skipped rule records no run.
- A rule that failed for authentication gets its accounts' adapters rebuilt before its next
  attempt, which picks up a credential stored meanwhile by ``calsync auth``.

Beside the scheduler, the daemon serves the local API (``calsync.api``) on
``<state_dir>/calsync.sock`` and, given ``listen``, the web UI (``calsync.web.app``) over TCP,
with and without sync. Each service catches its own failure, logs it and sets ``stop``:
a failing server never cancels a cycle in progress. Once every service has ended (the cycle
finished), ``run`` raises the first failure.

Operations (``calsync.ops``) posted to the socket run inside the daemon, under ``_cycle_lock``:
never during a cycle, and the store keeps one writer. In this version a config the daemon
writes takes effect when it restarts.

Without sync (``--no-sync``) the daemon takes no lock, reads no credential, opens no store and
runs no cycle: it only serves the API. It cannot run beside another daemon in the same state
directory (that one's socket is live: exit 4), only beside ``calsync run``; to show a running
daemon's data, point it at a copy of the state directory.
"""

import asyncio
import contextlib
import logging
import os
import random
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from calsync import api, debug, ops, wiring
from calsync.config import Config
from calsync.configfile import Snapshot
from calsync.cycle import counts_line, run_cycle
from calsync.engine import CycleResult, Engine, RuleResult
from calsync.http import RequestLog
from calsync.lock import RunLock
from calsync.store import Store
from calsync.web.app import create_web_app

log = logging.getLogger(__name__)

# Minutes a rule is skipped after 1, 2, 3, 4 and 5 or more failures in a row.
BACKOFF = (1, 2, 4, 8, 15)
BACKOFF_CAP = timedelta(minutes=15)

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
        self._config_path = config_path  # the file operations edit; None: no operations
        self._written: Snapshot | None = None  # the config last written by an operation
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
        await api.serve(app, sock, stop)

    def request_cycle(self, rule: str | None = None) -> bool:
        """Run a cycle now (after the current one, if one is running): ``rule`` only, or every
        rule. The backoff does not apply to a requested rule.

        True if accepted; False without sync (nothing runs cycles); ``ValueError`` for a rule
        that is not configured."""
        if rule is not None and rule not in self._rules:
            raise ValueError(f"unknown rule {rule!r}")
        if not self._sync:
            return False
        if rule is None:
            self._pending_all = True
        else:
            self._pending.add(rule)
        self._wake.set()
        return True

    async def run_op(
        self, name: str, args: dict[str, Any], dry_run: bool, *, source: ops.Source
    ) -> ops.OpResult:
        """Run one operation between cycles (an operation that arrives during a cycle waits for
        it). ``OpsUnavailable`` without sync (no lock, no store) or without a config path.

        The operation runs on the event loop's thread: the store's sqlite connection belongs to
        it, and an operation is one small file write and one row; the lock keeps cycles out."""
        if self._store is None:
            raise ops.OpsUnavailable("The daemon runs with --no-sync: it changes no configuration.")
        if self._config_path is None:
            raise ops.OpsUnavailable("The daemon was started without a config file path.")
        async with self._cycle_lock:
            store = self._store
            if store is None:  # stopped while waiting
                raise ops.OpsUnavailable("The daemon is stopping.")
            ctx = ops.OpContext(
                config_path=self._config_path,
                state_dir=self._state_dir,
                store=store,
                source=source,
                clock=self._clock,
                on_config_written=self._config_written,
            )
            return ops.run_op(ctx, name, args, dry_run)

    def _config_written(self, snapshot: Snapshot) -> None:
        self._written = snapshot
        log.info("config changed; applied at restart")

    def snapshot(self) -> DaemonInfo:
        return DaemonInfo(
            running=self._running,
            sync=self._sync,
            pid=os.getpid(),
            started_at=self._started_at,
            last_cycle_at=self._last_cycle_at,
            next_cycle_at=self._next_cycle_at,
            cycles=self._cycles,
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
            yield sock, tcp

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
        due = True  # the first cycle starts at once
        deadline = self._clock()
        while not stop.is_set():
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
        the time ran out."""
        timer = asyncio.ensure_future(self._sleep(delay))
        woken = asyncio.ensure_future(self._wake.wait())
        stopped = asyncio.ensure_future(stop.wait())
        try:
            done, _ = await asyncio.wait({timer, woken, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (timer, woken, stopped):
                task.cancel()
            await asyncio.gather(timer, woken, stopped, return_exceptions=True)
        if timer in done:
            timer.result()  # a failing sleep fails the daemon
            return True
        return False

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
        rules = [r for r in self._rules if r in requested or (due and not self._backed_off(r, now))]
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
            await self._locked_cycle(rules)

    async def _locked_cycle(self, rules: list[str]) -> None:
        assert self._engine is not None and self._store is not None
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
            )
        except Exception as exc:
            # The state database or a bug; the next cycle tries again. The type only: the text
            # (and a traceback's locals) can carry a title or a request URL. CALSYNC_DEBUG=1
            # adds the traceback, at DEBUG.
            log.error("sync cycle failed (%s)", type(exc).__name__)
            debug.details(log, "sync cycle failed", exc)
            return
        self._record(result)

    def _record(self, result: CycleResult) -> None:
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
            if r.error_kind == "auth":
                self._reauth.add(name)
            parts.append(counts_line(name, r) + (f" error_kind={r.error_kind}" if r.error_kind else ""))
        parts.append(f"requests={result.requests}")
        log.info("%s", "; ".join(parts))
