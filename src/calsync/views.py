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
    Series,
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

# A rule's health: ``paused`` (the rule is paused) and ``retiring`` (``retire = true``: its
# copies are being deleted) come from the config and win over the run-based ones.
Health = Literal["ok", "failing", "blocked", "auth", "never_run", "paused", "retiring"]
Delivery = Literal["delivered", "pending", "unknown"]
# Worst first: the summary health is the first of these any rule has (paused and retiring
# count as ok).
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
    error_account: str | None = None  # the account whose adapter or setup failed (schema 5)

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
            "error_account": self.error_account,
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
    detail: str | None = None  # the stored JSON (schema 5): ``{"fields": {name: [old, new]}}``

    def detail_json(self) -> dict[str, Any] | None:
        """The parsed detail; None when there is none or it cannot be read."""
        if self.detail is None:
            return None
        try:
            value = json.loads(self.detail)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "rule": self.rule,
            "object_id": self.object_id,
            "action": self.action,
            "at": self.at.isoformat(),
            "title": self.title,
            "detail": self.detail_json(),
        }


@dataclass(frozen=True, slots=True)
class ConfigChangeView:
    id: int
    at: datetime
    source: str
    summary: str
    before: str | None
    after_fingerprint: str
    undoes: int | None


@dataclass(frozen=True, slots=True)
class AccountCalendarView:
    account: str
    id: str
    name: str
    role: str | None
    fetched_at: datetime


