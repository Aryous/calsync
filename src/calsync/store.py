"""SQLite persistence.

Most tables are caches that can be rebuilt from both ends (the source mirror and cursors, the
target objects and cursors): losing them costs one heavy cycle, never data. History is not: the
``run`` rows, the ``change`` audit log (the only place a deleted event's title survives) and the
``config_change`` history cannot be rebuilt, so a schema change keeps them.

Schema versions: an older database is upgraded in place, one ``_UPGRADES`` step per version,
in one transaction. A version with no path to ``SCHEMA_VERSION`` (older than the first step,
newer than this code, or unreadable) is dropped and rebuilt. To add version N+1: bump
``SCHEMA_VERSION``, change ``_SCHEMA`` (a fresh database), and add an ``N: _upgrade_N`` step
that turns a version-N database into exactly that, keeping ``run``, ``change`` and
``config_change``; a step may still empty a cache table if that is simpler. A step that creates
a table drops it first: an older calsync that reopened a newer database rebuilt only the tables
it knows and left the newer ones behind."""

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from calsync.mirror import Mirror
from calsync.sources.base import CalendarPull
from calsync.targets.base import CachedObject, TargetCache, TargetCalendar

SCHEMA_VERSION = 5
RUN_RETENTION = 500  # newest runs kept per rule
CHANGE_RETENTION = timedelta(days=90)  # change rows older than this (vs. the newest run) are pruned
CONFIG_CHANGE_RETENTION = 200  # newest config_change rows kept

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE source_state (account TEXT PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE source_event (
    account TEXT NOT NULL, calendar TEXT NOT NULL, event_id TEXT NOT NULL, raw TEXT NOT NULL,
    PRIMARY KEY (account, calendar, event_id)
);
CREATE TABLE target_calendar (
    account TEXT NOT NULL, ref TEXT NOT NULL, href TEXT NOT NULL, name TEXT NOT NULL,
    mode TEXT NOT NULL, cursor TEXT,
    PRIMARY KEY (account, ref)
);
CREATE TABLE target_object (
    account TEXT NOT NULL, calendar_href TEXT NOT NULL, href TEXT NOT NULL, etag TEXT,
    rule_tag TEXT NOT NULL, object_id TEXT NOT NULL, content_hash TEXT,
    span_start TEXT, span_end TEXT, verify_digest TEXT, reverting INTEGER NOT NULL,
    revert_times TEXT NOT NULL, hold_until TEXT, summary TEXT,
    PRIMARY KEY (account, calendar_href, href)
);
CREATE TABLE run (
    id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL,
    updated INTEGER NOT NULL, deleted INTEGER NOT NULL, failed INTEGER NOT NULL,
    requests INTEGER NOT NULL, error TEXT, reverted INTEGER NOT NULL DEFAULT 0,
    protected INTEGER NOT NULL DEFAULT 0, out_of_window INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT, error_account TEXT
);
CREATE INDEX run_rule ON run (rule, id);
CREATE TABLE alert (key TEXT PRIMARY KEY, state TEXT NOT NULL, last_sent_at TEXT);
CREATE TABLE change (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, rule TEXT NOT NULL,
    object_id TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL, title TEXT, detail TEXT
);
CREATE INDEX change_rule ON change (rule, id);
CREATE INDEX change_at ON change (at);
"""
# The change log as version 4 created it: what the 3 -> 4 step builds (the 4 -> 5 step then adds
# ``detail``). Frozen; ``test_upgraded_schema_equals_a_fresh_one`` keeps it in line with ``_SCHEMA``.
_CHANGE_SCHEMA_V4 = (
    """CREATE TABLE change (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, rule TEXT NOT NULL,
    object_id TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL, title TEXT
)""",
    "CREATE INDEX change_rule ON change (rule, id)",
    "CREATE INDEX change_at ON change (at)",
)
# Version 5: the calendar cache and the config history. Shared by a fresh schema and the 4 -> 5
# upgrade, so they cannot drift.
_V5_SCHEMA = (
    # role: Google accessRole / CalDAV "rw" | "ro"
    """CREATE TABLE account_calendar (
    account TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, role TEXT, fetched_at TEXT NOT NULL,
    PRIMARY KEY (account, id)
)""",
    # source: cli | ui | daemon | external
    """CREATE TABLE config_change (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, source TEXT NOT NULL,
    summary TEXT NOT NULL, before TEXT, after_fingerprint TEXT NOT NULL, undoes INTEGER
)""",
    "CREATE INDEX config_change_at ON config_change (at)",
)
_TABLES = (
    "meta",
    "source_state",
    "source_event",
    "target_calendar",
    "target_object",
    "run",
    "change",
    "alert",
    "account_calendar",
    "config_change",
)


@dataclass(frozen=True, slots=True)
class RunRecord:
    rule: str
    started_at: datetime
    finished_at: datetime
    status: str
    created: int
    updated: int
    deleted: int
    failed: int
    requests: int
    error: str | None
    reverted: int = 0
    protected: int = 0
    out_of_window: int = 0
    error_kind: str | None = None
    error_account: str | None = None  # the account whose adapter or setup failed


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    """One object-level change. ``title`` is event content: it may be stored here and shown in
    --json/API output, never logged."""

    rule: str
    object_id: str
    action: str  # created, updated, reverted, deleted, protected, blocked, approved, guard_loosened
    at: datetime
    title: str | None
    # JSON ``{"fields": {name: [old, new]}}`` (summary fields that differ) for an update or a
    # revert whose previous summary is known; None otherwise. Event content, like ``title``.
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigChangeRecord:
    """One write of the config file. ``before`` is the whole previous text (``None``: none kept)."""

    id: int
    at: datetime
    source: str  # cli, ui, daemon, external
    summary: str
    before: str | None
    after_fingerprint: str
    undoes: int | None  # the id of the change this one undid


@dataclass(frozen=True, slots=True)
class AccountCalendar:
    """One calendar of an account's calendar list, as last fetched."""

    account: str
    id: str
    name: str
    role: str | None  # Google accessRole / CalDAV "rw" | "ro"
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class OpenAlert:
    """An open ``alert`` key: when its push went out (None: unknown) and its detail."""

    sent_at: datetime | None
    detail: Mapping[str, Any] | None = None


def _open_alert(state: str, sent: str | None) -> OpenAlert:
    detail: Any = None
    if state.startswith("open "):
        try:
            detail = json.loads(state[len("open ") :])
        except ValueError:
            detail = None
    try:
        sent_at = datetime.fromisoformat(sent) if sent else None
    except ValueError:
        sent_at = None
    return OpenAlert(sent_at, detail if isinstance(detail, dict) else None)


def _utc(t: datetime) -> str:
    """Times in the history tables are stored in UTC so they compare (and prune) as text."""
    return t.astimezone(UTC).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._path = path
        try:
            self._db = self._open()
        except sqlite3.DatabaseError as exc:
            if isinstance(exc, sqlite3.OperationalError) and "not a database" not in str(exc):
                raise
            self._quarantine()
            self._db = self._open()
        self._restrict_permissions()

    def _open(self) -> sqlite3.Connection:
        # Create the file as 0600 ourselves so it is never briefly world-readable.
        os.close(os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600))
        db = sqlite3.connect(self._path, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema(db)
        except BaseException:
            db.close()
            raise
        return db

    def _quarantine(self) -> None:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self._path) + suffix)
            if src.exists():
                src.rename(Path(f"{self._path}.corrupt-{stamp}{suffix}"))

    def _restrict_permissions(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self._path) + suffix)
            if path.exists():
                path.chmod(0o600)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> None:
        try:
            row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            row = None
        try:
            version = int(row[0]) if row else None
        except (TypeError, ValueError):
            version = None  # unreadable version: rebuild
        if version == SCHEMA_VERSION:
            return
        with _Transaction(db):
            if version is not None and _upgradable(version):
                while version != SCHEMA_VERSION:
                    _UPGRADES[version](db)
                    version += 1
                db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                return
            for table in _TABLES:
                db.execute(f"DROP TABLE IF EXISTS {table}")
            # executescript() would commit our open transaction, so run statements one by one.
            for statement in (*_SCHEMA.split(";"), *_V5_SCHEMA):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def _tx(self) -> "_Transaction":
        return _Transaction(self._db)

    # ----- source side --------------------------------------------------
    def load_source(self, account: str) -> tuple[dict[str, Any], dict[str, Mirror]]:
        row = self._db.execute("SELECT state FROM source_state WHERE account = ?", (account,)).fetchone()
        state = json.loads(row[0]) if row else {}
        mirrors: dict[str, Mirror] = {}
        for calendar, event_id, raw in self._db.execute(
            "SELECT calendar, event_id, raw FROM source_event WHERE account = ?", (account,)
        ):
            mirrors.setdefault(calendar, {})[event_id] = json.loads(raw)
        return state, mirrors

    def save_source(self, account: str, state: Mapping[str, Any], pulls: Iterable[CalendarPull]) -> None:
        """Mirror rows and the cursor that produced them commit together."""
        with self._tx():
            for pull in pulls:
                if pull.full:
                    self._db.execute(
                        "DELETE FROM source_event WHERE account = ? AND calendar = ?",
                        (account, pull.calendar_id),
                    )
                self._db.executemany(
                    "INSERT OR REPLACE INTO source_event (account, calendar, event_id, raw) VALUES (?, ?, ?, ?)",
                    [
                        (account, pull.calendar_id, e["id"], json.dumps(e, ensure_ascii=False))
                        for e in pull.upserts
                    ],
                )
                self._db.executemany(
                    "DELETE FROM source_event WHERE account = ? AND calendar = ? AND event_id = ?",
                    [(account, pull.calendar_id, event_id) for event_id in pull.deletes],
                )
            self._db.execute(
                "INSERT OR REPLACE INTO source_state (account, state) VALUES (?, ?)",
                (account, json.dumps(state, ensure_ascii=False)),
            )

    # ----- target side --------------------------------------------------
    def load_target_calendar(self, account: str, ref: str) -> TargetCalendar | None:
        row = self._db.execute(
            "SELECT href, name, mode FROM target_calendar WHERE account = ? AND ref = ?", (account, ref)
        ).fetchone()
        return TargetCalendar(row[0], row[1], row[2]) if row else None

    def save_target_calendar(self, account: str, ref: str, cal: TargetCalendar) -> None:
        with self._tx():
            old = self._db.execute(
                "SELECT href FROM target_calendar WHERE account = ? AND ref = ?", (account, ref)
            ).fetchone()
            if old and old[0] != cal.href:
                self._drop_objects(account, old[0])
            self._db.execute(
                "INSERT INTO target_calendar (account, ref, href, name, mode) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (account, ref) DO UPDATE SET href = excluded.href, name = excluded.name, "
                "mode = excluded.mode, cursor = CASE WHEN target_calendar.href = excluded.href "
                "THEN target_calendar.cursor ELSE NULL END",
                (account, ref, cal.href, cal.name, cal.mode),
            )

    def forget_target_calendar(self, account: str, ref: str) -> None:
        with self._tx():
            old = self._db.execute(
                "SELECT href FROM target_calendar WHERE account = ? AND ref = ?", (account, ref)
            ).fetchone()
            if old:
                self._drop_objects(account, old[0])
            self._db.execute("DELETE FROM target_calendar WHERE account = ? AND ref = ?", (account, ref))

    def _drop_objects(self, account: str, calendar_href: str) -> None:
        """Forget a calendar's objects and, with them, every ref's cursor for that href:
        a cursor without its objects would make the next detection skip them for good."""
        self._db.execute(
            "DELETE FROM target_object WHERE account = ? AND calendar_href = ?", (account, calendar_href)
        )
        self._db.execute(
            "UPDATE target_calendar SET cursor = NULL WHERE account = ? AND href = ?",
            (account, calendar_href),
        )

    def load_target_cache(self, account: str, calendar_href: str) -> TargetCache:
        row = self._db.execute(
            "SELECT cursor FROM target_calendar WHERE account = ? AND href = ? LIMIT 1",
            (account, calendar_href),
        ).fetchone()
        cache = TargetCache(cursor=row[0] if row else None)
        for (
            href,
            etag,
            tag,
            oid,
            content_hash,
            lo,
            hi,
            verify_digest,
            reverting,
            times,
            hold,
            summary,
        ) in self._db.execute(
            "SELECT href, etag, rule_tag, object_id, content_hash, span_start, span_end, verify_digest, "
            "reverting, revert_times, hold_until, summary FROM target_object "
            "WHERE account = ? AND calendar_href = ?",
            (account, calendar_href),
        ):
            span = (datetime.fromisoformat(lo), datetime.fromisoformat(hi)) if lo and hi else None
            cache.objects[href] = CachedObject(
                etag,
                tag,
                oid,
                content_hash,
                span,
                verify_digest,
                bool(reverting),
                revert_times=tuple(datetime.fromisoformat(t) for t in json.loads(times)),
                hold_until=datetime.fromisoformat(hold) if hold else None,
                summary=summary,
            )
        return cache

    def save_target_cache(self, account: str, calendar_href: str, cache: TargetCache) -> None:
        rows = [
            (
                account,
                calendar_href,
                href,
                obj.etag,
                obj.rule_tag,
                obj.object_id,
                obj.content_hash,
                obj.span[0].isoformat() if obj.span else None,
                obj.span[1].isoformat() if obj.span else None,
                obj.verify_digest,
                int(obj.reverting),
                json.dumps([t.isoformat() for t in obj.revert_times]),
                obj.hold_until.isoformat() if obj.hold_until else None,
                obj.summary,
            )
            for href, obj in cache.objects.items()
        ]
        with self._tx():
            self._db.execute(
                "DELETE FROM target_object WHERE account = ? AND calendar_href = ?", (account, calendar_href)
            )
            self._db.executemany(
                "INSERT INTO target_object (account, calendar_href, href, etag, rule_tag, object_id, "
                "content_hash, span_start, span_end, verify_digest, reverting, revert_times, hold_until, "
                "summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._db.execute(
                "UPDATE target_calendar SET cursor = ? WHERE account = ? AND href = ?",
                (cache.cursor, account, calendar_href),
            )

    # ----- runs ---------------------------------------------------------
    def record_runs(self, runs: Sequence[RunRecord], changes: Sequence[ChangeRecord] = ()) -> None:
        """Runs, their changes and the retention pruning commit together. Each change belongs to
        the run of its rule in this call (a change for a rule with no run here is a bug: KeyError,
        nothing is written, not even the runs)."""
        with self._tx():
            run_ids: dict[str, int] = {}
            for r in runs:
                cursor = self._db.execute(
                    "INSERT INTO run (rule, started_at, finished_at, status, created, updated, deleted, "
                    "failed, requests, error, reverted, protected, out_of_window, error_kind, error_account) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        r.rule,
                        r.started_at.isoformat(),
                        r.finished_at.isoformat(),
                        r.status,
                        r.created,
                        r.updated,
                        r.deleted,
                        r.failed,
                        r.requests,
                        r.error,
                        r.reverted,
                        r.protected,
                        r.out_of_window,
                        r.error_kind,
                        r.error_account,
                    ),
                )
                assert cursor.lastrowid is not None
                run_ids[r.rule] = cursor.lastrowid
            self._db.executemany(
                "INSERT INTO change (run_id, rule, object_id, action, at, title, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (run_ids[c.rule], c.rule, c.object_id, c.action, _utc(c.at), c.title, c.detail)
                    for c in changes
                ],
            )
            for rule in run_ids:
                self._db.execute(
                    "DELETE FROM run WHERE rule = ? AND id NOT IN "
                    "(SELECT id FROM run WHERE rule = ? ORDER BY id DESC LIMIT ?)",
                    (rule, rule, RUN_RETENTION),
                )
            if runs:
                # Changes are pruned by age only, never with their run: RUN_RETENTION keeps hours of
                # runs, changes are kept CHANGE_RETENTION, so a change's run_id may name a pruned run.
                # "Now" is the newest run in this call, never the wall clock: deterministic.
                now = max(r.finished_at for r in runs)
                self._db.execute("DELETE FROM change WHERE at < ?", (_utc(now - CHANGE_RETENTION),))

    def record_change(self, change: ChangeRecord) -> None:
        """One change row outside a cycle (a loosened delete guard), attached to the rule's
        newest run (``run_id`` 0 when it has none)."""
        with self._tx():
            row = self._db.execute("SELECT MAX(id) FROM run WHERE rule = ?", (change.rule,)).fetchone()
            run_id = int(row[0]) if row and row[0] is not None else 0
            self._db.execute(
                "INSERT INTO change (run_id, rule, object_id, action, at, title, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    change.rule,
                    change.object_id,
                    change.action,
                    _utc(change.at),
                    change.title,
                    change.detail,
                ),
            )

    def last_title(self, rule: str, object_id: str) -> str | None:
        """The newest known title of an object: the only one left once its source is gone."""
        row = self._db.execute(
            "SELECT title FROM change WHERE rule = ? AND object_id = ? AND title IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (rule, object_id),
        ).fetchone()
        return row[0] if row else None

    def statuses(self, rule: str) -> Iterator[tuple[str, int, str | None]]:
        """The rule's runs as (status, failed, error_kind), newest first (as
        ``StateReader.statuses``)."""
        rows = self._db.execute(
            "SELECT status, failed, error_kind FROM run WHERE rule = ? ORDER BY id DESC", (rule,)
        )
        return ((r[0], r[1], r[2]) for r in rows.fetchall())

    # ----- alerts -------------------------------------------------------
    # ``alert.state`` is ``closed``, ``open``, or ``open <JSON>``: an open key with its detail
    # (the count a ``blocked`` push named, the config text a ``reload`` push was for), which
    # ``notify`` reads to damp a replacement key. Kept in ``state`` so the schema stays as is.

    def open_alerts(self) -> dict[str, OpenAlert]:
        """The alerts sent and not yet closed, by key (``notify``)."""
        rows = self._db.execute(
            "SELECT key, state, last_sent_at FROM alert WHERE state = 'open' OR state LIKE 'open %'"
        )
        return {key: _open_alert(state, sent) for key, state, sent in rows.fetchall()}

    def update_alerts(
        self,
        *,
        opened: Mapping[str, OpenAlert] | None = None,
        closed: Iterable[str] = (),
        prune_before: datetime | None = None,
    ) -> None:
        """Open (or replace) ``opened``, close ``closed`` and, with ``prune_before``, delete the
        closed rows last sent before it (or never), in one transaction."""
        rows = [
            (
                key,
                "open " + json.dumps(alert.detail, sort_keys=True) if alert.detail else "open",
                _utc(alert.sent_at) if alert.sent_at is not None else None,
            )
            for key, alert in (opened or {}).items()
        ]
        with self._tx():
            self._db.executemany(
                "INSERT OR REPLACE INTO alert (key, state, last_sent_at) VALUES (?, ?, ?)", rows
            )
            self._db.executemany(
                "UPDATE alert SET state = 'closed' WHERE key = ?", [(key,) for key in closed]
            )
            if prune_before is not None:
                self._db.execute(
                    "DELETE FROM alert WHERE state = 'closed' AND (last_sent_at IS NULL OR last_sent_at < ?)",
                    (_utc(prune_before),),
                )

    # ----- config history ------------------------------------------------
    def copies(self, tag: str, account: str, ref: str) -> int:
        """The copies of rule ``tag`` recorded in the target calendar ``ref`` of ``account``
        (copies left in a calendar the rule no longer targets are not counted)."""
        row = self._db.execute(
            "SELECT COUNT(*) FROM target_object o JOIN target_calendar c "
            "ON o.account = c.account AND o.calendar_href = c.href "
            "WHERE c.account = ? AND c.ref = ? AND o.rule_tag = ?",
            (account, ref, tag),
        ).fetchone()
        return int(row[0])

    def tagged_copies(self, tag: str) -> int:
        """The copies of rule ``tag`` recorded in any target calendar."""
        row = self._db.execute("SELECT COUNT(*) FROM target_object WHERE rule_tag = ?", (tag,)).fetchone()
        return int(row[0])

    def record_config_change(
        self,
        *,
        at: datetime,
        source: str,
        summary: str,
        before: str | None,
        after_fingerprint: str,
        undoes: int | None = None,
    ) -> int:
        """Record one config write and prune to the newest ``CONFIG_CHANGE_RETENTION``; the new id."""
        with self._tx():
            cursor = self._db.execute(
                "INSERT INTO config_change (at, source, summary, before, after_fingerprint, undoes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_utc(at), source, summary, before, after_fingerprint, undoes),
            )
            assert cursor.lastrowid is not None
            self._db.execute(
                "DELETE FROM config_change WHERE id NOT IN "
                "(SELECT id FROM config_change ORDER BY id DESC LIMIT ?)",
                (CONFIG_CHANGE_RETENTION,),
            )
        return cursor.lastrowid

    def config_changes(self, limit: int = 50) -> list[ConfigChangeRecord]:
        """Config writes, newest first."""
        rows = self._db.execute(
            "SELECT id, at, source, summary, before, after_fingerprint, undoes FROM config_change "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [ConfigChangeRecord(r[0], datetime.fromisoformat(r[1]), *r[2:]) for r in rows]

    # ----- calendar cache ----------------------------------------------
    def save_account_calendars(
        self, account: str, calendars: Sequence[AccountCalendar], at: datetime
    ) -> None:
        """Replace the account's cached calendar list with ``calendars``, fetched at ``at``. The
        arguments are authoritative: each row is stored under ``account`` with ``fetched_at = at``."""
        with self._tx():
            self._db.execute("DELETE FROM account_calendar WHERE account = ?", (account,))
            self._db.executemany(
                "INSERT INTO account_calendar (account, id, name, role, fetched_at) VALUES (?, ?, ?, ?, ?)",
                [(account, c.id, c.name, c.role, _utc(at)) for c in calendars],
            )

    def latest_runs(self) -> list[RunRecord]:
        rows = self._db.execute(
            "SELECT rule, started_at, finished_at, status, created, updated, deleted, failed, requests, error, "
            "reverted, protected, out_of_window, error_kind, error_account "
            "FROM run WHERE id IN (SELECT MAX(id) FROM run GROUP BY rule) ORDER BY rule"
        ).fetchall()
        return [
            RunRecord(r[0], datetime.fromisoformat(r[1]), datetime.fromisoformat(r[2]), *r[3:]) for r in rows
        ]


def _upgrade_3(db: sqlite3.Connection) -> None:
    """3 -> 4: the richer run rows and the change log; every row is kept."""
    for column in ("reverted", "protected", "out_of_window"):
        db.execute(f"ALTER TABLE run ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE run ADD COLUMN error_kind TEXT")
    # An older calsync that reopened a v4 database rebuilt its own tables but left ``change``
    # behind; its rows point at run ids that rebuild reset, so they go.
    db.execute("DROP TABLE IF EXISTS change")
    for statement in _CHANGE_SCHEMA_V4:
        db.execute(statement)


def _upgrade_4(db: sqlite3.Connection) -> None:
    """4 -> 5: copy summaries, change details, the failing account of a run, the calendar cache
    and the config history; every row is kept."""
    db.execute("ALTER TABLE target_object ADD COLUMN summary TEXT")
    db.execute("ALTER TABLE change ADD COLUMN detail TEXT")
    db.execute("ALTER TABLE run ADD COLUMN error_account TEXT")
    # A v4 calsync that reopened a v5 database rebuilt its own tables (so the columns above are
    # new again) but left these two behind.
    for table in ("account_calendar", "config_change"):
        db.execute(f"DROP TABLE IF EXISTS {table}")
    for statement in _V5_SCHEMA:
        db.execute(statement)


# version -> the step that turns a database of that version into the next one
_UPGRADES: dict[int, Callable[[sqlite3.Connection], None]] = {3: _upgrade_3, 4: _upgrade_4}


def _upgradable(version: int) -> bool:
    """Whether the steps lead from ``version`` to ``SCHEMA_VERSION`` (never downwards)."""
    while version < SCHEMA_VERSION and version in _UPGRADES:
        version += 1
    return version == SCHEMA_VERSION


class _Transaction:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def __enter__(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: object, *_: object) -> None:
        if exc_type is None:
            try:
                self._db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
        else:
            self._rollback()  # never mask the original error

    def _rollback(self) -> None:
        # SQLite may already have rolled back on its own (e.g. disk full).
        if self._db.in_transaction:
            self._db.execute("ROLLBACK")
