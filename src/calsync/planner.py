"""Pure planning: desired state vs. actual state -> create / update / delete."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from calsync.config import DeleteGuard
from calsync.model import Series, Window

# The ratio check only applies once a plan deletes at least this many events,
# so removing the last one or two events of a small calendar is never blocked.
RATIO_GUARD_MIN = 3


@dataclass(frozen=True, slots=True)
class Desired:
    object_id: str
    series: Series
    content_hash: str
    span: tuple[datetime, datetime]


@dataclass(frozen=True, slots=True)
class Actual:
    object_id: str
    href: str
    etag: str | None
    content_hash: str | None
    span: tuple[datetime, datetime] | None


@dataclass(slots=True)
class Plan:
    create: list[Desired] = field(default_factory=list)
    update: list[tuple[Desired, Actual]] = field(default_factory=list)
    delete: list[Actual] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.create or self.update or self.delete)


def plan(desired: Mapping[str, Desired], actual: Mapping[str, Actual], window: Window) -> Plan:
    result = Plan()
    for oid, want in desired.items():
        if not window.intersects(want.span):
            continue
        have = actual.get(oid)
        if have is None:
            result.create.append(want)
        elif have.content_hash != want.content_hash:
            result.update.append((want, have))
    for oid, have in actual.items():
        want = desired.get(oid)
        if want is not None and window.intersects(want.span):
            continue
        # Unknown span (unparseable body) counts as "in window": it is ours and unwanted.
        if have.span is None or window.intersects(have.span):
            result.delete.append(have)
    result.create.sort(key=lambda d: d.object_id)
    result.update.sort(key=lambda pair: pair[0].object_id)
    result.delete.sort(key=lambda a: a.object_id)
    return result


def check_delete_guard(p: Plan, actual_count: int, guard: DeleteGuard) -> str | None:
    """Return a reason string if the plan deletes suspiciously much, else None."""
    count = len(p.delete)
    if count > guard.max_count:
        return f"plan deletes {count} events, more than max_count={guard.max_count}"
    if count >= RATIO_GUARD_MIN and actual_count and count > guard.max_ratio * actual_count:
        return f"plan deletes {count} of {actual_count} managed events, more than max_ratio={guard.max_ratio}"
    return None