# Change actions that put the source version on the target (what the engine records for writes).
_DELIVERY_ACTIONS = ("created", "updated", "reverted")

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
    "error_account",
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
        present = self.columns("change")
        if not {"id", "run_id", "rule", "object_id", "action", "at", "title"} <= present:
            return
        where: list[str] = []
        params: list[Any] = []
        if rule is not None:
            where.append("rule = ?")
            params.append(rule)
        if run_id is not None:
            where.append("run_id = ?")
            params.append(run_id)
        detail = "detail" if "detail" in present else "NULL"
        sql = f"SELECT id, run_id, rule, object_id, action, at, title, {detail} FROM change"
        if where:
            sql += " WHERE " + " AND ".join(where)
        for cid, rid, name, oid, action, at, title, text in self._db.execute(
            sql + " ORDER BY id DESC", params
        ):
            yield ChangeView(cid, rid, name, oid, action, _parse_time(at), title, text)

    def latest_blocked(self, rule: str) -> tuple[int, list[tuple[str, str | None]]] | None:
        """The most recently recorded blocked set: its run id and (object id, title) rows; None
        if none is recorded.

        Blocked rows are written only when the blocked set changes, so the latest blocked run
        may have none: the newest set is the rows sharing the greatest ``run_id``."""
        if not {"run_id", "rule", "action", "object_id", "title"} <= self.columns("change"):
            return None
        rows = self._db.execute(
            "SELECT run_id, object_id, title FROM change WHERE rule = ? AND action = 'blocked' AND run_id = "
            "(SELECT MAX(run_id) FROM change WHERE rule = ? AND action = 'blocked') ORDER BY id",
            (rule, rule),
        ).fetchall()
        if not rows:
            return None
        return int(rows[0][0]), [(oid, title) for _, oid, title in rows]

    def delivered_at(self, rule: str, object_id: str) -> datetime | None:
        """When the rule last wrote the object to its target (the newest create, update or
        revert); ``None``: never recorded."""
        if not {"id", "rule", "object_id", "action", "at"} <= self.columns("change"):
            return None
        placeholders = ", ".join("?" * len(_DELIVERY_ACTIONS))
        row = self._db.execute(
            f"SELECT at FROM change WHERE rule = ? AND object_id = ? AND action IN ({placeholders}) "
            "ORDER BY id DESC LIMIT 1",
            (rule, object_id, *_DELIVERY_ACTIONS),
        ).fetchone()
        return _parse_time(row[0]) if row else None

    def delivered_times(self, rule: str) -> dict[str, datetime]:
        """``delivered_at`` of every object of the rule, in one query (object id -> time)."""
        if not {"id", "rule", "object_id", "action", "at"} <= self.columns("change"):
            return {}
        placeholders = ", ".join("?" * len(_DELIVERY_ACTIONS))
        found: dict[str, datetime] = {}
        for oid, at in self._db.execute(
            f"SELECT object_id, at FROM change WHERE rule = ? AND action IN ({placeholders}) ORDER BY id",
            (rule, *_DELIVERY_ACTIONS),
        ):
            found[oid] = _parse_time(at)  # ascending: the newest wins
        return found

    def deliveries_since(self, since: datetime) -> int:
        """Change rows that wrote a copy (create, update, revert) at or after ``since``."""
        if not {"action", "at"} <= self.columns("change"):
            return 0
        placeholders = ", ".join("?" * len(_DELIVERY_ACTIONS))
        # Stored in UTC ISO text; the prefilter on the text is a day early (a fraction of a
        # second changes the width), the exact comparison is on the parsed times.
        rough = (since.astimezone(UTC) - timedelta(days=1)).isoformat()
        rows = self._db.execute(
            f"SELECT at FROM change WHERE action IN ({placeholders}) AND at >= ?", (*_DELIVERY_ACTIONS, rough)
        )
        return sum(1 for (at,) in rows if _parse_time(at) >= since)

    # ----- config history and the calendar cache ----------------------------------
    def config_changes(self, limit: int = 50) -> list[ConfigChangeView]:
        """Config writes, newest first (no ``config_change`` table: none)."""
        columns = ("id", "at", "source", "summary", "before", "after_fingerprint", "undoes")
        if not set(columns) <= self.columns("config_change"):
            return []
        rows = self._db.execute(
            f"SELECT {', '.join(columns)} FROM config_change ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [ConfigChangeView(r[0], _parse_time(r[1]), *r[2:]) for r in rows]

    def account_calendars(self, account: str) -> list[AccountCalendarView]:
        """The account's cached calendar list, by id (none cached: empty)."""
        if not {"account", "id", "name", "role", "fetched_at"} <= self.columns("account_calendar"):
            return []
        return [
            AccountCalendarView(account, cid, name, role, _parse_time(fetched_at))
            for cid, name, role, fetched_at in self._db.execute(
                "SELECT id, name, role, fetched_at FROM account_calendar WHERE account = ? ORDER BY id",
                (account,),
            )
        ]

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

    def series_count(self, account: str, calendar: str) -> int:
        """Mirrored source events of a calendar (``calendar``: the resolved Google calendar id)."""
        if not {"account", "calendar"} <= self.columns("source_event"):
            return 0
        row = self._db.execute(
            "SELECT COUNT(*) FROM source_event WHERE account = ? AND calendar = ?", (account, calendar)
        ).fetchone()
        return int(row[0])

    def target_calendar_href(self, account: str, ref: str) -> str | None:
        """The href the target calendar ``ref`` resolved to when it was last synced."""
        if not {"account", "ref", "href"} <= self.columns("target_calendar"):
            return None
        row = self._db.execute(
            "SELECT href FROM target_calendar WHERE account = ? AND ref = ?", (account, ref)
        ).fetchone()
        return row[0] if row else None

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

    def copy_summary(self, tag: str, object_id: str) -> dict[str, Any] | None:
        """What calsync last wrote for the object (the stored summary JSON); ``None``: none
        stored or unreadable."""
        if not {"rule_tag", "object_id", "summary"} <= self.columns("target_object"):
            return None
        row = self._db.execute(
            "SELECT summary FROM target_object WHERE rule_tag = ? AND object_id = ? AND summary IS NOT NULL "
            "LIMIT 1",
            (tag, object_id),
        ).fetchone()
        if row is None:
            return None
        try:
            summary = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return summary if isinstance(summary, dict) else None

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
    return urlparse(url).hostname or HIDDEN  # never the raw text: it may carry a login


HIDDEN = "(hidden)"


def _scheme_host(url: str) -> str:
    """``https://host[:port]`` of a URL: a push URL's path carries its device key, and a login
    (``user:pass@``) never shows. ``"(hidden)"`` when the URL has no host or a bad port."""
    parts = urlparse(url)
    host = parts.hostname
    try:
        port = parts.port
    except ValueError:
        return HIDDEN
    if not parts.scheme or not host:
        return HIDDEN
    if ":" in host:  # IPv6
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def config_json(config: Config, *, reveal: bool = False) -> dict[str, Any]:
    """The parsed config as JSON (``calsync config get``). Unless ``reveal``: push, webhook and
    public URLs are cut to scheme and host, a CalDAV URL too, and each account's username (an
    Apple ID) and credential ref read ``"(hidden)"``."""
    from calsync.durations import format_duration

    def url(value: str | None) -> str | None:
        return value if reveal or value is None or value == "icloud" else _scheme_host(value)

    def hidden(value: str | None) -> str | None:
        return value if reveal or value is None else HIDDEN

    def alarms(rule: Rule) -> str | list[str]:
        policy = rule.alarms
        return policy if isinstance(policy, str) else [format_duration(o) for o in policy]

    accounts = {
        a.name: {
            "provider": a.provider,
            "host": host_for(a.provider, a.server_url),
            "server_url": url(a.server_url),
            "username": hidden(a.username),
            "credential": {"backend": a.credential.backend, "ref": hidden(a.credential.ref)},
            "max_concurrency": a.max_concurrency,
        }
        for a in config.accounts.values()
    }
    rules = [
        {
            "name": r.name,
            "source": {"account": r.source.account, "calendar": r.source.calendar},
            "target": {
                "account": r.target.account,
                "calendar": r.target.calendar,
                "create_if_missing": r.target.create_if_missing,
            },
            "window": None
            if r.window is None
            else {"past": format_duration(r.window.past), "future": format_duration(r.window.future)},
            "alarms": alarms(r),
            "fields": {
                "content": r.fields.content,
                "attendees": r.fields.attendees,
                "conference": r.fields.conference,
            },
            "delete_guard": {"max_count": r.delete_guard.max_count, "max_ratio": r.delete_guard.max_ratio},
            "paused": r.paused,
            "retire": r.retire,
        }
        for r in config.rules
    ]
    web = config.web
    host = f"[{web.host}]" if ":" in web.host else web.host
    return {
        "revealed": reveal,
        "google": {
            "client_secret_file": None
            if config.google_client_secret_file is None
            else str(config.google_client_secret_file)
        },
        "accounts": accounts,
        "rules": rules,
        "notify": {
            "bark_url": url(config.notify.bark_url),
            "webhook_url": url(config.notify.webhook_url),
            "public_url": url(config.notify.public_url),
            "failure_threshold": config.notify.failure_threshold,
        },
        "runtime": {
            "interval": format_duration(config.runtime.interval),
            "jitter": config.runtime.jitter,
        },
        "web": {
            "listen": f"{host}:{web.port}" if web.enabled else "off",
            "allowed_hosts": list(web.allowed_hosts),
        },
    }


def config_history_json(changes: Sequence[ConfigChangeView]) -> dict[str, Any]:
    """``calsync config history --json`` and ``GET /api/config/history``. Never the ``before``
    text: a whole config, URLs included."""
    return {
        "changes": [
            {
                "id": c.id,
                "at": c.at.isoformat(),
                "source": c.source,
                "summary": c.summary,
                "after_fingerprint": c.after_fingerprint,
                "undoes": c.undoes,
            }
            for c in changes
        ]
    }


def calendars_json(account: str, calendars: Sequence[AccountCalendarView]) -> dict[str, Any]:
    """``calsync calendars --json`` and ``GET /api/calendars``: an account's cached list."""
    return {
        "account": account,
        "fetched_at": calendars[0].fetched_at.isoformat() if calendars else None,
        "calendars": [{"id": c.id, "name": c.name, "role": c.role} for c in calendars],
    }


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


AccountAuth = Literal["ok", "failed", "unknown"]


@dataclass(frozen=True, slots=True)
class CalendarStatus:
    """One calendar of an account's cached list, and the rules that sync it."""

    id: str
    name: str
    role: str | None
    series: int | None  # mirrored source series (a source account's calendar); None: a target's
    rules: tuple[str, ...]  # the rules using it, in config order
    synced: bool  # some rule using it is active (neither paused nor retiring)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "series": self.series,
            "rules": list(self.rules),
            "synced": self.synced,
        }


