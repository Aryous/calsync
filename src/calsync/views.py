"""Read-only views over the state database: what the CLI ``--json`` commands, the local API and
the web UI show.

``StateReader`` opens the database read-only and never writes, creates or migrates anything. It
reads databases written by any calsync version: each query checks which tables and columns exist
and degrades (a missing ``change`` table means no changes; missing run columns read as 0 / None).

The JSON shapes built here are a contract shared by every consumer: keep the keys stable.
"""

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Literal
from urllib.parse import quote, urlparse

from calsync.config import Account, Config, Rule
from calsync.errors import ConfigError
from calsync.model import (
    Instance,
    When,
    Window,
    apply_alarm_policy,
    apply_field_policy,
    expand_instances,
    series_hash,
    series_span,
)
from calsync.naming import object_id, rule_tag
from calsync.store import SCHEMA_VERSION

Health = Literal["ok", "failing", "blocked", "auth", "never_run"]
Delivery = Literal["delivered", "pending", "unknown"]
# Worst first: the summary health is the first of these any rule has.
_HEALTH_ORDER: tuple[Health, ...] = ("auth", "blocked", "failing", "never_run", "ok")
NO_STATE_MESSAGE = "no state yet: run `calsync run` or start the daemon"


class StateUnavailable(Exception):
    """There is no state database (yet)."""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_time(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ----- reader ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunView:
    id: int
    rule: str
    started_at: datetime
    finished_at: datetime
    status: str
    created: int
    updated: int
    deleted: int
    failed: int
    reverted: int
    protected: int
    out_of_window: int
    requests: int
    error: str | None
    error_kind: str | None

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at) / timedelta(milliseconds=1))

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule": self.rule,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_ms": self.duration_ms,
            "status": self.status,
            "created": self.created,
            "updated": self.updated,
            "deleted": self.deleted,
            "failed": self.failed,
            "reverted": self.reverted,
            "protected": self.protected,
            "out_of_window": self.out_of_window,
            "requests": self.requests,
            "error": self.error,
            "error_kind": self.error_kind,
        }


@dataclass(frozen=True, slots=True)
class ChangeView:
    id: int
    run_id: int
    rule: str
    object_id: str
    action: str
    at: datetime
    title: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "rule": self.rule,
            "object_id": self.object_id,
            "action": self.action,
            "at": self.at.isoformat(),
            "title": self.title,
        }


_RUN_COLUMNS: tuple[str, ...] = (
    "id",
    "rule",
    "started_at",
    "finished_at",
    "status",
    "created",
    "updated",
    "deleted",
    "failed",
    "reverted",
    "protected",
    "out_of_window",
    "requests",
    "error",
    "error_kind",
)
_RUN_DEFAULTS: Mapping[str, Any] = {"reverted": 0, "protected": 0, "out_of_window": 0, "error_kind": None}


