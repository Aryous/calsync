"""`calsync daemon`: sync cycles on a jittered schedule in one long-lived process.

With sync, the daemon holds the run lock for its whole life, keeps one ``Store``, one set of
adapters and one ``Engine`` (whose in-memory mirrors and target caches then carry from cycle to
cycle), and runs a cycle at start, then after every wait of ``interval × (1 ± jitter)``.

- ``request_cycle()`` cuts the wait short. Requests made during a cycle collapse into one
  follow-up cycle; a request names one rule or means every rule.
- A rule that keeps failing is skipped for ``BACKOFF`` minutes after its latest failure (by
  the number of failures in a row). A requested rule runs regardless; a skipped rule records
  no run.
- A rule that failed for authentication gets its accounts' adapters rebuilt before its next
  attempt, which picks up a credential stored meanwhile by ``calsync auth``.

Without sync (``--no-sync``) the daemon takes no lock, reads no credential, opens no store and
runs no cycle: it only serves the API beside a daemon (or ``calsync run``) that does.
"""

import asyncio
import contextlib
import logging
import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from calsync import wiring
from calsync.config import Config
from calsync.cycle import counts_line, run_cycle
from calsync.engine import CycleResult, Engine, RuleResult
from calsync.http import RequestLog
from calsync.lock import RunLock
from calsync.store import Store

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
        clock: Callable[[], datetime] = _now,
        sleep: Sleep = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        adapters_factory: AdaptersFactory | None = None,
    ) -> None:
        self._config = config
        self._db_path, self._lock_path = wiring.state_paths(state_dir)
        self._sync = sync
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

    # ----- interface ----------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        """Run until ``stop`` is set; a cycle in progress finishes first.

        Every service runs as a task of one group and ends when ``stop`` is set: the scheduler
        (with sync) and the API server beside it.
        """
        self._running = True
        try:
            async with self._resources(), asyncio.TaskGroup() as group:
                if self._sync:
                    group.create_task(self._scheduler(stop))
                group.create_task(stop.wait())
        except ExceptionGroup as failed:  # one service failed: raise its error as is
            if len(failed.exceptions) == 1:
                raise failed.exceptions[0] from None
            raise
        finally:
            self._running = False

    def request_cycle(self, rule: str | None = None) -> None:
        """Run a cycle now (after the current one, if one is running): ``rule`` only, or every
        rule. The backoff does not apply to a requested rule. Ignored without sync."""
        if rule is not None and rule not in self._rules:
            raise ValueError(f"unknown rule {rule!r}")
        if not self._sync:
            return
        if rule is None:
            self._pending_all = True
        else:
            self._pending.add(rule)
        self._wake.set()

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
    async def _resources(self) -> AsyncIterator[None]:
        """With sync: the lock, the store, the adapters and the engine, closed in reverse."""
        if not self._sync:
            yield
            return
        with RunLock(self._lock_path):
            store = Store(self._db_path)
            try:
                try:
                    await self._build(sorted(wiring.accounts_in_use(self._config)))
                    self._store = store
                    self._engine = wiring.build_engine(self._config, self._adapters, store, clock=self._clock)
                    yield
                finally:
                    self._engine = self._store = None
                    await self._close_adapters()
            finally:
                store.close()

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
        while not stop.is_set():
            rules = self._select(due)
            if rules:
                await self._cycle(rules)
            if stop.is_set():
                break
            if self._wake.is_set():  # requested during the cycle: one follow-up, at once
                due = False
                continue
            delay = self._delay()
            self._next_cycle_at = self._clock() + timedelta(seconds=delay)
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

    def _select(self, due: bool) -> list[str]:
        """This cycle's rules, in config order: the requested ones and, when the wait ran out,
        every rule not backed off. Takes the pending requests."""
        requested = set(self._rules) if self._pending_all else set(self._pending)
        self._pending_all = False
        self._pending.clear()
        self._wake.clear()
        now = self._clock()
        return [r for r in self._rules if r in requested or (due and not self._backed_off(r, now))]

    def _backed_off(self, rule: str, now: datetime) -> bool:
        entry = self._failures.get(rule)
        if entry is None:
            return False
        failures, last = entry
        return now < last + backoff_for(failures)

    # ----- one cycle ----------------------------------------------------------
    async def _cycle(self, rules: list[str]) -> None:
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
        except Exception:
            # The state database or a bug; the next cycle tries again.
            log.exception("sync cycle failed")
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
            else:
                self._failures.pop(name, None)
            if r.error_kind == "auth":
                self._reauth.add(name)
            parts.append(counts_line(name, r) + (f" error_kind={r.error_kind}" if r.error_kind else ""))
        parts.append(f"requests={result.requests}")
        log.info("%s", "; ".join(parts))
