"""SQLite persistence.

Most tables are caches that can be rebuilt from both ends (the source mirror and cursors, the
target objects and cursors): losing them costs one heavy cycle, never data. History is not: the
``run`` rows and the ``change`` audit log (the only place a deleted event's title survives)
cannot be rebuilt, so a schema change keeps them.

Schema versions: an older database is upgraded in place, one ``_UPGRADES`` step per version,
in one transaction. A version with no path to ``SCHEMA_VERSION`` (older than the first step,
newer than this code, or unreadable) is dropped and rebuilt. To add version N+1: bump
``SCHEMA_VERSION``, change ``_SCHEMA`` (a fresh database), and add an ``N: _upgrade_N`` step
that turns a version-N database into exactly that, keeping ``run`` and ``change``; a step may
still empty a cache table if that is simpler. A step that creates a table drops it first: an
older calsync that reopened a newer database rebuilt only the tables it knows and left the
newer ones behind."""

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from calsync.mirror import Mirror
from calsync.sources.base import CalendarPull
from calsync.targets.base import CachedObject, TargetCache, TargetCalendar

SCHEMA_VERSION = 4
RUN_RETENTION = 500  # newest runs kept per rule
CHANGE_RETENTION = timedelta(days=90)  # change rows older than this (vs. the newest run) are pruned

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
    revert_times TEXT NOT NULL, hold_until TEXT,
    PRIMARY KEY (account, calendar_href, href)
);
CREATE TABLE run (
    id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL,
    updated INTEGER NOT NULL, deleted INTEGER NOT NULL, failed INTEGER NOT NULL,
    requests INTEGER NOT NULL, error TEXT, reverted INTEGER NOT NULL DEFAULT 0,
    protected INTEGER NOT NULL DEFAULT 0, out_of_window INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT
);
CREATE INDEX run_rule ON run (rule, id);
CREATE TABLE alert (key TEXT PRIMARY KEY, state TEXT NOT NULL, last_sent_at TEXT);
"""
# Version 4: the change log. Shared by a fresh schema and the 3 -> 4 upgrade, so they cannot drift.
_CHANGE_SCHEMA = (
    """CREATE TABLE change (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, rule TEXT NOT NULL,
    object_id TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL, title TEXT
)""",
    "CREATE INDEX change_rule ON change (rule, id)",
    "CREATE INDEX change_at ON change (at)",
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


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    """One object-level change. ``title`` is event content: it may be stored here and shown in
    --json/API output, never logged."""

    rule: str
    object_id: str
    action: str  # created, updated, reverted, deleted, protected, blocked
    at: datetime
    title: str | None


def _utc(t: datetime) -> str:
    """Change times are stored in UTC so the retention cutoff compares as text."""
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
            for statement in (*_SCHEMA.split(";"), *_CHANGE_SCHEMA):
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
        ) in self._db.execute(
            "SELECT href, etag, rule_tag, object_id, content_hash, span_start, span_end, verify_digest, "
            "reverting, revert_times, hold_until FROM target_object WHERE account = ? AND calendar_href = ?",
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
            )
            for href, obj in cache.objects.items()
        ]
        with self._tx():
            self._db.execute(
                "DELETE FROM target_object WHERE account = ? AND calendar_href = ?", (account, calendar_href)
            )
            self._db.executemany(
                "INSERT INTO target_object VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
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
                    "failed, requests, error, reverted, protected, out_of_window, error_kind) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    ),
                )
                assert cursor.lastrowid is not None
                run_ids[r.rule] = cursor.lastrowid
            self._db.executemany(
                "INSERT INTO change (run_id, rule, object_id, action, at, title) VALUES (?, ?, ?, ?, ?, ?)",
                [(run_ids[c.rule], c.rule, c.object_id, c.action, _utc(c.at), c.title) for c in changes],
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

    def last_title(self, rule: str, object_id: str) -> str | None:
        """The newest known title of an object: the only one left once its source is gone."""
        row = self._db.execute(
            "SELECT title FROM change WHERE rule = ? AND object_id = ? AND title IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (rule, object_id),
        ).fetchone()
        return row[0] if row else None

    def latest_runs(self) -> list[RunRecord]:
        rows = self._db.execute(
            "SELECT rule, started_at, finished_at, status, created, updated, deleted, failed, requests, error, "
            "reverted, protected, out_of_window, error_kind "
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
    for statement in _CHANGE_SCHEMA:
        db.execute(statement)


# version -> the step that turns a database of that version into the next one
_UPGRADES: dict[int, Callable[[sqlite3.Connection], None]] = {3: _upgrade_3}


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
