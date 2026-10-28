"""Operations that write Google events: create, edit, delete (and, in ``event_apply``, a batch).

calsync stays one-way: these change Google's originals, and the next cycle brings every copy
in line (each successful write requests that cycle). Rules of the house:

- An edit or delete reads the live event first and refuses (``WriteConflict``) when it is not
  the version the caller saw (``expect``: its etag, or ``expect_updated``: its ``updated``);
  the write itself carries ``If-Match``, so a change in between is refused too (412).
- Read-only calendars and invitations (the account does not organise the event) are refused
  before any request; the web UI greys them out.
- A grant without the write scope: the daemon's adapter is rebuilt once (a credential stored
  by ``calsync auth --write`` meanwhile is picked up), then ``needs_human``.
- Only a delete made in the web UI (origin ``ui``) pre-approves its copies' deletes; deletes by
  the CLI or the API (an agent, a script) meet the mass-delete guard.
- Summaries name accounts, calendars and counts, never a title; titles go in ``data`` and the
  write history (``event_write``), as in the change log.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from calsync import events_view, views
from calsync.config import Config
from calsync.durations import parse_duration
from calsync.errors import ConfigError, RulePaused
from calsync.model import When
from calsync.naming import object_id
from calsync.ops import DRY_RUN, OpContext, OpResult, _adapters, _Args, _config_now, _ran, _wrote
from calsync.sources.google_map import parse_when
from calsync.sources.google_write import (
    EventFields,
    GoogleWriter,
    NoWriteAccess,
    WriteConflict,
    check_fields,
    field_changes,
    from_resource,
    to_resource,
)
from calsync.store import WriteRecord
from calsync.wiring import state_paths

WRITE_TIMEOUT = 60.0  # seconds: one operation's requests to Google, all together
READ_ONLY_ROLES = frozenset({"reader", "freeBusyReader"})
WRITE_REASON = "a person allows calsync to change events in Google (one consent page)"
FIELD_ARGS = {"title", "start", "end", "all_day", "location", "description", "alarms"}


def needs_write_access(account: str) -> dict[str, str]:
    return {"command": f"calsync auth {account} --write", "reason": WRITE_REASON}


# ----- arguments ---------------------------------------------------------------------------------


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_moment(op: str, key: str, text: str, *, all_day: bool | None, tz: str) -> When:
    """``2026-10-25`` (all day), ``2026-10-25T09:30`` (in ``tz``) or with an offset."""
    text = text.strip()
    try:
        if len(text) == 10 and all_day is not False:
            return When(date.fromisoformat(text))
        if all_day:
            raise ValueError
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        expected = "a date such as 2026-10-25" if all_day else "a time such as 2026-10-25T09:30"
        raise ConfigError([f'{op}: {key}: expected {expected}, got "{text}"']) from None
    zone = _zone(tz)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return When(moment.astimezone(zone), tz)


def parse_alarms(op: str, value: Any) -> tuple[tuple[int, ...] | None, bool]:
    """(minutes before, the calendar's default) from ``"default"``, ``""`` / ``"none"``, a list of
    minutes before (``[10, 0]``) or a comma list of offsets as rules write them (``"-10m,0m"``)."""
    if isinstance(value, str):
        text = value.strip()
        if text == "default":
            return None, True
        if text in ("", "none"):
            return (), False
        items: list[Any] = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(value, list):
        items = list(value)
    else:
        raise ConfigError([f'{op}: "alarms" must be "default", "none", minutes before or offsets'])
    minutes: list[int] = []
    for item in items:
        if isinstance(item, bool):
            raise ConfigError([f'{op}: alarms: expected minutes before the start, got "{item}"'])
        if isinstance(item, int):
            minutes.append(item)
            continue
        text = str(item)
        try:
            minutes.append(int(text))
            continue
        except ValueError:
            pass
        try:
            offset = parse_duration(text)
        except ValueError:
            raise ConfigError([f'{op}: alarms: expected e.g. -10m or 10, got "{text}"']) from None
        minutes.append(-int(offset.total_seconds() // 60))
    return tuple(minutes), False


def parse_fields(op: str, a: _Args, args: Mapping[str, Any], *, tz: str, insert: bool) -> EventFields:
    """The fields ``args`` set (an insert: ``title``, ``start`` and ``end`` are required)."""
    all_day: bool | None = a.flag("all_day") if "all_day" in args else None
    start_text, end_text = a.opt_text("start"), a.opt_text("end")
    if insert and (start_text is None or end_text is None):
        raise ConfigError([f'{op}: "start" and "end" are required'])
    start = parse_moment(op, "start", start_text, all_day=all_day, tz=tz) if start_text is not None else None
    end = parse_moment(op, "end", end_text, all_day=all_day, tz=tz) if end_text is not None else None
    alarms: tuple[int, ...] | None = None
    alarms_default = False
    if "alarms" in args and args["alarms"] is not None:
        alarms, alarms_default = parse_alarms(op, args["alarms"])
    title = a.opt_text("title")
    if insert and title is None:
        raise ConfigError([f'{op}: "title" is required'])
    repeat = args.get("repeat")
    if repeat is not None and (not isinstance(repeat, list) or not all(isinstance(r, str) for r in repeat)):
        raise ConfigError([f'{op}: "repeat" must be a list of lines such as "RRULE:FREQ=DAILY;COUNT=10"'])
    fields = EventFields(
        title=title,
        start=start,
        end=end,
        location=a.opt_text("location"),
        description=a.opt_text("description"),
        alarms=alarms,
        alarms_default=alarms_default,
        key=a.opt_text("key"),
        repeat=tuple(repeat) if repeat else None,
    )
    if problems := check_fields(fields):
        raise ConfigError([f"{op}: {p}" for p in problems])
    return fields


def _fields_json(fields: EventFields) -> dict[str, Any]:
    def when(w: When | None) -> str | None:
        return None if w is None else w.value.isoformat()

    return {
        "title": fields.title,
        "start": when(fields.start),
        "end": when(fields.end),
        "all_day": None if fields.start is None else fields.start.all_day,
        "location": fields.location,
        "alarms": "default" if fields.alarms_default else list(fields.alarms or ()),
        "key": fields.key,
    }


# ----- where the event is ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """The event a write names, as the mirror and the calendar cache know it."""

    located: events_view.Located
    role: str | None  # the calendar's cached accessRole
    master: dict[str, Any] | None  # the raw series (or single event) from the mirror
    override: dict[str, Any] | None  # the raw exception of the occurrence, if it has one
    title: str


def _same_start(raw: Mapping[str, Any], when: When, tz: str) -> bool:
    try:
        start = parse_when(raw["start"], tz)
    except (KeyError, ValueError, TypeError):
        return False
    return start.all_day == when.all_day and start.utc() == when.utc()


def _role(reader: views.StateReader, account: str, cid: str) -> str | None:
    return next((c.role for c in reader.account_calendars(account) if c.id == cid), None)


def locate(ctx: OpContext, config: Config, eid: str) -> Target:
    """The event ``eid`` names. ``ConfigError`` when there is none (any more)."""
    try:
        with views.StateReader(state_paths(ctx.state_dir)[0]) as reader:
            found = events_view.locate(config, reader, eid, ctx.clock())
            raws = reader.source_events(found.account, found.cid)
            role = _role(reader, found.account, found.cid)
    except (events_view.NotFound, views.StateUnavailable):
        raise ConfigError(["no such event (it may have been deleted or moved in Google meanwhile)"]) from None
    master = next((r for r in raws if r.get("id") == found.source_id), None)
    override = None
    if found.instance_start is not None:
        override = next(
            (
                r
                for r in raws
                if r.get("recurringEventId") == found.source_id
                and r.get("status") != "cancelled"
                and _same_start(r, found.instance.start, found.timezone)
            ),
            None,
        )
    return Target(found, role, master, override, found.instance.title)


def refuse_unwritable(target: Target) -> None:
    """Read-only calendars and invitations, refused before any request."""
    if target.role in READ_ONLY_ROLES:
        raise ConfigError(["this calendar is read-only for the account: calsync cannot change its events"])
    organizer = (target.master or {}).get("organizer")
    if isinstance(organizer, dict) and organizer and not organizer.get("self"):
        raise ConfigError(["an invitation: only its organiser can change it"])


# ----- the writer ----------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _writer(ctx: OpContext, config: Config, account: str) -> AsyncIterator[GoogleWriter]:
    from calsync.sources.google import GoogleSource

    async with _adapters(ctx, config, {account}) as adapters:
        source = adapters.adapter(account)  # its setup error, if any
        if not isinstance(source, GoogleSource):
            raise ConfigError([f'account "{account}" is not a Google account'])
        yield source.writer()


async def with_writer[T](
    ctx: OpContext,
    config: Config,
    account: str,
    work: Callable[[GoogleWriter], Awaitable[T]],
    *,
    limit: float = WRITE_TIMEOUT,
) -> T:
    """Run ``work`` with the account's writer, at most ``limit`` seconds. On ``NoWriteAccess``
    the daemon's adapter is rebuilt once from the stored credential and ``work`` runs again;
    the scopes the writer saw are stored either way."""
    for attempt in (1, 2):
        try:
            async with _writer(ctx, config, account) as writer:
                try:
                    return await asyncio.wait_for(work(writer), limit)
                finally:
                    if writer.scopes:
                        ctx.store.save_scopes(account, writer.scopes, ctx.clock())
        except NoWriteAccess:
            if attempt == 2 or ctx.daemon is None:
                raise
            await ctx.daemon.rebuild({account})
    raise AssertionError("unreachable")


def no_write_access(op: str, account: str) -> OpResult:
    return OpResult(
        op,
        False,
        f'account "{account}" has no write access to Google Calendar yet',
        data={"status": "no_write_access", "account": account},
        needs_human=needs_write_access(account),
    )


# ----- after a write ---------------------------------------------------------------------------------


def syncing_rules(ctx: OpContext, config: Config, account: str, cid: str) -> list[str]:
    """The rules that sync ``account``'s calendar ``cid`` (by the mirror's names), in config order."""
    from calsync.sources.google import resolve_calendar_ref

    try:
        with views.StateReader(state_paths(ctx.state_dir)[0]) as reader:
            meta = reader.source_state(account).get("meta") or {}
    except views.StateUnavailable:
        meta = {}
    names: list[str] = []
    for rule in config.rules:
        if rule.source.account != account or rule.retire:
            continue
        try:
            if resolve_calendar_ref(rule.source.calendar, meta) == cid:
                names.append(rule.name)
        except ConfigError:
            continue
    return names


async def follow(
    ctx: OpContext, config: Config, rules: Sequence[str], result: OpResult, *, wait: bool
) -> OpResult:
    """Request the cycle that brings the copies in line (``wait``: answer with it). In-process
    (no daemon) the cycle runs here, as ``trigger`` does."""
    from calsync import ops

    by_name = {r.name: r for r in config.rules}
    runnable = [r for r in rules if r in by_name and not by_name[r].paused]
    if not runnable:
        return result
    if ctx.daemon is None:
        cycle, removed = await ops._cycle_here(ctx, config, runnable)  # pyright: ignore[reportPrivateUsage]
        data = {**result.data, "cycle": _ran(cycle)}
        summary = "; ".join([result.summary, "the copies were synced here", *removed])
        return OpResult(result.op, result.changed or _wrote(cycle), summary, data=data)
    waiter = None
    for i, name in enumerate(runnable):
        try:
            if wait and i == len(runnable) - 1:
                waiter = ctx.daemon.request_cycle(name, wait=True)
            else:
                ctx.daemon.request_cycle(name)
        except (RulePaused, ValueError):
            continue
    if waiter is not None:
        future = waiter

        async def finish() -> OpResult:
            try:
                await asyncio.wait({future}, timeout=ops.TRIGGER_TIMEOUT)
            finally:
                if not future.done():
                    future.cancel()
            if future.cancelled():
                return OpResult(
                    result.op,
                    result.changed,
                    result.summary,
                    data={**result.data, "cycle": {"ran": False, "reason": "timeout"}},
                )
            cycle = future.result()
            if cycle is None:
                return OpResult(
                    result.op,
                    result.changed,
                    result.summary,
                    data={**result.data, "cycle": {"ran": False, "reason": "cycle_failed"}},
                )
            return OpResult(
                result.op, result.changed, result.summary, data={**result.data, "cycle": _ran(cycle)}
            )

        ctx.followup = finish
    return OpResult(
        result.op, result.changed, result.summary, data={**result.data, "cycle": {"requested": runnable}}
    )


def record(
    ctx: OpContext,
    op: str,
    account: str,
    cid: str,
    eid: str | None,
    title: str | None,
    detail: Mapping[str, Any],
    status: str = "ok",
) -> None:
    ctx.store.record_write(
        WriteRecord(
            at=ctx.clock(),
            origin="ui" if ctx.source == "ui" else "cli",
            op=op,
            account=account,
            calendar=cid,
            event_id=eid,
            title=title,
            detail=dict(detail),
            status=status,
        )
    )


def _calsync_id(account: str, cid: str, raw: Mapping[str, Any], tz: str) -> str:
    """The calsync event id of a Google event resource as the mirror will list it."""
    if raw.get("recurrence"):
        start = parse_when(raw["start"], tz)
        return events_view.event_id(account, cid, raw["id"], start.value.isoformat())
    if raw.get("recurringEventId"):
        start = parse_when(raw["start"], tz)
        return events_view.event_id(account, cid, raw["recurringEventId"], start.value.isoformat())
    return events_view.event_id(account, cid, raw["id"], None)


def _event_json(raw: Mapping[str, Any], tz: str) -> dict[str, Any]:
    out = _fields_json(from_resource(raw, tz))
    out.update({"source_id": raw.get("id"), "etag": raw.get("etag"), "updated": raw.get("updated")})
    return out


# ----- create ----------------------------------------------------------------------------------------


def _calendar_for(
    ctx: OpContext, config: Config, account: str, ref: str
) -> tuple[str, str | None, str | None]:
    """(calendar id, its cached role, its zone) for ``ref`` (an id or a name) of ``account``."""
    try:
        with views.StateReader(state_paths(ctx.state_dir)[0]) as reader:
            cached = reader.account_calendars(account)
            meta = reader.source_state(account).get("meta") or {}
    except views.StateUnavailable:
        cached, meta = [], {}
    from calsync.sources.google import resolve_calendar_ref

    meta = meta if isinstance(meta, dict) else {}
    match = [c for c in cached if c.id == ref] or [c for c in cached if c.name == ref]
    if len(match) > 1:
        raise ConfigError([f'calendar name "{ref}" is ambiguous; use its id'])
    if match:
        cid, role = match[0].id, match[0].role
    else:
        # Not in the calendar cache (never refreshed): the names the sync's own listing knows.
        try:
            cid = resolve_calendar_ref(ref, meta)
        except ConfigError:
            raise ConfigError(
                [f'calendar "{ref}" not found; run `calsync calendars {account} --refresh`']
            ) from None
        role = None
    entry = meta.get(cid) if isinstance(meta, dict) else None
    tz = entry.get("timezone") if isinstance(entry, dict) else None
    return cid, role, tz


async def event_create(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    """A new event in a Google calendar. ``data.event_id``: calsync's id for it."""
    op = "event.create"
    a = _Args(op, args, {"account", "calendar", "tz", "wait", "repeat", "key", *FIELD_ARGS})
    config = _config_now(ctx)
    account = a.text("account")
    if account not in config.accounts or config.accounts[account].provider != "google":
        raise ConfigError([f'"{account}" is not a configured Google account'])
    cid, role, zone = _calendar_for(ctx, config, account, a.text("calendar"))
    if role in READ_ONLY_ROLES:
        raise ConfigError(["this calendar is read-only for the account: calsync cannot add events to it"])
    tz = a.opt_text("tz") or zone
    if tz is None:
        start = a.opt_text("start") or ""
        if "T" in start and not (start.endswith("Z") or "+" in start[10:] or "-" in start[10:]):
            raise ConfigError(
                [f"{op}: the calendar's time zone is not known yet; give times with an offset or tz"]
            )
        tz = "UTC"
    fields = parse_fields(op, a, args, tz=tz, insert=True)
    rules = syncing_rules(ctx, config, account, cid)
    data: dict[str, Any] = {
        "account": account,
        "calendar": cid,
        "event": _fields_json(fields),
        "rules": rules,
    }
    where = f'account "{account}"'
    if dry_run:
        return OpResult(
            op,
            False,
            f"{DRY_RUN}one event would be created in a calendar of {where}",
            data={**data, "would_change": True},
        )
    try:
        made = await with_writer(
            ctx, config, account, lambda w: w.insert(cid, to_resource(fields, patch=False))
        )
    except NoWriteAccess:
        return no_write_access(op, account)
    eid = _calsync_id(account, cid, made, tz)
    record(ctx, "create", account, cid, eid, fields.title, {"changes": [], "event": _fields_json(fields)})
    result = OpResult(
        op,
        True,
        f"one event was created in a calendar of {where}",
        data={**data, "event_id": eid, "event": _event_json(made, tz)},
    )
    return await follow(ctx, config, rules, result, wait=a.flag("wait"))