@dataclass(frozen=True, slots=True)
class AccountView:
    name: str
    provider: str
    host: str
    # ``failed``: a rule using the account last failed on its credential; ``ok``: every rule
    # using it last succeeded; else ``unknown`` (no rule, never run, or failing otherwise).
    auth: AccountAuth = "unknown"
    calendars: tuple[CalendarStatus, ...] = ()  # from the calendar cache; none cached: empty

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "host": self.host,
            "auth": self.auth,
            "calendars": [c.to_json() for c in self.calendars],
        }


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
    delivered_today: int = 0  # copies written (created, updated, reverted) since midnight

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
                "delivered_today": self.delivered_today,
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
    return "ok"  # also: only paused or retiring rules


def _rule_health(rule: Rule, last: RunView | None) -> Health:
    if rule.paused:
        return "paused"
    if rule.retire:
        return "retiring"
    return _health(last)


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


def blocked_message(rule: str, count: int) -> str:
    """What a blocked rule's attention says (``count`` 0: unknown)."""
    what = f"{count} {'copy' if count == 1 else 'copies'}" if count else "Copies"
    return f"{what} would be deleted — check `calsync blocked --rule {rule}`, then approve if intended"


def _attention(reader: StateReader, status: RuleStatus) -> Attention | None:
    """What needs a person for one rule. A paused rule needs nothing; a retiring one keeps the
    attention its runs call for (a retirement over the delete guard waits for a decision)."""
    rule = status.rule
    if rule.paused:
        return None
    health = _health(status.last_run) if rule.retire else status.health
    if health == "auth":
        last = status.last_run
        account = last.error_account if last is not None else None
        if account is None:  # a run recorded before ``error_account`` existed: guess from its text
            error = ((last and last.error) or "").lower()
            account = rule.source.account if "google" in error or "oauth" in error else rule.target.account
        return Attention(
            "auth",
            rule.name,
            account,
            f"The {account} account needs to be connected again.",
            f"calsync auth {account}",
        )
    if health == "blocked":
        # The rows of this block, as ``calsync blocked`` reads them: one count per block.
        entry = _blocked_rule(rule, reader)
        message = blocked_message(rule.name, entry.count if entry is not None else 0)
        return Attention("blocked", rule.name, None, message, f"calsync approve {rule.name}")
    if health == "failing":
        k = status.consecutive_failures
        return Attention("failing", rule.name, None, f"Sync has failed {k} times in a row.", None)
    return None


