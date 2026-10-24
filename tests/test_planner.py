from datetime import UTC, datetime, timedelta

from calsync.config import DeleteGuard
from calsync.model import Window
from calsync.planner import Actual, Desired, Plan, check_delete_guard, plan
from tests.factories import series

NOW = datetime(2026, 10, 24, tzinfo=UTC)
WINDOW = Window.around(NOW, timedelta(days=7), timedelta(days=180))
IN = (NOW + timedelta(days=1), NOW + timedelta(days=1, minutes=5))
OLD = (NOW - timedelta(days=30), NOW - timedelta(days=29))
ONGOING = (NOW - timedelta(days=30), NOW + timedelta(days=10))


def want(oid: str, h: str = "h", span=IN) -> Desired:
    return Desired(oid, series(source_id=oid), h, span)


def have(oid: str, h: str | None = "h", span=IN) -> Actual:
    return Actual(oid, f"/cal/{oid}.ics", '"e"', h, span)


def test_create_update_noop() -> None:
    p = plan(
        {"a": want("a"), "b": want("b", "new"), "c": want("c")},
        {"b": have("b", "old"), "c": have("c")},
        WINDOW,
    )
    assert [d.object_id for d in p.create] == ["a"]
    assert [(d.object_id, a.content_hash) for d, a in p.update] == [("b", "old")]
    assert p.delete == []


def test_unknown_remote_hash_forces_update() -> None:
    p = plan({"a": want("a")}, {"a": have("a", None)}, WINDOW)
    assert len(p.update) == 1


def test_delete_only_inside_window() -> None:
    p = plan(
        {}, {"old": have("old", span=OLD), "cur": have("cur"), "unknown": have("unknown", span=None)}, WINDOW
    )
    assert sorted(a.object_id for a in p.delete) == ["cur", "unknown"]


def test_ongoing_series_started_before_window_is_deleted() -> None:
    p = plan({}, {"s": have("s", span=ONGOING)}, WINDOW)
    assert [a.object_id for a in p.delete] == ["s"]


def test_desired_outside_window_is_ignored_or_removed() -> None:
    far = (NOW + timedelta(days=200), NOW + timedelta(days=201))
    assert plan({"f": want("f", span=far)}, {}, WINDOW).empty
    moved = plan({"f": want("f", span=far)}, {"f": have("f")}, WINDOW)
    assert [a.object_id for a in moved.delete] == ["f"]


def test_delete_guard() -> None:
    guard = DeleteGuard(max_ratio=0.5, max_count=10)
    many = Plan(delete=[have(str(i)) for i in range(11)])
    assert "max_count" in (check_delete_guard(many, 100, guard) or "")
    half = Plan(delete=[have(str(i)) for i in range(4)])
    assert "max_ratio" in (check_delete_guard(half, 6, guard) or "")
    assert check_delete_guard(Plan(delete=[have("a"), have("b")]), 2, guard) is None
    assert check_delete_guard(Plan(), 0, guard) is None


def test_protected_actuals_are_never_deleted() -> None:
    p = plan({}, {"p": have("p"), "q": have("q", span=None), "x": have("x")}, WINDOW, frozenset({"p", "q"}))
    assert [a.object_id for a in p.delete] == ["x"]
    assert p.create == [] and p.update == []


def test_protected_desired_copy_is_left_untouched_but_may_be_created() -> None:
    far = (NOW + timedelta(days=200), NOW + timedelta(days=201))
    p = plan(
        {"held": want("held", "new"), "moved": want("moved", span=far), "fresh": want("fresh")},
        {"held": have("held", "old"), "moved": have("moved")},
        WINDOW,
        frozenset({"held", "moved", "fresh"}),
    )
    assert p.update == [] and p.delete == []
    assert [d.object_id for d in p.create] == ["fresh"]