class StateReader:
    """A read-only connection to the state database."""

    def __init__(self, db_path: Path) -> None:
        if not db_path.is_file():
            raise StateUnavailable(str(db_path))
        # Quoted: a path with % ? or # would otherwise be read as URI syntax.
        uri = f"file:{quote(str(db_path), safe='/')}?mode=ro"
        if not Path(f"{db_path}-wal").exists():
            # Everything committed is in the main file. A plain read-only open of a WAL database
            # would create -wal and -shm files next to it; immutable reads it without touching
            # the directory (the writer changes the main file only when it checkpoints).
            uri += "&immutable=1"
        try:
            self._db = self._connect(uri)
        except sqlite3.OperationalError:
            if uri.endswith("&immutable=1"):
                raise
            # A WAL database whose -shm file cannot be created (a read-only directory): read the
            # main file as it is; changes still in the -wal file are not seen then.
            self._db = self._connect(uri + "&immutable=1")
        self._columns: dict[str, frozenset[str]] = {}

    @staticmethod
    def _connect(uri: str) -> sqlite3.Connection:
        db = sqlite3.connect(uri, uri=True)
        try:
            db.execute("SELECT count(*) FROM sqlite_master").fetchone()  # opening is lazy
        except BaseException:
            db.close()
            raise
        return db

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "StateReader":
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def columns(self, table: str) -> frozenset[str]:
        """The table's column names; empty if there is no such table."""
        if table not in self._columns:
            rows = self._db.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchall()
            self._columns[table] = frozenset(r[0] for r in rows)
        return self._columns[table]

    def schema_version(self) -> int | None:
        if not {"key", "value"} <= self.columns("meta"):
            return None
        row = self._db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        try:
            return int(row[0]) if row else None
        except (TypeError, ValueError):
            return None

    def schema_current(self) -> bool:
        return self.schema_version() == SCHEMA_VERSION

    # ----- runs and changes ---------------------------------------------------
    def _run_rows(self, where: str, params: Sequence[Any], limit: int | None) -> list[RunView]:
        present = self.columns("run")
        if not present:
            return []
        select = ", ".join(c if c in present else "NULL" for c in _RUN_COLUMNS)
        sql = f"SELECT {select} FROM run {where} ORDER BY id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        result = []
        for row in self._db.execute(sql, params):
            values: dict[str, Any] = dict(zip(_RUN_COLUMNS, row, strict=True))
            for name, default in _RUN_DEFAULTS.items():
                if values[name] is None:
                    values[name] = default
            values["started_at"] = _parse_time(values["started_at"])
            values["finished_at"] = _parse_time(values["finished_at"])
            result.append(RunView(**values))
        return result

    def runs(self, rule: str | None = None, limit: int | None = None) -> list[RunView]:
        """Runs, newest first."""
        if rule is None:
            return self._run_rows("", (), limit)
        return self._run_rows("WHERE rule = ?", (rule,), limit)

    def last_ok_run(self, rule: str) -> RunView | None:
        rows = self._run_rows("WHERE rule = ? AND status = 'ok'", (rule,), 1)
        return rows[0] if rows else None

    def statuses(self, rule: str) -> Iterator[tuple[str, int]]:
        """The rule's run (status, failed) pairs, newest first."""
        if not {"rule", "status", "failed", "id"} <= self.columns("run"):
            return iter(())
        return (
            (r[0], r[1])
            for r in self._db.execute(
                "SELECT status, failed FROM run WHERE rule = ? ORDER BY id DESC", (rule,)
            )
        )

    def changes(self, rule: str | None = None, run_id: int | None = None) -> Iterator[ChangeView]:
        """Changes, newest first: of ``rule`` and of the run ``run_id`` when given (a missing
        ``change`` table: none)."""
        if not {"id", "run_id", "rule", "object_id", "action", "at", "title"} <= self.columns("change"):
            return
        where: list[str] = []
        params: list[Any] = []
        if rule is not None:
            where.append("rule = ?")
            params.append(rule)
        if run_id is not None:
            where.append("run_id = ?")
            params.append(run_id)
        sql = "SELECT id, run_id, rule, object_id, action, at, title FROM change"
        if where:
            sql += " WHERE " + " AND ".join(where)
        for cid, rid, name, oid, action, at, title in self._db.execute(sql + " ORDER BY id DESC", params):
            yield ChangeView(cid, rid, name, oid, action, _parse_time(at), title)

    def blocked_count(self, rule: str) -> int:
        """Deletions in the most recently recorded blocked set (0: none recorded).

        Blocked rows are written only when the blocked set changes, so the latest blocked run
        may have none: the newest set is the rows sharing the greatest ``run_id``."""
        if not {"run_id", "rule", "action"} <= self.columns("change"):
            return 0
        row = self._db.execute(
            "SELECT COUNT(*) FROM change WHERE rule = ? AND action = 'blocked' AND run_id = "
            "(SELECT MAX(run_id) FROM change WHERE rule = ? AND action = 'blocked')",
            (rule, rule),
        ).fetchone()
        return int(row[0])

    # ----- source and target state ----------------------------------------------
    def source_state(self, account: str) -> dict[str, Any]:
        if not {"account", "state"} <= self.columns("source_state"):
            return {}
        row = self._db.execute("SELECT state FROM source_state WHERE account = ?", (account,)).fetchone()
        try:
            state = json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def source_events(self, account: str, calendar: str) -> list[dict[str, Any]]:
        if not {"account", "calendar", "raw"} <= self.columns("source_event"):
            return []
        events = []
        for (raw,) in self._db.execute(
            "SELECT raw FROM source_event WHERE account = ? AND calendar = ?", (account, calendar)
        ):
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def target_calendar_name(self, account: str, ref: str) -> str | None:
        if not {"account", "ref", "name"} <= self.columns("target_calendar"):
            return None
        row = self._db.execute(
            "SELECT name FROM target_calendar WHERE account = ? AND ref = ?", (account, ref)
        ).fetchone()
        return row[0] if row else None

    def copies(self, tag: str) -> int:
        if "rule_tag" not in self.columns("target_object"):
            return 0
        return int(
            self._db.execute("SELECT COUNT(*) FROM target_object WHERE rule_tag = ?", (tag,)).fetchone()[0]
        )

    def delivered_hashes(self, tag: str) -> dict[str, str | None]:
        """Object id → recorded content hash of the rule's copies; ``None`` while a revert is pending."""
        present = self.columns("target_object")
        if not {"rule_tag", "object_id", "content_hash"} <= present:
            return {}
        reverting = "reverting" if "reverting" in present else "0"
        return {
            oid: None if rev else content_hash
            for oid, content_hash, rev in self._db.execute(
                f"SELECT object_id, content_hash, {reverting} FROM target_object WHERE rule_tag = ?", (tag,)
            )
        }


