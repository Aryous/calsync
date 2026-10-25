"""The local JSON API (``calsync.api``) on a state built by one cycle (FakeGoogle + FakeTarget).

Requests go through ``httpx.ASGITransport``; the socket serving is tested in ``test_daemon``.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from calsync import __version__, api, views
from calsync.config import Config
from calsync.daemon import DaemonInfo
from calsync.store import ChangeRecord, RunRecord, Store
from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, NOW, build_fake, config_for, rule

pytestmark = pytest.mark.anyio

STATUS_KEYS = ["generated_at", "schema", "summary", "accounts", "rules", "attention", "version", "daemon"]
NO_STATE = {"error": {"kind": "no_state", "message": "No sync has run yet."}}


def config() -> Config:
    return config_for([rule(alarms=["-10m", "0m"])])


@pytest.fixture
async def state(tmp_path: Path) -> Path:
    """A state directory after one cycle at harness NOW."""
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(
        GCAL,
        google_event(
            "standup",
            "Standup",
            "2026-10-23T18:00:00+08:00",
            "2026-10-23T18:15:00+08:00",
            recurrence=["RRULE:FREQ=DAILY;COUNT=10"],
        ),
    )
    fake.put_event(
        GCAL, google_event("lunch", "Lunch", "2026-10-25T12:00:00+08:00", "2026-10-25T13:00:00+08:00")
    )
    h = build_fake(tmp_path, fake, config())
    await h.engine.cycle()
    h.store.close()
    return tmp_path


class FakeDaemon:
    """Records ``request_cycle`` calls; ``answer`` is what it returns (or raises)."""

    def __init__(self, answer: bool | Exception = True) -> None:
        self.answer = answer
        self.calls: list[str | None] = []

    def request_cycle(self, rule: str | None = None) -> bool:
        self.calls.append(rule)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def snapshot(self) -> DaemonInfo:
        return DaemonInfo(True, True, 4242, NOW, NOW, NOW + timedelta(seconds=60), 1)


def client(state_dir: Path, daemon: Any = None) -> httpx.AsyncClient:
    app = api.create_app(config_provider=config, state_dir=state_dir, daemon=daemon, clock=lambda: NOW)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://calsync")


# ----- GET routes -------------------------------------------------------------


async def test_status(state: Path) -> None:
    async with client(state) as c:
        response = await c.get("/api/status")
    assert response.status_code == 200 and response.headers["content-type"] == "application/json"
    body = response.json()
    assert list(body) == STATUS_KEYS
    assert body["version"] == __version__ and body["daemon"] is None
    assert body["summary"]["health"] == "ok" and body["rules"][0]["copies"] == 2


async def test_status_includes_the_daemon(state: Path) -> None:
    daemon = FakeDaemon()
    async with client(state, daemon) as c:
        body = (await c.get("/api/status")).json()
    assert body["daemon"] == daemon.snapshot().to_json()


async def test_upcoming(state: Path) -> None:
    async with client(state) as c:
        response = await c.get("/api/upcoming", params={"hours": 48, "rule": "family"})
    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["from", "to", "hours", "schema", "events"] and body["hours"] == 48
    assert [(e["title"], e["delivery"]) for e in body["events"]] == [
        ("Standup", "delivered"),
        ("Lunch", "delivered"),
        ("Standup", "delivered"),
    ]


@pytest.mark.parametrize(("given", "used"), [("0", 1), ("-5", 1), ("1", 1), ("168", 168), ("1000", 168)])
async def test_upcoming_hours_are_clamped(state: Path, given: str, used: int) -> None:
    async with client(state) as c:
        body = (await c.get("/api/upcoming", params={"hours": given})).json()
    assert body["hours"] == used


async def test_upcoming_defaults_to_24_hours(state: Path) -> None:
    async with client(state) as c:
        body = (await c.get("/api/upcoming")).json()
    assert body["hours"] == 24 and len(body["events"]) == 2


async def test_upcoming_rejects_an_unknown_rule(state: Path) -> None:
    async with client(state) as c:
        response = await c.get("/api/upcoming", params={"rule": "nope"})
    assert response.status_code == 404 and response.json()["error"]["kind"] == "unknown_rule"


def add_runs(state_dir: Path, count: int) -> None:
    with Store(state_dir / "state.db") as store:
        for i in range(count):
            at = NOW + timedelta(minutes=i + 1)
            store.record_runs(
                [RunRecord("family", at, at, "ok", 1, 0, 0, 0, 1, None)],
                [ChangeRecord("family", f"o{i}", "updated", at, "Weekly review")],
            )


async def test_runs(state: Path) -> None:
    add_runs(state, 3)
    async with client(state) as c:
        response = await c.get("/api/runs")
        one = (await c.get("/api/runs", params={"rule": "family", "limit": 1})).json()
        other = (await c.get("/api/runs", params={"rule": "retired"})).json()
    assert response.status_code == 200 and list(response.json()) == ["runs"]
    assert [r["id"] for r in response.json()["runs"]] == [4, 3, 2, 1]
    assert [r["id"] for r in one["runs"]] == [4]
    assert other == {"runs": []}  # history outlives the config: any rule name is accepted


@pytest.mark.parametrize(
    ("route", "given", "used"),
    [
        ("runs", None, 50),
        ("runs", "0", 1),
        ("runs", "500", 500),
        ("runs", "501", 500),
        ("changes", None, 100),
        ("changes", "-1", 1),
        ("changes", "1000", 1000),
        ("changes", "5000", 1000),
    ],
)
async def test_limits_are_clamped(
    state: Path, monkeypatch: pytest.MonkeyPatch, route: str, given: str | None, used: int
) -> None:
    seen: list[int] = []
    name = f"build_{route}"
    real = getattr(views, name)

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["limit"])
        return real(*args, **kwargs)

    monkeypatch.setattr(views, name, spy)
    async with client(state) as c:
        response = await c.get(f"/api/{route}", params={} if given is None else {"limit": given})
    assert response.status_code == 200 and seen == [used]


async def test_changes(state: Path) -> None:
    add_runs(state, 2)
    async with client(state) as c:
        response = await c.get("/api/changes")
        of_run = (await c.get("/api/changes", params={"run_id": 2})).json()
        later = (
            await c.get("/api/changes", params={"since": (NOW + timedelta(minutes=2)).isoformat()})
        ).json()
        limited = (await c.get("/api/changes", params={"limit": 1, "rule": "family"})).json()
    assert response.status_code == 200 and list(response.json()) == ["changes"]
    body = response.json()["changes"]
    assert len(body) == 4 and body[0]["title"] == "Weekly review"
    assert [(c["run_id"], c["object_id"]) for c in of_run["changes"]] == [(2, "o0")]
    assert [c["object_id"] for c in later["changes"]] == ["o1"]
    assert [c["object_id"] for c in limited["changes"]] == ["o1"]


@pytest.mark.parametrize(
    ("route", "params"),
    [
        ("upcoming", {"hours": "soon"}),
        ("runs", {"limit": "many"}),
        ("changes", {"limit": "1.5"}),
        ("changes", {"since": "yesterday"}),
        ("changes", {"run_id": "first"}),
    ],
)
async def test_bad_parameters_are_400(state: Path, route: str, params: dict[str, str]) -> None:
    async with client(state) as c:
        response = await c.get(f"/api/{route}", params=params)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["kind"] == "bad_request" and next(iter(params)) in error["message"]


async def test_each_request_reads_the_current_state(state: Path) -> None:
    """A reader per request: a later request sees runs recorded after an earlier one."""
    async with client(state) as c:
        before = (await c.get("/api/runs")).json()["runs"]
        add_runs(state, 1)
        after = (await c.get("/api/runs")).json()["runs"]
    assert len(before) == 1 and len(after) == 2


# ----- errors -----------------------------------------------------------------


@pytest.mark.parametrize("route", ["status", "upcoming", "runs", "changes"])
async def test_missing_state_is_503(tmp_path: Path, route: str) -> None:
    async with client(tmp_path / "none") as c:
        response = await c.get(f"/api/{route}")
    assert response.status_code == 503 and response.json() == NO_STATE
    assert not (tmp_path / "none").exists()


@pytest.mark.parametrize("route", ["status", "upcoming", "runs", "changes"])
async def test_unreadable_state_is_500_without_a_traceback(tmp_path: Path, route: str) -> None:
    (tmp_path / "state.db").write_bytes(b"not a database, just some text " * 64)
    async with client(tmp_path) as c:
        response = await c.get(f"/api/{route}")
    assert response.status_code == 500
    assert response.json() == {
        "error": {"kind": "state_unreadable", "message": "The state database could not be read."}
    }


async def test_unknown_path_and_method(state: Path) -> None:
    async with client(state) as c:
        missing = await c.get("/api/nope")
        wrong = await c.post("/api/status")
    assert missing.status_code == 404 and missing.json()["error"]["kind"] == "not_found"
    assert wrong.status_code == 405 and wrong.json()["error"]["kind"] == "method_not_allowed"


# ----- trigger ----------------------------------------------------------------


async def test_trigger_requests_a_cycle(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    async with client(tmp_path, daemon) as c:  # no state needed
        every = await c.post("/api/trigger")
        empty = await c.post("/api/trigger", json={})
        one = await c.post("/api/trigger", json={"rule": "family"})
        null = await c.post("/api/trigger", json={"rule": None})
    assert every.status_code == 202 and every.json() == {"accepted": True, "rule": None}
    assert empty.json() == null.json() == {"accepted": True, "rule": None}
    assert one.status_code == 202 and one.json() == {"accepted": True, "rule": "family"}
    assert daemon.calls == [None, None, "family", None]


async def test_trigger_without_sync_is_409(tmp_path: Path) -> None:
    for daemon in (FakeDaemon(False), None):
        async with client(tmp_path, daemon) as c:
            response = await c.post("/api/trigger", json={"rule": "family"})
        assert response.status_code == 409 and response.json()["error"]["kind"] == "read_only"


async def test_trigger_for_an_unknown_rule_is_404(tmp_path: Path) -> None:
    daemon = FakeDaemon(ValueError("unknown rule 'nope'"))
    async with client(tmp_path, daemon) as c:
        response = await c.post("/api/trigger", json={"rule": "nope"})
    assert response.status_code == 404 and response.json()["error"]["kind"] == "unknown_rule"


@pytest.mark.parametrize("body", [b"{not json", b'{"rule": 3}', b"[1]", b'"family"'])
async def test_trigger_rejects_a_bad_body(tmp_path: Path, body: bytes) -> None:
    daemon = FakeDaemon()
    async with client(tmp_path, daemon) as c:
        response = await c.post("/api/trigger", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 400 and response.json()["error"]["kind"] == "bad_request"
    assert daemon.calls == []


async def test_responses_are_utf8_json(state: Path) -> None:
    with Store(state / "state.db") as store:
        at = datetime(2026, 10, 24, 9, tzinfo=UTC)
        store.record_runs(
            [RunRecord("family", at, at, "ok", 1, 0, 0, 0, 1, None)],
            [ChangeRecord("family", "o9", "created", at, "Café ☕")],
        )
    async with client(state) as c:
        response = await c.get("/api/changes", params={"limit": 1})
    assert json.loads(response.content.decode("utf-8"))["changes"][0]["title"] == "Café ☕"
