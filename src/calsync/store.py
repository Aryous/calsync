"""SQLite persistence. Every table is a rebuildable cache, so a schema change
simply drops and recreates everything instead of migrating."""

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from calsync.mirror import Mirror
from calsync.sources.base import CalendarPull
from calsync.targets.base import CachedObject, TargetCache, TargetCalendar

SCHEMA_VERSION = 1

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
    span_start TEXT, span_end TEXT,
    PRIMARY KEY (account, calendar_href, href)
);
CREATE TABLE run (
    id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL,
    updated INTEGER NOT NULL, deleted INTEGER NOT NULL, failed INTEGER NOT NULL,
    requests INTEGER NOT NULL, error TEXT
);
CREATE INDEX run_rule ON run (rule, id);
CREATE TABLE alert (key TEXT PRIMARY KEY, state TEXT NOT NULL, last_sent_at TEXT);
"""
_TABLES = ("meta", "source_state", "source_event", "target_calendar", "target_object", "run", "alert")


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


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _ensure_schema(self) -> None:
        try:
            row = self._db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.OperationalError:
            row = None
        if row and int(row[0]) == SCHEMA_VERSION:
            return
        with self._tx():
            for table in _TABLES:
                self._db.execute(f"DROP TABLE IF EXISTS {table}")
            # executescript() would commit our open transaction, so run statements one by one.
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    self._db.execute(statement)
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

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
        self._db.execute(
            "INSERT INTO target_calendar (account, ref, href, name, mode) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (account, ref) DO UPDATE SET href = excluded.href, name = excluded.name, "
            "mode = excluded.mode, cursor = CASE WHEN target_calendar.href = excluded.href "
            "THEN target_calendar.cursor ELSE NULL END",
            (account, ref, cal.href, cal.name, cal.mode),
        )

    def forget_target_calendar(self, account: str, ref: str) -> None:
        self._db.execute("DELETE FROM target_calendar WHERE account = ? AND ref = ?", (account, ref))

    def load_target_cache(self, account: str, calendar_href: str) -> TargetCache:
        row = self._db.execute(
            "SELECT cursor FROM target_calendar WHERE account = ? AND href = ? LIMIT 1",
            (account, calendar_href),
        ).fetchone()
        cache = TargetCache(cursor=row[0] if row else None)
        for href, etag, tag, oid, content_hash, lo, hi in self._db.execute(
            "SELECT href, etag, rule_tag, object_id, content_hash, span_start, span_end "
            "FROM target_object WHERE account = ? AND calendar_href = ?",
            (account, calendar_href),
        ):
            span = (_dt(lo), _dt(hi)) if lo and hi else None
            cache.objects[href] = CachedObject(etag, tag, oid, content_hash, span)  # type: ignore[arg-type]
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
            )
            for href, obj in cache.objects.items()
        ]
        with self._tx():
            self._db.execute(
                "DELETE FROM target_object WHERE account = ? AND calendar_href = ?", (account, calendar_href)
            )
            self._db.executemany("INSERT INTO target_object VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self._db.execute(
                "UPDATE target_calendar SET cursor = ? WHERE account = ? AND href = ?",
                (cache.cursor, account, calendar_href),
            )

    # ----- runs ---------------------------------------------------------
    def record_runs(self, runs: Sequence[RunRecord]) -> None:
        with self._tx():
            self._db.executemany(
                "INSERT INTO run (rule, started_at, finished_at, status, created, updated, deleted, "
                "failed, requests, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
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
                    )
                    for r in runs
                ],
            )

    def latest_runs(self) -> list[RunRecord]:
        rows = self._db.execute(
            "SELECT rule, started_at, finished_at, status, created, updated, deleted, failed, requests, error "
            "FROM run WHERE id IN (SELECT MAX(id) FROM run GROUP BY rule) ORDER BY rule"
        ).fetchall()
        return [
            RunRecord(r[0], datetime.fromisoformat(r[1]), datetime.fromisoformat(r[2]), *r[3:]) for r in rows
        ]


class _Transaction:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def __enter__(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: object, *_: object) -> None:
        self._db.execute("ROLLBACK" if exc_type else "COMMIT")