# ----- shared helpers -------------------------------------------------------------


def host_for(provider: str, server_url: str | None) -> str:
    if provider == "google":
        return "Google Calendar"
    url = server_url or ""
    if url == "icloud" or "icloud.com" in url.lower():  # also icloud.com.cn
        return "iCloud"
    return urlparse(url).hostname or url


def _schema_json(reader: StateReader) -> dict[str, Any]:
    return {"version": reader.schema_version(), "current": reader.schema_current()}


def _source_calendar(reader: StateReader, rule: Rule) -> tuple[str | None, dict[str, Any]]:
    """The rule's source calendar id (None: not resolvable from the state) and its meta entry."""
    from calsync.sources.google import resolve_calendar_ref

    meta = reader.source_state(rule.source.account).get("meta")
    if not isinstance(meta, dict):
        meta = {}
    try:
        cid = resolve_calendar_ref(rule.source.calendar, meta)
    except ConfigError:
        return None, {}
    entry = meta.get(cid)
    return cid, entry if isinstance(entry, dict) else {}


# ----- status ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountView:
    name: str
    provider: str
    host: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "provider": self.provider, "host": self.host}


@dataclass(frozen=True, slots=True)
class Attention:
    kind: Literal["auth", "blocked", "failing"]
    rule: str
    account: str | None
    message: str
    command: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "rule": self.rule,
            "account": self.account,
            "message": self.message,
            "command": self.command,
        }


