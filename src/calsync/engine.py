"""One sync cycle: ingest sources, detect target changes, plan, guard, push, record.

The engine keeps mirrors and target caches in memory across cycles (the
daemon case) and persists them after every phase, so a fresh process picks
up exactly where the last one stopped.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from calsync.config import Config, Rule
from calsync.errors import AuthError, ConflictError, NotFoundError
from calsync.http import RequestLog
from calsync.mirror import Mirror, apply_pull
from calsync.model import Window, apply_alarm_policy, series_hash, series_span
from calsync.naming import object_id, parse_resource_name, resource_name, rule_tag, uid_for
from calsync.planner import Actual, Desired, Plan, check_delete_guard, plan
from calsync.render import inspect, render
from calsync.sources.base import CalendarMeta, Source
from calsync.store import RunRecord, Store
from calsync.targets.base import CachedObject, Target, TargetCache, TargetCalendar, TargetDelta

log = logging.getLogger(__name__)

Status = Literal["ok", "partial", "failed", "blocked"]
ErrorKind = Literal["auth", "guard", "other"]


@dataclass(slots=True)
class RuleResult:
    rule: str
    status: Status = "ok"
    created: int = 0
    updated: int = 0
    deleted: int = 0
    failed: int = 0
    error: str | None = None
    error_kind: ErrorKind | None = None
    plan: Plan | None = None

    def fail(self, exc: BaseException) -> None:
        self.status = "failed"
        self.error = f"{type(exc).__name__}: {exc}"
        self.error_kind = "auth" if isinstance(exc, AuthError) else "other"


@dataclass(slots=True)
class CycleResult:
    started_at: datetime
    finished_at: datetime
    rules: dict[str, RuleResult] = field(default_factory=dict)
    requests: int = 0

    @property
    def ok(self) -> bool:
        return all(r.status == "ok" for r in self.rules.values())


def apply_target_delta(cache: TargetCache, delta: TargetDelta) -> list[str]:
    """Fold a change listing into the cache; return hrefs whose bodies must be fetched."""
    if delta.full:
        for href in [h for h in cache.objects if h not in delta.changed]:
            del cache.objects[href]
    for href in delta.deleted:
        cache.objects.pop(href, None)
    need: list[str] = []
    for href, etag in delta.changed.items():
        known = cache.objects.get(href)
        if known is None:
            need.append(href)
        elif known.etag is None and etag is not None:
            cache.objects[href] = replace(known, etag=etag)  # our own write: adopt the etag
        elif etag is None or known.etag != etag:
            need.append(href)
    return need


TargetKey = tuple[str, str]  # (account, calendar ref from config)


class Engine:
    def __init__(
        self,
        config: Config,
        store: Store,
        sources: Mapping[str, Source],
        targets: Mapping[str, Target],
        *,
        request_log: RequestLog | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._store = store
        self._sources = sources
        self._targets = targets
        self._log = request_log
        self._clock = clock
        self._source_state: dict[str, dict[str, Any]] = {}
        self._mirrors: dict[str, dict[str, Mirror]] = {}
        self._meta: dict[tuple[str, str], CalendarMeta] = {}
        self._resolved: dict[tuple[str, str], str] = {}
        self._calendars: dict[TargetKey, TargetCalendar] = {}
        self._caches: dict[tuple[str, str], TargetCache] = {}

    # ----- phase 1: ingest ---------------------------------------------
    async def _ingest(self, account: str, rules: list[Rule]) -> None:
        if account not in self._source_state:
            self._source_state[account], self._mirrors[account] = self._store.load_source(account)
        refs = sorted({r.source.calendar for r in rules})
        pull = await self._sources[account].pull(refs, self._source_state[account])
        unique = {p.calendar_id: p for p in pull.calendars.values()}
        self._store.save_source(account, pull.state, unique.values())
        self._source_state[account] = pull.state
        mirrors = self._mirrors[account]
        for ref, cal_pull in pull.calendars.items():
            self._resolved[(account, ref)] = cal_pull.calendar_id
            self._meta[(account, cal_pull.calendar_id)] = cal_pull.meta
        for cal_id, cal_pull in unique.items():
            apply_pull(mirrors.setdefault(cal_id, {}), cal_pull)

    # ----- phase 2: detect ---------------------------------------------
    async def _detect(self, key: TargetKey, rules: list[Rule], dry_run: bool) -> None:
        account, ref = key
        target = self._targets[account]
        cal = self._calendars.get(key) or self._store.load_target_calendar(account, ref)
        if cal is None:
            wants_create = any(r.target.create_if_missing for r in rules)
            try:
                cal = await target.ensure_calendar(ref, create=wants_create and not dry_run)
            except NotFoundError:
                if dry_run and wants_create:
                    raise NotFoundError(
                        f"target calendar {ref!r} does not exist yet; a real run creates it"
                    ) from None
                raise
            self._store.save_target_calendar(account, ref, cal)
        self._calendars[key] = cal
        cache_key = (account, cal.href)
        cache = self._caches.get(cache_key) or self._store.load_target_cache(account, cal.href)
        # Registered before any network call: it is unchanged until apply_target_delta.
        self._caches[cache_key] = cache
        try:
            delta = await target.changes(cal, cache.cursor)
        except NotFoundError:
            # Calendar deleted or moved: rediscover on the next cycle.
            self._store.forget_target_calendar(account, ref)
            self._calendars.pop(key, None)
            self._caches.pop(cache_key, None)
            raise
        need = apply_target_delta(cache, delta)
        for obj in await target.fetch(cal, need) if need else []:
            name = parse_resource_name(obj.href)
            if name is None:
                continue
            found = inspect(obj.data)
            cache.objects[obj.href] = CachedObject(
                etag=obj.etag,
                rule_tag=name[0],
                object_id=name[1],
                content_hash=found.content_hash if found else None,
                span=found.span if found else None,
            )
        cache.cursor = delta.cursor
        self._store.save_target_cache(account, cal.href, cache)

    # ----- phase 3-5: plan, guard, push --------------------------------
    def _desired(self, rule: Rule) -> dict[str, Desired]:
        account = rule.source.account
        cal_id = self._resolved[(account, rule.source.calendar)]
        meta = self._meta.get((account, cal_id), CalendarMeta())
        series = self._sources[account].to_series(self._mirrors[account].get(cal_id, {}).values(), meta)
        desired: dict[str, Desired] = {}
        for item in series:
            shaped = apply_alarm_policy(item, rule.alarms)
            oid = object_id(rule.name, cal_id, item.source_id)
            desired[oid] = Desired(oid, shaped, series_hash(shaped), series_span(shaped))
        return desired

    def _cache_for(self, rule: Rule) -> tuple[TargetCalendar, TargetCache]:
        key = (rule.target.account, rule.target.calendar)
        cal = self._calendars[key]
        return cal, self._caches[(rule.target.account, cal.href)]

    async def _run_rule(self, rule: Rule, result: RuleResult, *, dry_run: bool, allow_deletes: bool) -> None:
        cal, cache = self._cache_for(rule)
        tag = rule_tag(rule.name)
        actual = {
            obj.object_id: Actual(obj.object_id, href, obj.etag, obj.content_hash, obj.span)
            for href, obj in cache.objects.items()
            if obj.rule_tag == tag
        }
        window = Window.around(self._clock(), rule.window.past, rule.window.future)
        result.plan = plan(self._desired(rule), actual, window)
        reason = None if allow_deletes else check_delete_guard(result.plan, len(actual), rule.delete_guard)
        if reason:
            result.status, result.error, result.error_kind = "blocked", reason, "guard"
            return
        if not dry_run and not result.plan.empty:
            await self._push(rule, cal, cache, result.plan, result)

    async def _push(
        self, rule: Rule, cal: TargetCalendar, cache: TargetCache, p: Plan, result: RuleResult
    ) -> None:
        target = self._targets[rule.target.account]
        tag = rule_tag(rule.name)
        now = self._clock()

        def body(want: Desired) -> bytes:
            return render(
                want.series,
                uid=uid_for(want.object_id),
                rule_name=rule.name,
                content_hash=want.content_hash,
                now=now,
            )

        async def current_etag(href: str) -> str | None:
            fetched = await target.fetch(cal, [href])
            return fetched[0].etag if fetched else None

        def remember(href: str, etag: str | None, want: Desired) -> None:
            cache.objects[href] = CachedObject(etag, tag, want.object_id, want.content_hash, want.span)

        async def create(want: Desired) -> None:
            href = target.href_for(cal, resource_name(rule.name, want.object_id))
            data = body(want)
            try:
                etag = await target.create(href, data)
            except ConflictError:  # exists already (e.g. local state was lost)
                etag = await target.update(href, await current_etag(href), data)
            remember(href, etag, want)
            result.created += 1

        async def update(want: Desired, have: Actual) -> None:
            data = body(want)
            try:
                etag = await target.update(have.href, have.etag, data)
            except ConflictError:
                etag = await target.update(have.href, await current_etag(have.href), data)
            remember(have.href, etag, want)
            result.updated += 1

        async def delete(have: Actual) -> None:
            try:
                await target.delete(have.href, have.etag)
            except ConflictError:
                await target.delete(have.href, await current_etag(have.href))
            cache.objects.pop(have.href, None)
            result.deleted += 1

        phases: list[list[Coroutine[Any, Any, None]]] = [
            [create(w) for w in p.create],
            [update(w, a) for w, a in p.update],
            [delete(a) for a in p.delete],
        ]
        for jobs in phases:
            for outcome in await asyncio.gather(*jobs, return_exceptions=True):
                if isinstance(outcome, Exception):
                    result.failed += 1
                    result.error = f"{type(outcome).__name__}: {outcome}"
                    if isinstance(outcome, AuthError):
                        result.error_kind = "auth"
                    log.warning("rule %s: write failed: %s", rule.name, outcome)
        if result.failed:
            done = result.created + result.updated + result.deleted
            result.status = "partial" if done else "failed"
            result.error_kind = result.error_kind or "other"

    # ----- the cycle ----------------------------------------------------
    async def cycle(
        self,
        *,
        rules: Sequence[str] | None = None,
        dry_run: bool = False,
        allow_deletes: bool = False,
    ) -> CycleResult:
        started = self._clock()
        mark = self._log.mark() if self._log else 0
        selected = [r for r in self._config.rules if rules is None or r.name in rules]
        results = {r.name: RuleResult(r.name) for r in selected}
        for target in self._targets.values():
            target.reset()

        by_source: dict[str, list[Rule]] = defaultdict(list)
        for rule in selected:
            by_source[rule.source.account].append(rule)
        outcomes = await asyncio.gather(
            *(self._ingest(a, rs) for a, rs in by_source.items()), return_exceptions=True
        )
        for (_, rs), outcome in zip(by_source.items(), outcomes, strict=True):
            if isinstance(outcome, Exception):
                for rule in rs:
                    results[rule.name].fail(outcome)

        by_target: dict[TargetKey, list[Rule]] = defaultdict(list)
        for rule in selected:
            if results[rule.name].status == "ok":
                by_target[(rule.target.account, rule.target.calendar)].append(rule)
        outcomes = await asyncio.gather(
            *(self._detect(k, rs, dry_run) for k, rs in by_target.items()), return_exceptions=True
        )
        for (_, rs), outcome in zip(by_target.items(), outcomes, strict=True):
            if isinstance(outcome, Exception):
                for rule in rs:
                    results[rule.name].fail(outcome)

        async def guarded(rule: Rule) -> None:
            try:
                await self._run_rule(rule, results[rule.name], dry_run=dry_run, allow_deletes=allow_deletes)
            except Exception as exc:  # isolate rules from each other
                log.exception("rule %s failed", rule.name)
                results[rule.name].fail(exc)

        await asyncio.gather(*(guarded(r) for r in selected if results[r.name].status == "ok"))

        if not dry_run:
            touched = {
                (r.target.account, self._calendars[(r.target.account, r.target.calendar)].href)
                for r in selected
                if (r.target.account, r.target.calendar) in self._calendars
            }
            for account, href in touched:
                cache = self._caches.get((account, href))
                if cache is not None:  # detection may have failed before the cache existed
                    self._store.save_target_cache(account, href, cache)

        finished = self._clock()
        requests = (self._log.mark() - mark) if self._log else 0
        if not dry_run:
            self._store.record_runs(
                [
                    RunRecord(
                        r.rule,
                        started,
                        finished,
                        r.status,
                        r.created,
                        r.updated,
                        r.deleted,
                        r.failed,
                        requests,
                        r.error,
                    )
                    for r in results.values()
                ]
            )
        return CycleResult(started, finished, results, requests)