# ----- edit and delete ---------------------------------------------------------------------------------


def _scope(op: str, a: _Args, target: Target) -> str:
    scope = a.opt_text("scope")
    if target.located.instance_start is None:
        if scope not in (None, "series", "this"):
            raise ConfigError([f'{op}: "scope" must be "this" or "series"'])
        return "single"
    if scope not in ("this", "series"):
        raise ConfigError(
            [f'{op}: a repeating event: "scope" must be "this" (only this one) or "series" (all of them)']
        )
    return scope


async def _live(writer: GoogleWriter, target: Target, scope: str) -> dict[str, Any]:
    """The live Google resource the write changes: the occurrence (``this``) or the series."""
    loc = target.located
    if scope == "this":
        if target.override is not None:
            return await writer.get(loc.cid, target.override["id"])
        assert loc.instance_start is not None
        return await writer.instance(loc.cid, loc.source_id, loc.instance_start)
    return await writer.get(loc.cid, loc.source_id)


def _check_expected(a: _Args, live: Mapping[str, Any]) -> None:
    expect, expect_updated = a.opt_text("expect"), a.opt_text("expect_updated")
    if expect is not None and expect != live.get("etag"):
        raise WriteConflict("the event changed in Google since it was read")
    if expect_updated is not None and _instant(expect_updated) != _instant(live.get("updated")):
        raise WriteConflict("the event changed in Google since it was read")