def _uses_target(reader: StateReader, rule: Rule, calendar: AccountCalendarView) -> bool:
    """The cached target calendar is the rule's: the href its ref resolved to when synced, else
    the ref itself (an href, or a name), as ``ops._match_calendar`` matches."""
    from calsync.naming import same_href

    ref = rule.target.calendar
    href = reader.target_calendar_href(rule.target.account, ref)
    if href is not None:
        return same_href(calendar.id, href)
    return same_href(calendar.id, ref) or calendar.name == ref


def _account_auth(account: str, rules: Sequence[Rule], last: Mapping[str, RunView | None]) -> AccountAuth:
    using = [r for r in rules if account in (r.source.account, r.target.account)]
    runs = [last[r.name] for r in using]
    if any(run is not None and run.error_kind == "auth" and run.error_account == account for run in runs):
        return "failed"
    if using and all(run is not None and _is_ok(run.status, run.failed) for run in runs):
        return "ok"
    return "unknown"


def _account_view(
    account: Account,
    config: Config,
    reader: StateReader,
    last: Mapping[str, RunView | None],
    cids: Mapping[str, str | None],
) -> AccountView:
    calendars: list[CalendarStatus] = []
    for cal in reader.account_calendars(account.name):
        sources = [r for r in config.rules if r.source.account == account.name and cids[r.name] == cal.id]
        targets = [
            r for r in config.rules if r.target.account == account.name and _uses_target(reader, r, cal)
        ]
        using = [r for r in config.rules if r in sources or r in targets]
        calendars.append(
            CalendarStatus(
                id=cal.id,
                name=cal.name,
                role=cal.role,
                series=reader.series_count(account.name, cal.id) if account.provider == "google" else None,
                rules=tuple(r.name for r in using),
                synced=any(not r.paused and not r.retire for r in using),
            )
        )
    return AccountView(
        account.name,
        account.provider,
        host_for(account.provider, account.server_url),
        auth=_account_auth(account.name, config.rules, last),
        calendars=tuple(calendars),
    )