@dataclass(frozen=True, slots=True)
class RuleStatus:
    rule: Rule
    source_name: str
    target_name: str
    health: Health
    last_run: RunView | None
    last_ok_at: datetime | None
    consecutive_failures: int
    copies: int

    def to_json(self) -> dict[str, Any]:
        rule = self.rule
        window = rule.window
        scope: dict[str, Any] = (
            {"kind": "all"}
            if window is None
            else {
                "kind": "window",
                "past_days": int(window.past // timedelta(days=1)),
                "future_days": int(window.future // timedelta(days=1)),
            }
        )
        alarms: Any = (
            rule.alarms
            if isinstance(rule.alarms, str)
            else {"minutes_before": [-int(o.total_seconds() // 60) for o in rule.alarms]}
        )
        last = self.last_run
        return {
            "name": rule.name,
            "source": {
                "account": rule.source.account,
                "calendar": rule.source.calendar,
                "calendar_name": self.source_name,
            },
            "target": {
                "account": rule.target.account,
                "calendar": rule.target.calendar,
                "calendar_name": self.target_name,
            },
            "scope": scope,
            "alarms": alarms,
            "fields": {
                "attendees": rule.fields.attendees,
                "conference": rule.fields.conference,
                "content": rule.fields.content,
            },
            "health": self.health,
            "last_run": last.to_json() if last else None,
            "last_ok_at": _iso(self.last_ok_at),
            "consecutive_failures": self.consecutive_failures,
            "copies": self.copies,
            "out_of_window": last.out_of_window if last else 0,
            "protected": last.protected if last else 0,
        }


@dataclass(frozen=True, slots=True)
class StatusView:
    generated_at: datetime
    schema_version: int | None
    schema_current: bool
    health: Health
    last_sync_at: datetime | None
    changes_24h: int
    accounts: tuple[AccountView, ...]
    rules: tuple[RuleStatus, ...]
    attention: tuple[Attention, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "schema": {"version": self.schema_version, "current": self.schema_current},
            "summary": {
                "health": self.health,
                "rules": len(self.rules),
                "attention": len(self.attention),
                "last_sync_at": _iso(self.last_sync_at),
                "changes_24h": self.changes_24h,
            },
            "accounts": [a.to_json() for a in self.accounts],
            "rules": [r.to_json() for r in self.rules],
            "attention": [a.to_json() for a in self.attention],
        }


def _health(run: RunView | None) -> Health:
    if run is None:
        return "never_run"
    if _is_ok(run.status, run.failed):
        return "ok"
    if run.error_kind == "auth" or (run.error_kind is None and (run.error or "").startswith("AuthError")):
        return "auth"
    if run.status == "blocked":
        return "blocked"
    return "failing"


def _worst(present: set[Health]) -> Health:
    for health in _HEALTH_ORDER:
        if health in present:
            return health
    return "ok"


def _is_ok(status: str, failed: int) -> bool:
    """A run whose health is ``ok``: ok, or partial without failed items."""
    return status == "ok" or (status == "partial" and failed == 0)


def _consecutive_failures(reader: StateReader, rule: str) -> int:
    """The newest runs in a row whose health is not ``ok``."""
    count = 0
    for status, failed in reader.statuses(rule):
        if _is_ok(status, failed):
            break
        count += 1
    return count


BLOCKED_ADVICE = "To allow them, stop the daemon, run the command, then start the daemon again."


def _attention(reader: StateReader, status: RuleStatus) -> Attention | None:
    rule = status.rule
    if status.health == "auth":
        error = ((status.last_run and status.last_run.error) or "").lower()
        account = rule.source.account if "google" in error or "oauth" in error else rule.target.account
        return Attention(
            "auth",
            rule.name,
            account,
            f"The {account} account needs to be connected again.",
            f"calsync auth {account}",
        )
    if status.health == "blocked":
        n = reader.blocked_count(rule.name)
        waiting = f"{n} deletions are" if n else "deletions are"
        # While the daemon runs it holds the lock, so the command can only run with it stopped
        # (``approve`` will let the daemon do it).
        return Attention(
            "blocked",
            rule.name,
            None,
            f"Sync paused: {waiting} waiting for your confirmation. {BLOCKED_ADVICE}",
            f"calsync run --rule {rule.name} --allow-deletes",
        )
    if status.health == "failing":
        k = status.consecutive_failures
        return Attention("failing", rule.name, None, f"Sync has failed {k} times in a row.", None)
    return None


def _account_view(account: Account) -> AccountView:
    return AccountView(account.name, account.provider, host_for(account.provider, account.server_url))


def build_status(config: Config, reader: StateReader, now: datetime) -> StatusView:
    rules: list[RuleStatus] = []
    for rule in config.rules:
        runs = reader.runs(rule.name, limit=1)
        last = runs[0] if runs else None
        last_ok = reader.last_ok_run(rule.name)
        _, entry = _source_calendar(reader, rule)
        rules.append(
            RuleStatus(
                rule=rule,
                source_name=entry.get("summary_override") or entry.get("summary") or rule.source.calendar,
                target_name=reader.target_calendar_name(rule.target.account, rule.target.calendar)
                or rule.target.calendar,
                health=_health(last),
                last_run=last,
                last_ok_at=last_ok.finished_at if last_ok else None,
                consecutive_failures=_consecutive_failures(reader, rule.name),
                copies=reader.copies(rule_tag(rule.name)),
            )
        )
    attention = tuple(a for r in rules if (a := _attention(reader, r)) is not None)
    health = _worst({r.health for r in rules})
    ok_times = [r.last_ok_at for r in rules if r.last_ok_at is not None]
    since = now - timedelta(hours=24)
    changes_24h = sum(
        run.created + run.updated + run.deleted
        for rule in config.rules
        for run in reader.runs(rule.name)
        if since <= run.finished_at <= now
    )
    return StatusView(
        generated_at=now,
        schema_version=reader.schema_version(),
        schema_current=reader.schema_current(),
        health=health,
        last_sync_at=max(ok_times) if ok_times else None,
        changes_24h=changes_24h,
        accounts=tuple(_account_view(a) for a in config.accounts.values()),
        rules=tuple(rules),
        attention=attention,
    )


# ----- upcoming -------------------------------------------------------------------------


def _when_json(when: When) -> str:
    return when.value.isoformat()


@dataclass(frozen=True, slots=True)
class UpcomingEvent:
    rule: str
    instance: Instance
    target_calendar: str
    delivery: Delivery

    @property
    def alarms_minutes_before(self) -> list[int]:
        return sorted((-int(a.offset.total_seconds() // 60) for a in self.instance.alarms), reverse=True)

    def to_json(self) -> dict[str, Any]:
        i = self.instance
        return {
            "rule": self.rule,
            "start": _when_json(i.start),
            "end": _when_json(i.end),
            "all_day": i.start.all_day,
            "title": i.title,
            "location": i.location,
            "alarms_minutes_before": self.alarms_minutes_before,
            "recurring": i.recurring,
            "target_calendar": self.target_calendar,
            "delivery": self.delivery,
        }


@dataclass(frozen=True, slots=True)
class UpcomingView:
    start: datetime
    end: datetime
    hours: int
    schema_version: int | None
    schema_current: bool
    events: tuple[UpcomingEvent, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "hours": self.hours,
            "schema": {"version": self.schema_version, "current": self.schema_current},
            "events": [e.to_json() for e in self.events],
        }


def _rule_events(
    rule: Rule, reader: StateReader, now: datetime, end: datetime, current: bool
) -> list[UpcomingEvent]:
    """The rule's instances in ``[now, end)``, shaped exactly as ``Engine._desired`` shapes them."""
    from calsync.sources.google import meta_from_state
    from calsync.sources.google_map import build_series

    cid, entry = _source_calendar(reader, rule)
    if cid is None:
        return []
    raw = reader.source_events(rule.source.account, cid)
    tag = rule_tag(rule.name)
    delivered = reader.delivered_hashes(tag) if current else {}
    target_name = (
        reader.target_calendar_name(rule.target.account, rule.target.calendar) or rule.target.calendar
    )
    window = None if rule.window is None else Window.around(now, rule.window.past, rule.window.future)
    events: list[UpcomingEvent] = []
    for item in build_series(raw, meta_from_state(entry)):
        shaped = apply_field_policy(apply_alarm_policy(item, rule.alarms), rule.fields)
        if window is not None and not window.intersects(series_span(shaped)):
            continue
        delivery: Delivery = "unknown"
        if current:
            recorded = delivered.get(object_id(rule.name, cid, item.source_id))
            delivery = "delivered" if recorded is not None and recorded == series_hash(shaped) else "pending"
        events.extend(
            UpcomingEvent(rule.name, inst, target_name, delivery)
            for inst in expand_instances(shaped, now, end)
        )
    return events


def _event_order(event: UpcomingEvent) -> tuple[datetime, str, str]:
    return (event.instance.start.utc(), event.rule, event.instance.title)


def build_upcoming(
    config: Config, reader: StateReader, now: datetime, hours: int = 24, rule: str | None = None
) -> UpcomingView:
    end = now + timedelta(hours=hours)
    current = reader.schema_current()  # hashes from another calsync version are not comparable
    events = [
        event
        for r in config.rules
        if rule is None or r.name == rule
        for event in _rule_events(r, reader, now, end, current)
    ]
    return UpcomingView(
        start=now,
        end=end,
        hours=hours,
        schema_version=reader.schema_version(),
        schema_current=current,
        events=tuple(sorted(events, key=_event_order)),
    )


# ----- runs and changes --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunsView:
    runs: tuple[RunView, ...]

    def to_json(self) -> dict[str, Any]:
        return {"runs": [r.to_json() for r in self.runs]}


@dataclass(frozen=True, slots=True)
class ChangesView:
    changes: tuple[ChangeView, ...]

    def to_json(self) -> dict[str, Any]:
        return {"changes": [c.to_json() for c in self.changes]}


def build_runs(config: Config, reader: StateReader, rule: str | None = None, limit: int = 50) -> RunsView:
    """Recorded runs, newest first (also of rules no longer configured: history is history)."""
    return RunsView(tuple(reader.runs(rule, limit=limit)))


def build_changes(
    config: Config,
    reader: StateReader,
    rule: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
    run_id: int | None = None,
) -> ChangesView:
    """Recorded changes, newest first; ``since`` keeps those at or after that instant, ``run_id``
    those of that run only."""
    found: list[ChangeView] = []
    for change in reader.changes(rule, run_id):
        if len(found) >= limit:
            break
        if since is None or change.at >= since:
            found.append(change)
    return ChangesView(tuple(found))