def _instant(text: Any) -> datetime | str | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text


def _series_shift(fields: EventFields, target: Target, live: Mapping[str, Any], tz: str) -> EventFields:
    """A new time for one occurrence, applied to the whole series: the series' own start moves by
    the same offset, the length follows the occurrence's new length."""
    if fields.start is None or fields.end is None:
        return fields
    old = target.located.instance.start
    if fields.start.all_day != old.all_day:
        raise ConfigError(["a whole series cannot switch between all day and timed; change one occurrence"])
    master_start = parse_when(live["start"], tz)
    length = fields.end.utc() - fields.start.utc() if not fields.start.all_day else None
    if fields.start.all_day:
        first = _as_date(fields.start.value)
        days = first - _as_date(old.value)
        span = _as_date(fields.end.value) - first
        moved_day = _as_date(master_start.value) + days
        new_start, new_end = When(moved_day), When(moved_day + span)
    else:
        assert length is not None and isinstance(master_start.value, datetime)
        shift = fields.start.utc() - old.utc()
        zone = master_start.tzid or tz
        moved = (master_start.value + shift).astimezone(_zone(zone))
        new_start = When(moved, zone)
        new_end = When((moved + length).astimezone(_zone(zone)), zone)
    return EventFields(
        title=fields.title,
        start=new_start,
        end=new_end,
        location=fields.location,
        description=fields.description,
        alarms=fields.alarms,
        alarms_default=fields.alarms_default,
    )


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _keep_zone(fields: EventFields, live: Mapping[str, Any], tz: str) -> EventFields:
    """A timed edit keeps the event's own zone (the form's times are instants)."""
    if fields.start is None or fields.end is None or fields.start.all_day:
        return fields
    zone = (live.get("start") or {}).get("timeZone") or tz
    z = _zone(zone)
    start = When(fields.start.value.astimezone(z), zone)  # type: ignore[union-attr]
    end = When(fields.end.value.astimezone(z), zone)  # type: ignore[union-attr]
    return EventFields(
        title=fields.title,
        start=start,
        end=end,
        location=fields.location,
        description=fields.description,
        alarms=fields.alarms,
        alarms_default=fields.alarms_default,
    )