def _midnight(now: datetime) -> datetime:
    """The start of ``now``'s day, in ``now``'s own zone."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def build_status(config: Config, reader: StateReader, now: datetime) -> StatusView:
    rules: list[RuleStatus] = []
    lasts: dict[str, RunView | None] = {}
    cids: dict[str, str | None] = {}
    for rule in config.rules:
        runs = reader.runs(rule.name, limit=1)
        last = lasts[rule.name] = runs[0] if runs else None
        last_ok = reader.last_ok_run(rule.name)
        cids[rule.name], entry = _source_calendar(reader, rule)
        rules.append(
            RuleStatus(
                rule=rule,
                source_name=entry.get("summary_override") or entry.get("summary") or rule.source.calendar,
                target_name=reader.target_calendar_name(rule.target.account, rule.target.calendar)
                or rule.target.calendar,
                health=_rule_health(rule, last),
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
        accounts=tuple(_account_view(a, config, reader, lasts, cids) for a in config.accounts.values()),
        rules=tuple(rules),
        attention=attention,
        delivered_today=reader.deliveries_since(_midnight(now)),
    )


# ----- blocked deletes -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockedItem:
    object_id: str
    title: str | None


@dataclass(frozen=True, slots=True)
class BlockedRule:
    rule: str
    since: datetime  # the first blocked run since the rule last synced ok
    count: int
    copies: int  # the rule's copies now (the guard's ratio is of these)
    max_count: int
    max_ratio: float
    reason: str | None  # the guard's reason, as the run recorded it
    fingerprint: str | None  # None: the blocked set is no longer recorded
    items: tuple[BlockedItem, ...]

    @property
    def ratio(self) -> float | None:
        return round(self.count / self.copies, 3) if self.copies else None

    def to_json(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "since": self.since.isoformat(),
            "count": self.count,
            "copies": self.copies,
            "ratio": self.ratio,
            "limit": {"max_count": self.max_count, "max_ratio": self.max_ratio},
            "reason": self.reason,
            "fingerprint": self.fingerprint,
            "items": [{"object_id": i.object_id, "title": i.title} for i in self.items],
        }


@dataclass(frozen=True, slots=True)
class BlockedView:
    rules: tuple[BlockedRule, ...]

    def to_json(self) -> dict[str, Any]:
        return {"blocked": [r.to_json() for r in self.rules]}


def _blocked_rule(rule: Rule, reader: StateReader) -> BlockedRule | None:
    from calsync.engine import delete_fingerprint  # light here: views must not load the engine early

    runs = reader.runs(rule.name)
    if not runs or runs[0].status != "blocked":
        return None
    first = runs[0]
    for run in runs:  # newest first: back to the last ok run (a failure does not end a block)
        if _is_ok(run.status, run.failed):
            break
        if run.status == "blocked":
            first = run
    found = reader.latest_blocked(rule.name)
    # Rows from before this block (an older one) do not describe it: unknown, never guessed.
    rows = found[1] if found is not None and found[0] >= first.id else []
    items = sorted(
        (BlockedItem(oid, title) for oid, title in rows),
        key=lambda i: (i.title is None, (i.title or "").casefold(), i.object_id),
    )
    return BlockedRule(
        rule=rule.name,
        since=first.started_at,
        count=len(items),
        copies=reader.copies(rule_tag(rule.name)),
        max_count=rule.delete_guard.max_count,
        max_ratio=rule.delete_guard.max_ratio,
        reason=runs[0].error,
        fingerprint=delete_fingerprint(i.object_id for i in items) if items else None,
        items=tuple(items),
    )


def build_blocked(config: Config, reader: StateReader, rule: str | None = None) -> BlockedView:
    """The rules whose latest run is blocked by the mass-delete guard, in config order, each
    with every planned delete (title order) and the fingerprint ``calsync approve`` names."""
    found = [
        entry
        for r in config.rules
        if (rule is None or r.name == rule) and (entry := _blocked_rule(r, reader)) is not None
    ]
    return BlockedView(tuple(found))


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


def source_series(reader: StateReader, account: str, cid: str, entry: Mapping[str, Any]) -> list[Series]:
    """The mirrored source calendar's series, as the engine builds them."""
    from calsync.sources.google import meta_from_state
    from calsync.sources.google_map import build_series

    return build_series(reader.source_events(account, cid), meta_from_state(entry))


