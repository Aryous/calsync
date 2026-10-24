"""Apply a source pull to the in-memory mirror of one calendar."""

from typing import Any

from calsync.sources.base import CalendarPull

Mirror = dict[str, dict[str, Any]]  # event id -> raw event


def apply_pull(mirror: Mirror, pull: CalendarPull) -> None:
    if pull.full:
        mirror.clear()
    for event in pull.upserts:
        mirror[event["id"]] = event
    for event_id in pull.deletes:
        mirror.pop(event_id, None)