async def event_edit(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    """Change an event in Google: this occurrence or the whole series (``scope``)."""
    op = "event.edit"
    a = _Args(op, args, {"id", "scope", "expect", "expect_updated", "move", "wait", *FIELD_ARGS})
    config = _config_now(ctx)
    target = locate(ctx, config, a.text("id"))
    loc = target.located
    scope = _scope(op, a, target)
    refuse_unwritable(target)
    fields = parse_fields(op, a, args, tz=loc.timezone, insert=False)
    if all(args.get(k) is None for k in FIELD_ARGS - {"all_day"}):
        raise ConfigError([f"{op}: nothing to change; give at least one field"])
    plan: dict[str, Any] = {}

    async def work(writer: GoogleWriter) -> dict[str, Any] | None:
        live = await _live(writer, target, scope)
        _check_expected(a, live)
        wanted = _series_shift(fields, target, live, loc.timezone) if scope == "series" else fields
        wanted = _keep_zone(wanted, live, loc.timezone)
        before = from_resource(live, loc.timezone)
        plan["changes"] = field_changes(before, wanted)
        plan["before"] = _event_json(live, loc.timezone)
        if dry_run or not plan["changes"]:
            return None
        return await writer.patch(loc.cid, live["id"], to_resource(wanted, patch=True), etag=live["etag"])

    try:
        changed = await with_writer(ctx, config, loc.account, work)
    except NoWriteAccess:
        return no_write_access(op, loc.account)
    where = f'a calendar of account "{loc.account}"'
    what = "the whole series" if scope == "series" else "the event"
    data: dict[str, Any] = {
        "event_id": a.text("id"),
        "scope": scope,
        "changes": plan["changes"],
        "before": plan["before"],
        "rules": list(loc.rules),
    }
    if dry_run:
        n = len(plan["changes"])
        return OpResult(
            op,
            False,
            f"{DRY_RUN}{what} in {where} would change ({n} field{'s' if n != 1 else ''})",
            data={**data, "would_change": bool(n)},
        )
    if changed is None:
        return OpResult(op, False, f"{what} in {where} already looks like that: nothing written", data=data)
    kind = "move" if a.flag("move") else "edit"
    title = changed.get("summary") or target.title
    record(ctx, kind, loc.account, loc.cid, a.text("id"), title, {"changes": plan["changes"], "scope": scope})
    data["event"] = _event_json(changed, loc.timezone)
    data["event_id"] = (
        _calsync_id(loc.account, loc.cid, changed, loc.timezone) if scope != "series" else a.text("id")
    )
    result = OpResult(op, True, f"{what} in {where} was changed in Google", data=data)
    return await follow(ctx, config, loc.rules, result, wait=a.flag("wait"))


async def event_delete(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    """Delete an event in Google: this occurrence or the whole series (``scope``)."""
    op = "event.delete"
    a = _Args(op, args, {"id", "scope", "expect", "expect_updated", "wait"})
    config = _config_now(ctx)
    target = locate(ctx, config, a.text("id"))
    loc = target.located
    scope = _scope(op, a, target)
    refuse_unwritable(target)
    seen: dict[str, Any] = {}

    async def work(writer: GoogleWriter) -> None:
        live = await _live(writer, target, scope)
        _check_expected(a, live)
        seen["live"] = live
        if not dry_run:
            await writer.delete(loc.cid, live["id"], etag=live.get("etag"))

    try:
        await with_writer(ctx, config, loc.account, work)
    except NoWriteAccess:
        return no_write_access(op, loc.account)
    where = f'a calendar of account "{loc.account}"'
    what = (
        "this occurrence" if scope == "this" else ("the whole series" if scope == "series" else "the event")
    )
    data: dict[str, Any] = {"event_id": a.text("id"), "scope": scope, "rules": list(loc.rules)}
    if dry_run:
        return OpResult(
            op, False, f"{DRY_RUN}{what} in {where} would be deleted", data={**data, "would_change": True}
        )
    # A delete a person made in the web UI releases its copies' deletes past the guard. One
    # occurrence changes the series' copy (an exception), so there is no copy delete to release.
    if ctx.source == "ui" and scope != "this" and ctx.daemon is not None:
        for rule in loc.rules:
            ctx.daemon.preapprove(rule, frozenset({object_id(rule, loc.cid, loc.source_id)}))
    record(ctx, "delete", loc.account, loc.cid, a.text("id"), target.title, {"changes": [], "scope": scope})
    result = OpResult(op, True, f"{what} in {where} was deleted in Google", data=data)
    return await follow(ctx, config, loc.rules, result, wait=a.flag("wait"))


# ----- a batch ---------------------------------------------------------------------------------------

MAX_ITEMS = 2000  # items in one events.apply call
ITEM_ARGS = {"key", "id", "delete", "repeat", *FIELD_ARGS}


@dataclass(slots=True)
class _Item:
    index: int
    key: str | None
    id: str | None  # calsync's event id
    delete: bool
    fields: EventFields
    action: str = ""  # create | update | delete | unchanged | absent
    source_id: str | None = None  # the Google event it writes
    etag: str | None = None
    status: str = "planned"  # planned | ok | failed
    reason: str | None = None
    changes: list[dict[str, Any]] | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"index": self.index, "action": self.action, "status": self.status}
        if self.key is not None:
            out["key"] = self.key
        if self.id is not None:
            out["id"] = self.id
        if self.source_id is not None:
            out["source_id"] = self.source_id
        if self.reason is not None:
            out["reason"] = self.reason
        if self.changes:
            out["changes"] = self.changes
        return out


def _items(op: str, raw: Any, tz: str) -> list[_Item]:
    if not isinstance(raw, list):
        raise ConfigError([f'{op}: "events" must be a list'])
    if len(raw) > MAX_ITEMS:
        raise ConfigError([f"{op}: at most {MAX_ITEMS} events in one call"])
    items: list[_Item] = []
    keys: set[str] = set()
    ids: set[str] = set()
    problems: list[str] = []
    for index, entry in enumerate(raw):
        where = f"{op}: events[{index}]"
        try:
            a = _Args(where, entry, ITEM_ARGS)
            key, eid, delete = a.opt_text("key"), a.opt_text("id"), a.flag("delete")
            if (key is None) == (eid is None):
                raise ConfigError(
                    [f'{where}: give "key" (calsync creates or updates it) or "id" (an existing event)']
                )
            if key is not None:
                if key in keys:
                    raise ConfigError([f'{where}: key "{key}" appears twice'])
                keys.add(key)
            if eid is not None:
                if eid in ids:
                    raise ConfigError([f"{where}: the same id appears twice"])
                ids.add(eid)
            if delete:
                if set(entry) - {"key", "id", "delete"}:
                    raise ConfigError([f"{where}: a delete takes only its key or id"])
                fields = EventFields()
            else:
                insert = key is not None
                fields = parse_fields(where, a, entry, tz=tz, insert=insert)
                if key is not None:
                    fields = EventFields(**{**_as_dict(fields), "key": key})
            items.append(_Item(index, key, eid, delete, fields))
        except ConfigError as exc:
            problems.extend(exc.problems)
    if problems:
        raise ConfigError(problems[:20])
    return items


def _as_dict(fields: EventFields) -> dict[str, Any]:
    return {name: getattr(fields, name) for name in EventFields.__dataclass_fields__}


def _differs(want: EventFields, have: EventFields) -> list[dict[str, Any]]:
    return [
        c
        for c in field_changes(have, want)
        if c["field"] != "description" or want.description != have.description
    ]


async def _current(ctx: OpContext, writer: GoogleWriter, account: str, cid: str) -> dict[str, dict[str, Any]]:
    """The calendar's live events (by Google id): the mirror plus what changed since the last
    cycle (an incremental pull from the stored cursor; the store is left as it is)."""
    from calsync.sources.google import GoogleSource

    state, mirrors = ctx.store.load_source(account)
    events = dict(mirrors.get(cid, {}))
    source = GoogleSource(writer.client, writer.token, base_url=writer.base_url)
    pulled = await source.pull([cid], state)
    if cid in pulled.errors:
        raise pulled.errors[cid]
    pull = pulled.calendars[cid]
    if pull.full:
        events = {}
    for raw in pull.upserts:
        events[raw["id"]] = raw
    for gone in pull.deletes:
        events.pop(gone, None)
    return {k: v for k, v in events.items() if v.get("status") != "cancelled"}


def _plan(
    items: list[_Item], live: Mapping[str, dict[str, Any]], prune: str | None, account: str, cid: str, tz: str
) -> list[_Item]:
    """Each item's action against ``live``; with ``prune``, deletes for the keyed events not named."""
    from calsync.sources.google_write import event_key

    by_key = {k: raw for raw in live.values() if (k := event_key(raw))}
    for item in items:
        if item.key is not None:
            raw = by_key.get(item.key)
        else:
            raw = _by_calsync_id(item, live, account, cid)
            if raw is None:
                item.action, item.status, item.reason = (
                    ("delete" if item.delete else "update"),
                    "failed",
                    "no such event",
                )
                continue
        if item.delete:
            if raw is None:
                item.action = "absent"
                item.status = "ok"
                continue
            item.action, item.source_id, item.etag = "delete", raw["id"], raw.get("etag")
            continue
        if raw is None:
            item.action = "create"
            continue
        item.source_id, item.etag = raw["id"], raw.get("etag")
        item.changes = _differs(item.fields, from_resource(raw, tz))
        item.action = "update" if item.changes else "unchanged"
        if not item.changes:
            item.status = "ok"
    if prune is not None:
        named = {i.key for i in items if i.key is not None}
        for key, raw in sorted(by_key.items()):
            if key.startswith(prune) and key not in named:
                extra = _Item(
                    len(items), key, None, True, EventFields(), "delete", raw["id"], raw.get("etag")
                )
                items.append(extra)
    return items


def _by_calsync_id(
    item: _Item, live: Mapping[str, dict[str, Any]], account: str, cid: str
) -> dict[str, Any] | None:
    """The live event (a single event or a whole series) a calsync id names in this calendar."""
    import base64

    assert item.id is not None
    try:
        text = base64.urlsafe_b64decode(item.id + "=" * (-len(item.id) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    prefix = f"{account}/{cid}/"
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix) :]
    if rest in live:
        return live[rest]
    series = rest.split("@", 1)[0]
    raw = live.get(series)
    return raw if raw is not None and raw.get("recurrence") else None


async def events_apply(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    """Many writes to one calendar in batches: each item creates or updates the event with its
    ``key``, or changes or deletes an existing event by ``id``; ``delete: true`` deletes;
    ``prune_prefix`` deletes the keyed events whose key starts with it and that the list does not
    name. Re-running the same list does only what is left. Never pre-approves copy deletes."""
    from calsync.sources.google_write import BatchCall

    op = "events.apply"
    a = _Args(op, args, {"account", "calendar", "events", "prune_prefix", "tz", "wait"})
    config = _config_now(ctx)
    account = a.text("account")
    if account not in config.accounts or config.accounts[account].provider != "google":
        raise ConfigError([f'"{account}" is not a configured Google account'])
    cid, role, zone = _calendar_for(ctx, config, account, a.text("calendar"))
    if role in READ_ONLY_ROLES:
        raise ConfigError(["this calendar is read-only for the account: calsync cannot change its events"])
    tz = a.opt_text("tz") or zone or "UTC"
    items = _items(op, a.raw("events"), tz)
    prune = a.opt_text("prune_prefix")
    if prune is not None and not prune:
        raise ConfigError([f'{op}: "prune_prefix" must not be empty'])

    async def work(writer: GoogleWriter) -> list[_Item]:
        live = await _current(ctx, writer, account, cid)
        planned = _plan(items, live, prune, account, cid, tz)
        todo = [i for i in planned if i.status == "planned"]
        if dry_run or not todo:
            return planned
        calls: list[BatchCall] = []
        for item in todo:
            if item.action == "create":
                calls.append(BatchCall("POST", cid, body=to_resource(item.fields, patch=False)))
            elif item.action == "update":
                assert item.source_id is not None
                calls.append(
                    BatchCall(
                        "PATCH", cid, item.source_id, to_resource(item.fields, patch=True), etag=item.etag
                    )
                )
            else:
                assert item.source_id is not None
                calls.append(BatchCall("DELETE", cid, item.source_id, etag=item.etag))
        replies = await writer.batch(calls)
        if replies and all(
            r.status == 403 and r.reason() in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
            for r in replies
        ):
            raise NoWriteAccess("no write access")
        for item, reply in zip(todo, replies, strict=True):
            if reply.ok or (item.action == "delete" and reply.gone):
                item.status = "ok"
                if item.action == "create":
                    item.source_id = reply.body.get("id")
            else:
                item.status, item.reason = "failed", reply.reason()
        return planned

    batches = 1 + len(items) // 50
    try:
        done = await with_writer(ctx, config, account, work, limit=WRITE_TIMEOUT * (1 + batches))
    except NoWriteAccess:
        return no_write_access(op, account)
    counts = {k: 0 for k in ("create", "update", "delete", "unchanged", "absent", "failed")}
    for item in done:
        counts["failed" if item.status == "failed" else item.action] += 1
    data: dict[str, Any] = {
        "account": account,
        "calendar": cid,
        "counts": counts,
        "items": [i.to_json() for i in done],
        "rules": syncing_rules(ctx, config, account, cid),
    }
    writes = counts["create"] + counts["update"] + counts["delete"]
    where = f'a calendar of account "{account}"'
    if dry_run:
        summary = f"{DRY_RUN}{_counts_text(counts)} in {where}"
        return OpResult(op, False, summary, data={**data, "would_change": writes > 0})
    wrote = any(i.status == "ok" and i.action in ("create", "update", "delete") for i in done)
    if wrote or counts["failed"]:
        status = "failed" if not wrote else ("partial" if counts["failed"] else "ok")
        record(ctx, "apply", account, cid, None, None, {"counts": counts}, status)
    result = OpResult(op, wrote, f"{_counts_text(counts)} in {where}", data=data)
    if not wrote:
        return result
    return await follow(ctx, config, data["rules"], result, wait=a.flag("wait"))


def _counts_text(counts: Mapping[str, int]) -> str:
    parts = [
        f"{counts[k]} {word}"
        for k, word in (
            ("create", "created"),
            ("update", "updated"),
            ("delete", "deleted"),
            ("unchanged", "unchanged"),
            ("failed", "failed"),
        )
        if counts[k]
    ]
    return ", ".join(parts) or "nothing to do"