class RuleReading:
    """One rule's copies as the state records them, read once per request: the source calendar
    its ref resolves to (``cid`` None: not resolvable), the recorded content hashes (only with a
    ``current`` schema: hashes of another calsync version are not comparable), the target
    calendar's name and the rule's window around ``now``. Shared by ``upcoming`` and ``events``."""

    def __init__(self, rule: Rule, reader: StateReader, now: datetime, current: bool) -> None:
        self.rule = rule
        self.cid, self.entry = _source_calendar(reader, rule)
        self.tag = rule_tag(rule.name)
        self.current = current
        self._hashes = reader.delivered_hashes(self.tag) if current else {}
        self.target_name = (
            reader.target_calendar_name(rule.target.account, rule.target.calendar) or rule.target.calendar
        )
        window = rule.window
        self.window = None if window is None else Window.around(now, window.past, window.future)

    def shape(self, item: Series) -> Series:
        """The series as the rule writes it (exactly as ``Engine._desired`` shapes it)."""
        return apply_field_policy(apply_alarm_policy(item, self.rule.alarms), self.rule.fields)

    def object_id(self, item: Series) -> str:
        assert self.cid is not None
        return object_id(self.rule.name, self.cid, item.source_id)

    def in_window(self, span: tuple[datetime, datetime]) -> bool:
        return self.window is None or self.window.intersects(span)

    def delivered(self, oid: str, shaped: Series) -> bool:
        """The copy's recorded hash is the one calsync would write now."""
        recorded = self._hashes.get(oid)
        return recorded is not None and recorded == series_hash(shaped)


def _rule_events(
    rule: Rule, reader: StateReader, now: datetime, end: datetime, current: bool
) -> list[UpcomingEvent]:
    """The rule's instances in ``[now, end)``, shaped as the rule writes them; series outside
    its window are left out."""
    reading = RuleReading(rule, reader, now, current)
    if reading.cid is None:
        return []
    events: list[UpcomingEvent] = []
    for item in source_series(reader, rule.source.account, reading.cid, reading.entry):
        shaped = reading.shape(item)
        if not reading.in_window(series_span(shaped)):
            continue
        delivery: Delivery = "unknown"
        if current:
            delivery = "delivered" if reading.delivered(reading.object_id(item), shaped) else "pending"
        events.extend(
            UpcomingEvent(rule.name, inst, reading.target_name, delivery)
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
