"""One sync cycle: ingest sources, detect target changes, plan, guard, push, record.

The engine keeps mirrors and target caches in memory across cycles (the
daemon case) and persists them after every phase, so a fresh process picks
up exactly where the last one stopped.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine, Mapping, Sequence, Set
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from calsync.config import Config, Rule
from calsync.errors import AuthError, CalsyncError, ConfigError, ConflictError, NotFoundError
from calsync.http import RequestLog
from calsync.mirror import Mirror, apply_pull
from calsync.model import Window, apply_alarm_policy, apply_field_policy, series_hash, series_span
from calsync.naming import object_id, parse_resource_name, resource_name, rule_tag, uid_for
from calsync.planner import Actual, Desired, Plan, check_delete_guard, plan
from calsync.render import content_digest, inspect, render
from calsync.sources.base import CalendarMeta, Source
from calsync.store import ChangeRecord, RunRecord, Store
from calsync.targets.base import (
    CachedObject,
    FetchedObject,
    Target,
    TargetCache,
    TargetCalendar,
    TargetDelta,
)

log = logging.getLogger(__name__)

# Rewrite-loop breaker: a copy reverted REVERT_LIMIT times within REVERT_WINDOW that differs
# again is not reverted for REVERT_HOLD (a server or phone that rewrites what calsync writes
# would otherwise be rewritten every cycle). Source changes are still written meanwhile.
REVERT_LIMIT = 3
REVERT_WINDOW = timedelta(hours=6)
REVERT_HOLD = timedelta(hours=24)

Status = Literal["ok", "partial", "failed", "blocked"]
ErrorKind = Literal["auth", "config", "guard", "other"]


def error_kind_for(exc: BaseException) -> ErrorKind:
    if isinstance(exc, AuthError):
        return "auth"
    if isinstance(exc, ConfigError):
        return "config"
    return "other"


@dataclass(slots=True)
class RuleResult:
    rule: str
    status: Status = "ok"
    created: int = 0
    updated: int = 0
    reverted: int = 0  # copies edited on the target and written back (counted in updated)
    deleted: int = 0
    failed: int = 0
    error: str | None = None
    error_kind: ErrorKind | None = None
    plan: Plan | None = None
    protected: int = 0  # existing target copies held untouched this cycle (series unparsed or degraded)
    degraded: int = 0  # series in the source calendar that are unparsed or degraded, copy or not
    out_of_window: int = 0  # desired series skipped by the rule's time window
    blocked_titles: list[str] = field(default_factory=list)  # planned deletes, for the guard alert
    blocked_titles_complete: bool = True  # False: the lookup failed or some bodies were unreadable
    changes: list[ChangeRecord] = field(default_factory=list)  # carries titles: never log

    def fail(self, exc: BaseException) -> None:
        self.status = "failed"
        self.error = f"{type(exc).__name__}: {exc}"
        self.error_kind = error_kind_for(exc)


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
        self._warned_degraded: set[tuple[str, str]] = set()  # (rule, source id), once per process
        # Last recorded protected / blocked object ids per rule: a set is recorded only when it
        # changes, not every cycle. In memory: a fresh process records the current set once.
        self._last_protected: dict[str, frozenset[str]] = {}
        self._last_blocked: dict[str, frozenset[str]] = {}

    # ----- phase 1: ingest ---------------------------------------------
    async def _ingest(self, account: str, rules: list[Rule]) -> dict[str, Exception]:
        """Pull and persist one source account; return the calendar refs that failed on their own."""
        if account not in self._source_state:
            self._source_state[account], self._mirrors[account] = self._store.load_source(account)
        refs = sorted({r.source.calendar for r in rules})
        pull = await self._sources[account].pull(refs, self._source_state[account])
        errors = dict(pull.errors)
        for ref in refs:
            if ref not in pull.calendars and ref not in errors:
                errors[ref] = CalsyncError(
                    f"source {account!r} returned neither a result nor an error for calendar {ref!r} "
                    "(source contract violation)"
                )
        for ref, exc in errors.items():
            log.warning("source %s calendar %r failed: %s", account, ref, exc)
        # Failed calendars are absent from ``calendars`` and keep their old cursor in ``state``.
        unique = {p.calendar_id: p for p in pull.calendars.values()}
        self._store.save_source(account, pull.state, unique.values())
        self._source_state[account] = pull.state
        mirrors = self._mirrors[account]
        for ref, cal_pull in pull.calendars.items():
            self._resolved[(account, ref)] = cal_pull.calendar_id
            self._meta[(account, cal_pull.calendar_id)] = cal_pull.meta
        for cal_id, cal_pull in unique.items():
            apply_pull(mirrors.setdefault(cal_id, {}), cal_pull)
        return errors

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
        fetched = await target.fetch(cal, need) if need else []
        unread = set(need) - {obj.href for obj in fetched}
        for href in unread:
            # Changed, but its body is unknown: plan as if absent (a create there 412s and
            # becomes an update). The cursor is held below so detection reports it again.
            cache.objects.pop(href, None)
        marker_lost = 0
        for obj in fetched:
            name = parse_resource_name(obj.href)
            if name is None:
                continue
            found = inspect(obj.data)
            known = cache.objects.get(obj.href)
            if found is not None:
                # Every body calsync writes carries the marker: without one, the target dropped it.
                # Another marker is not counted: a copy adopted on a cold start keeps the marker of
                # the version it was written with (adoption does not rewrite it).
                marker_lost += found.content_hash is None
            # The recorded hash is what we last wrote or adopted, whatever marker the target kept
            # in the body: only a copy unknown to us (cold start, lost state) is judged by its
            # marker. An unparseable body has no usable hash: the planner rewrites it.
            if found is None:
                content_hash = None
            elif known is not None:
                content_hash = known.content_hash
            else:
                content_hash = found.content_hash
            # Changed by someone else (or unknown to us): its rule compares it with the source
            # version when it next runs, which need not be this cycle (see _classify_reads).
            cache.objects[obj.href] = CachedObject(
                etag=obj.etag,
                rule_tag=name[0],
                object_id=name[1],
                content_hash=content_hash,
                span=found.span if found else None,
                verify_digest=content_digest(obj.data),
                reverting=known.reverting if known else False,
                revert_times=known.revert_times if known else (),
                hold_until=known.hold_until if known else None,
            )
        if marker_lost:
            # Counts only: tells whether the target keeps the marker (the real-device check).
            log.info(
                "target %s %r: %d copy(ies) read back with no calsync marker; judged by visible content",
                account,
                ref,
                marker_lost,
            )
        if unread:
            # Keep the previous cursor: sync-collection replays (ctag re-lists) these hrefs
            # next cycle; advancing would orphan them until they change on the target again.
            log.warning(
                "target %s %r: %d changed resource(s) could not be read; re-detecting next cycle",
                account,
                ref,
                len(unread),
            )
        else:
            cache.cursor = delta.cursor
        self._store.save_target_cache(account, cal.href, cache)

    # ----- phase 3-5: plan, guard, push --------------------------------
    def _desired(self, rule: Rule) -> tuple[dict[str, Desired], frozenset[str]]:
        """Desired objects, plus the object ids of source series that failed to parse."""
        account = rule.source.account
        source = self._sources[account]
        cal_id = self._resolved[(account, rule.source.calendar)]
        meta = self._meta.get((account, cal_id), CalendarMeta())
        # A missing mirror must fail the rule, never read as "the source is empty".
        raw = self._mirrors[account][cal_id].values()
        series = source.to_series(raw, meta)
        desired: dict[str, Desired] = {}
        for item in series:
            shaped = apply_field_policy(apply_alarm_policy(item, rule.alarms), rule.fields)
            oid = object_id(rule.name, cal_id, item.source_id)
            desired[oid] = Desired(oid, shaped, series_hash(shaped), series_span(shaped))
        unparsed = source.series_ids(raw) - {item.source_id for item in series}
        for sid in sorted(unparsed):
            self._warn_degraded(rule, sid, "could not be parsed")
        for item in series:
            if item.complete:
                self._warned_degraded.discard((rule.name, item.source_id))  # warn again if it recurs
            else:
                self._warn_degraded(rule, item.source_id, "has instances that could not be parsed")
        return desired, frozenset(object_id(rule.name, cal_id, sid) for sid in unparsed)

    def _warn_degraded(self, rule: Rule, source_id: str, why: str) -> None:
        """Once per process per (rule, series); names the source id only, never event content."""
        if (rule.name, source_id) in self._warned_degraded:
            return
        self._warned_degraded.add((rule.name, source_id))
        log.warning(
            "rule %s: source series %s %s; protecting any existing target copy", rule.name, source_id, why
        )

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
        window = (
            Window.everything()
            if rule.window is None
            else Window.around(self._clock(), rule.window.past, rule.window.future)
        )
        desired, unparsed = self._desired(rule)
        incomplete = {oid for oid, want in desired.items() if not want.series.complete}
        result.degraded = len(unparsed) + len(incomplete)
        # Unparsed and degraded series keep an existing copy untouched (no update, no
        # delete); a degraded one without a copy is still created from its parseable part.
        protected = (unparsed | incomplete) & actual.keys()
        result.protected = len(protected)
        if not dry_run and self._last_protected.get(rule.name) != protected:
            self._last_protected[rule.name] = frozenset(protected)
            now = self._clock()
            result.changes.extend(
                ChangeRecord(
                    rule.name, oid, "protected", now, desired[oid].series.title if oid in desired else None
                )
                for oid in sorted(protected)
            )
        if self._classify_reads(rule, cache, actual, desired, protected, window):
            # Saved now (also in a dry run): detection has already consumed the change, so a
            # dry run or a failed write must not forget the edit.
            self._store.save_target_cache(rule.target.account, cal.href, cache)
            # A copy edited on the target has an unknown hash now: planned as an update until
            # a write succeeds (remember() then clears ``reverting``). An adopted copy carries
            # the desired hash and span: nothing to write.
            actual = {
                oid: replace(
                    have,
                    content_hash=cache.objects[have.href].content_hash,
                    span=cache.objects[have.href].span,
                )
                for oid, have in actual.items()
            }
        result.plan = plan(desired, actual, window, frozenset(protected))
        result.out_of_window = result.plan.out_of_window
        # The guard counts only non-protected actuals.
        deletable = len(actual.keys() - protected)
        reason = None if allow_deletes else check_delete_guard(result.plan, deletable, rule.delete_guard)
        if reason:
            result.status, result.error, result.error_kind = "blocked", reason, "guard"
            result.blocked_titles, result.blocked_titles_complete = await self._titles(
                rule, cal, result.plan.delete
            )
            blocked = frozenset(a.object_id for a in result.plan.delete)
            if not dry_run and self._last_blocked.get(rule.name) != blocked:
                self._last_blocked[rule.name] = blocked
                # Complete titles are aligned with the planned deletes; otherwise none is trusted.
                titles: list[str | None] = (
                    list(result.blocked_titles)
                    if result.blocked_titles_complete
                    else [None] * len(result.plan.delete)
                )
                now = self._clock()
                result.changes.extend(
                    ChangeRecord(rule.name, a.object_id, "blocked", now, title)
                    for a, title in zip(result.plan.delete, titles, strict=True)
                )
            return
        if not dry_run:
            self._last_blocked[rule.name] = frozenset()  # a later block of the same set is new again
        if not dry_run and not result.plan.empty:
            await self._push(rule, cal, cache, result.plan, result)

    def _classify_reads(
        self,
        rule: Rule,
        cache: TargetCache,
        actual: Mapping[str, Actual],
        desired: Mapping[str, Desired],
        protected: Set[str],
        window: Window,
    ) -> bool:
        """Classify this rule's copies whose body detection read but no run has judged yet.

        What a person sees decides, never the marker in the body: a copy whose visible content
        equals the version we would write is adopted as current (so a phone that drops or keeps
        an old X-CALSYNC-HASH is never rewritten). A copy that differs although its recorded
        hash (what we last wrote or adopted, see _detect) is current was edited on the target:
        it is marked ``reverting`` (cleared only by a successful write) and counts toward the
        loop breaker. A copy that differs while its recorded hash is outdated or unknown (the
        source changed too, or a cold start read it without our marker) is left to the planner
        (an ordinary update). A hold that has ended turns the copy into one to revert, without
        reading it. Returns whether any cached object changed.
        """
        changed = foreign = adopted = unrenderable = restored = 0
        now = self._clock()
        for oid, have in actual.items():
            obj = cache.objects[have.href]
            if oid in protected:
                continue  # no trustworthy source version now: judge it once it is readable again
            if obj.hold_until is not None and now >= obj.hold_until:
                # The hold ended. The copy was recorded as current while the target kept its own
                # version: treat it as an edit to revert, so the source version is written back
                # (counted as one revert; revert_times was emptied when the hold started).
                obj = replace(obj, content_hash=None, hold_until=None, reverting=True)
                cache.objects[have.href] = obj
                changed += 1
                if obj.verify_digest is None:
                    restored += 1  # with a pending read, the read decides below
            seen = obj.verify_digest
            if seen is None:
                continue
            changed += 1
            want = desired.get(oid)
            # Gone or out of window: the planner decides.
            expected = None
            if want is not None and window.intersects(want.span):
                try:
                    expected = content_digest(
                        render(
                            want.series,
                            uid=uid_for(oid),
                            rule_name=rule.name,
                            content_hash=want.content_hash,
                            now=now,
                        )
                    )
                except Exception:  # its message may quote event content: count it, never log it
                    unrenderable += 1
            if want is None or expected is None:
                # Unreadable to us: never start rewriting on a parser limitation.
                cache.objects[have.href] = replace(obj, verify_digest=None)
            elif seen == expected:
                adopted += obj.content_hash != want.content_hash
                # It shows the source version: nothing to revert, and nothing left to hold.
                cache.objects[have.href] = replace(
                    obj,
                    content_hash=want.content_hash,
                    span=want.span,
                    verify_digest=None,
                    reverting=False,
                    hold_until=None,
                )
            elif obj.reverting or obj.content_hash == want.content_hash:
                # We wrote (or adopted) the source version and the copy shows something else:
                # edited on the target, whatever its marker says. The planner sees an unknown hash
                # and plans an update, unless this copy keeps changing after our writes (then it
                # is held as it is). ``reverting``: edited again before the revert was written.
                held = obj.hold_until is not None and now < obj.hold_until
                recent = [t for t in obj.revert_times if t > now - REVERT_WINDOW]
                if not held and len(recent) >= REVERT_LIMIT:
                    held = True
                    obj = replace(obj, revert_times=(), hold_until=now + REVERT_HOLD)
                    log.warning(
                        "rule %s: object %s was reverted %d times within %s and changed on the target "
                        "again; not reverting it until %s; the source version will be written back then",
                        rule.name,
                        obj.object_id,
                        len(recent),
                        REVERT_WINDOW,
                        obj.hold_until,
                    )
                if held:
                    cache.objects[have.href] = replace(
                        obj,
                        content_hash=want.content_hash,
                        span=want.span,
                        verify_digest=None,
                        reverting=False,
                    )
                else:
                    cache.objects[have.href] = replace(
                        obj, verify_digest=None, content_hash=None, reverting=True
                    )
                    foreign += 1
            else:
                # The source changed since we wrote it (or nothing tells what we wrote): an
                # ordinary update.
                cache.objects[have.href] = replace(obj, verify_digest=None)
        if unrenderable:
            log.warning(
                "rule %s: %d copy(ies) could not be compared with the source version", rule.name, unrenderable
            )
        if adopted:
            # Counts only. Whether the target keeps the marker is logged by _detect.
            log.info(
                "rule %s: %d copy(ies) already show the source version but were recorded with no or "
                "an outdated hash; recorded as current without rewriting",
                rule.name,
                adopted,
            )
        if foreign:
            log.info(
                "rule %s: %d copy(ies) edited on the target; the source version will be written back",
                rule.name,
                foreign,
            )
        if restored:
            log.info(
                "rule %s: the hold on %d copy(ies) ended; each will be brought back in line with the source",
                rule.name,
                restored,
            )
        return changed > 0

    async def _titles(self, rule: Rule, cal: TargetCalendar, deletes: list[Actual]) -> tuple[list[str], bool]:
        """SUMMARY of each planned delete and whether every one was read; only blocked rules pay."""
        try:
            fetched = await self._targets[rule.target.account].fetch(cal, [a.href for a in deletes])
        except Exception as exc:  # the alert loses detail, but the rule stays blocked
            log.warning("rule %s: could not read the titles of planned deletes: %s", rule.name, exc)
            return [], False
        titles: dict[str, str] = {}
        unreadable = 0
        for obj in fetched:
            found = inspect(obj.data)
            unreadable += found is None
            titles[obj.href] = "(unreadable)" if found is None else found.summary or "(untitled)"
        listed = [titles[a.href] for a in deletes if a.href in titles]
        return listed, len(listed) == len(deletes) and not unreadable

    async def _push(
        self,
        rule: Rule,
        cal: TargetCalendar,
        cache: TargetCache,
        p: Plan,
        result: RuleResult,
    ) -> None:
        target = self._targets[rule.target.account]
        tag = rule_tag(rule.name)
        now = self._clock()

        def body(want: Desired) -> bytes:
            try:
                return render(
                    want.series,
                    uid=uid_for(want.object_id),
                    rule_name=rule.name,
                    content_hash=want.content_hash,
                    now=now,
                )
            except Exception:
                # The message may quote event content, and it would reach the log and run history.
                raise CalsyncError(f"could not render object {want.object_id}") from None

        async def current(href: str) -> FetchedObject | None:
            """The resource as it is now; None if it no longer exists."""
            fetched = await target.fetch(cal, [href])
            return fetched[0] if fetched else None

        def remember(href: str, etag: str | None, want: Desired, *, reverted: bool = False) -> None:
            """After a successful write: nothing pending, nothing left to revert. The loop
            breaker's record is kept, plus this write if it was a revert."""
            prior = cache.objects.get(href)
            times = prior.revert_times if prior else ()
            if reverted:
                times = (*(t for t in times if t > now - REVERT_WINDOW), now)
            cache.objects[href] = CachedObject(
                etag,
                tag,
                want.object_id,
                want.content_hash,
                want.span,
                revert_times=times,
                hold_until=prior.hold_until if prior else None,
            )

        # Strict servers answer 412 to If-Match (even "*") for a missing resource, so
        # every conflict re-reads the resource before deciding how to retry once.
        async def create(want: Desired) -> None:
            href = target.href_for(cal, resource_name(rule.name, want.object_id))
            data = body(want)
            try:
                etag = await target.create(href, data)
            except ConflictError:  # exists already (e.g. local state was lost)
                found = await current(href)
                if found is None:
                    etag = await target.create(href, data)
                elif not want.series.complete:
                    # A copy we could not read before: never overwrite it with a degraded
                    # series. Cache it with an unknown hash so it stays protected.
                    cache.objects[href] = CachedObject(found.etag, tag, want.object_id, None, None)
                    result.protected += 1
                    return
                else:
                    etag = await target.update(href, found.etag, data)
            remember(href, etag, want)
            result.created += 1
            result.changes.append(ChangeRecord(rule.name, want.object_id, "created", now, want.series.title))

        async def update(want: Desired, have: Actual) -> None:
            prior = cache.objects.get(have.href)
            reverting = prior is not None and prior.reverting  # may be from an earlier cycle
            data = body(want)
            try:
                etag = await target.update(have.href, have.etag, data)
            except ConflictError:
                found = await current(have.href)
                if found is None:  # deleted behind our back: put it back
                    etag = await target.create(have.href, data)
                else:
                    etag = await target.update(have.href, found.etag, data)
            remember(have.href, etag, want, reverted=reverting)
            result.updated += 1
            result.reverted += reverting
            action = "reverted" if reverting else "updated"
            result.changes.append(ChangeRecord(rule.name, want.object_id, action, now, want.series.title))

        async def delete(have: Actual) -> None:
            # The source event is gone: the last recorded title is the only one left.
            title = self._store.last_title(rule.name, have.object_id)
            try:
                await target.delete(have.href, have.etag)
            except ConflictError:
                found = await current(have.href)
                if found is not None:
                    await target.delete(have.href, found.etag)
            cache.objects.pop(have.href, None)
            result.deleted += 1
            result.changes.append(ChangeRecord(rule.name, have.object_id, "deleted", now, title))

        # Coroutines are created per phase, so a cancelled cycle leaves none un-awaited.
        phases: list[Callable[[], list[Coroutine[Any, Any, None]]]] = [
            lambda: [create(w) for w in p.create],
            lambda: [update(w, a) for w, a in p.update],
            lambda: [delete(a) for a in p.delete],
        ]
        errors: list[Exception] = []
        for jobs in phases:
            for outcome in await asyncio.gather(*jobs(), return_exceptions=True):
                if isinstance(outcome, BaseException):
                    if not isinstance(outcome, Exception):
                        raise outcome
                    errors.append(outcome)
                    log.warning("rule %s: write failed: %s", rule.name, outcome)
        if errors:
            result.failed = len(errors)
            first = errors[0]
            more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
            result.error = f"{type(first).__name__}: {first}{more}"
            done = result.created + result.updated + result.deleted
            result.status = "partial" if done else "failed"
            # The most actionable kind wins: one revoked credential means "run calsync auth".
            kinds = {error_kind_for(e) for e in errors}
            result.error_kind = "auth" if "auth" in kinds else "config" if "config" in kinds else "other"

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
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome
                for rule in rs:
                    results[rule.name].fail(outcome)
                continue
            for rule in rs:
                if rule.source.calendar in outcome:
                    results[rule.name].fail(outcome[rule.source.calendar])

        by_target: dict[TargetKey, list[Rule]] = defaultdict(list)
        for rule in selected:
            if results[rule.name].status == "ok":
                by_target[(rule.target.account, rule.target.calendar)].append(rule)
        outcomes = await asyncio.gather(
            *(self._detect(k, rs, dry_run) for k, rs in by_target.items()), return_exceptions=True
        )
        for (_, rs), outcome in zip(by_target.items(), outcomes, strict=True):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome
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
                        reverted=r.reverted,
                        protected=r.protected,
                        out_of_window=r.out_of_window,
                        error_kind=r.error_kind,
                    )
                    for r in results.values()
                ],
                [c for r in results.values() for c in r.changes],
            )
        return CycleResult(started, finished, results, requests)
