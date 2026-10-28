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
        assert not (state / "state.db-wal").exists()  # read immutable: a kept reader would go stale
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


# ----- operations (the socket app only) --------------------------------------------------


class OpsDaemon(FakeDaemon):
    def __init__(self, answer: Any) -> None:
        super().__init__()
        self.op_answer = answer
        self.ops: list[tuple[str, dict[str, Any], bool, str]] = []

    async def run_op(self, name: str, args: dict[str, Any], dry_run: bool, *, source: str) -> Any:
        self.ops.append((name, args, dry_run, source))
        if isinstance(self.op_answer, Exception):
            raise self.op_answer
        return self.op_answer


async def test_an_op_runs_in_the_daemon(tmp_path: Path) -> None:
    from calsync.ops import OpResult

    daemon = OpsDaemon(OpResult("rule.pause", True, 'rule "family" paused', diff="d"))
    async with client(tmp_path, daemon) as c:
        response = await c.post("/api/ops/rule.pause", json={"args": {"name": "family"}, "dry_run": True})
        bare = await c.post("/api/ops/rule.pause")
    assert response.status_code == 200 and response.json()["summary"] == 'rule "family" paused'
    assert bare.status_code == 200
    assert daemon.ops == [("rule.pause", {"name": "family"}, True, "cli"), ("rule.pause", {}, False, "cli")]


async def test_op_errors(tmp_path: Path) -> None:
    from calsync.configfile import ConfigConflict
    from calsync.errors import ConfigError, RulePaused
    from calsync.ops import DaemonStopping, OpsUnavailable, TooSoon

    cases: list[tuple[Any, int, dict[str, Any]]] = [
        (ConfigError(["a", "b"]), 422, {"kind": "config", "message": "a; b", "problems": ["a", "b"]}),
        (
            ConfigConflict(),
            422,
            {
                "kind": "config_conflict",
                "message": "the file was changed outside calsync; reload and try again",
                "problems": ["the file was changed outside calsync; reload and try again"],
            },
        ),
        (OpsUnavailable("no ops here"), 409, {"kind": "read_only", "message": "no ops here"}),
        (
            RulePaused("family"),
            409,
            {"kind": "paused", "message": 'rule "family" is paused', "rule": "family"},
        ),
        (DaemonStopping(), 503, {"kind": "unavailable", "message": "The daemon is stopping."}),
        (
            TooSoon(),
            429,
            {
                "kind": "too_soon",
                "message": "A test push was sent less than 10 seconds ago; try again shortly.",
            },
        ),
    ]
    for answer, status, error in cases:
        async with client(tmp_path, OpsDaemon(answer)) as c:
            response = await c.post("/api/ops/rule.pause", json={"args": {"name": "family"}})
        assert (response.status_code, response.json()) == (status, {"error": error})
    async with client(tmp_path, None) as c:
        response = await c.post("/api/ops/rule.pause", json={})
    assert response.status_code == 409 and response.json()["error"]["kind"] == "read_only"


async def test_an_unknown_op_is_404(tmp_path: Path) -> None:
    daemon = OpsDaemon(None)
    async with client(tmp_path, daemon) as c:
        response = await c.post("/api/ops/rule.explode", json={})
    assert response.status_code == 404 and response.json()["error"]["kind"] == "unknown_op"
    assert daemon.ops == []


@pytest.mark.parametrize(
    "body", [b"{not json", b"[1]", b'{"args": [1]}', b'{"dry_run": "yes"}', b'{"args": {}, "extra": 1}']
)
async def test_an_op_rejects_a_bad_body(tmp_path: Path, body: bytes) -> None:
    daemon = OpsDaemon(None)
    async with client(tmp_path, daemon) as c:
        response = await c.post(
            "/api/ops/rule.pause", content=body, headers={"content-type": "application/json"}
        )
    assert response.status_code == 400 and response.json()["error"]["kind"] == "bad_request"
    assert daemon.ops == []


async def test_a_large_body_is_413(tmp_path: Path) -> None:
    daemon = OpsDaemon(None)
    big = b'{"args": {"name": "' + b"x" * (64 * 1024) + b'"}}'
    async with client(tmp_path, daemon) as c:
        op = await c.post("/api/ops/rule.pause", content=big, headers={"content-type": "application/json"})
        trigger = await c.post("/api/trigger", content=big, headers={"content-type": "application/json"})
    for response in (op, trigger):
        assert response.status_code == 413 and response.json()["error"]["kind"] == "too_large"
    assert daemon.ops == [] and daemon.calls == []


async def test_an_op_refuses_internal_arguments(tmp_path: Path) -> None:
    daemon = OpsDaemon(None)
    async with client(tmp_path, daemon) as c:
        response = await c.post(
            "/api/ops/rule.remove", json={"args": {"name": "family", "after_retire": True}}
        )
    assert response.status_code == 400 and response.json()["error"] == {
        "kind": "bad_request",
        "message": "after_retire: an internal argument",
    }
    assert daemon.ops == []


def test_ops_are_not_among_the_shared_routes(tmp_path: Path) -> None:
    """The operations come only with a source: ``"cli"`` on the socket, ``"ui"`` on the web UI
    (behind its own checks, ``test_web``)."""
    routes = api.api_routes(config_provider=config, state_dir=tmp_path, daemon=None)
    assert all("/api/ops" not in getattr(r, "path", "") for r in routes)
    app_routes = api.create_app(config_provider=config, state_dir=tmp_path, daemon=None).routes
    assert any(getattr(r, "path", "") == "/api/ops/{name}" for r in app_routes)


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


async def test_an_unexpected_view_error_is_a_plain_500_without_its_text(
    state: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("和朋友吃午饭 at https://calendar.example.invalid/e/123")

    monkeypatch.setattr(views, "build_status", broken)
    caplog.set_level(logging.DEBUG)
    async with client(state) as c:  # ASGITransport re-raises whatever reaches the server middleware
        response = await c.get("/api/status")
    assert response.status_code == 500
    assert response.json() == {"error": {"kind": "internal", "message": "Internal error."}}
    for text in (caplog.text, response.text):
        assert "和朋友吃午饭" not in text and "example.invalid" not in text
    assert "RuntimeError" in caplog.text and "Traceback" not in caplog.text


async def test_trigger_for_a_paused_rule_is_409_paused(tmp_path: Path) -> None:
    from calsync.errors import RulePaused

    daemon = FakeDaemon(RulePaused("family"))
    async with client(tmp_path, daemon) as c:
        response = await c.post("/api/trigger", json={"rule": "family"})
    assert response.status_code == 409
    assert response.json() == {"error": {"kind": "paused", "message": 'rule "family" is paused'}}


# ----- events, one event, config, calendars ------------------------------------------------------

DAY = {"from": "2026-10-24T00:00:00+08:00", "to": "2026-10-26T00:00:00+08:00"}


async def test_events(state: Path) -> None:
    async with client(state) as c:
        response = await c.get("/api/events", params=DAY)
        by_query = (await c.get("/api/events", params={**DAY, "q": "LUNCH"})).json()
        by_rule = (await c.get("/api/events", params={**DAY, "rule": "family"})).json()
        by_target = (await c.get("/api/events", params={**DAY, "target": "dav:Family"})).json()
    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["from", "to", "schema", "truncated", "events"]
    assert [(e["title"], e["start"]) for e in body["events"]] == [
        ("Standup", "2026-10-24T18:00:00+08:00"),
        ("Lunch", "2026-10-25T12:00:00+08:00"),
        ("Standup", "2026-10-25T18:00:00+08:00"),
    ]
    assert {d["state"] for e in body["events"] for d in e["deliveries"]} == {"delivered"}
    assert [e["title"] for e in by_query["events"]] == ["Lunch"]
    assert by_rule == body and by_target == body


async def test_events_default_to_a_week_from_today(state: Path) -> None:
    async with client(state) as c:
        body = (await c.get("/api/events")).json()
    start, end = datetime.fromisoformat(body["from"]), datetime.fromisoformat(body["to"])
    local = NOW.astimezone()
    assert start == local.replace(hour=0, minute=0, second=0, microsecond=0)
    assert end - start == timedelta(days=7)


async def test_events_accept_dates(state: Path) -> None:
    async with client(state) as c:
        body = (await c.get("/api/events", params={"from": "2026-10-24", "to": "2026-10-31"})).json()
    assert datetime.fromisoformat(body["from"]) == datetime(2026, 10, 24).astimezone()


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-10-01", "to": "2026-12-03"},  # 63 days
        {"from": "2026-10-24", "to": "2026-10-23"},  # backwards
        {"from": "soon"},
        {"to": "later"},
        {**DAY, "target": "no-colon"},
        {"from": "2040-01-01"},  # beyond ten years from now
        {"from": "9999-12-30"},  # its default end would overflow
        {"from": "9999-12-31T23:00"},  # local time: not representable as an instant
        {"from": "0001-01-01T00:00"},
        {"from": "2026-10-24T00:00+00:00", "to": "9999-12-31T23:59+00:00"},
    ],
)
async def test_bad_event_ranges_are_400(state: Path, params: dict[str, str]) -> None:
    async with client(state) as c:
        response = await c.get("/api/events", params=params)
    assert response.status_code == 400 and response.json()["error"]["kind"] == "bad_request"


async def test_events_range_of_62_days_is_fine(state: Path) -> None:
    async with client(state) as c:
        response = await c.get("/api/events", params={"from": "2026-10-01", "to": "2026-12-02"})
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("params", "kind"), [({"rule": "nope"}, "unknown_rule"), ({"target": "dav:Nowhere"}, "unknown_target")]
)
async def test_events_with_an_unknown_rule_or_target_are_404(
    state: Path, params: dict[str, str], kind: str
) -> None:
    async with client(state) as c:
        response = await c.get("/api/events", params={**DAY, **params})
    assert response.status_code == 404 and response.json()["error"]["kind"] == kind


async def test_one_event(state: Path) -> None:
    async with client(state) as c:
        events = (await c.get("/api/events", params=DAY)).json()["events"]
        response = await c.get("/api/event", params={"id": events[1]["id"]})
        missing = await c.get("/api/event")
        unknown = await c.get("/api/event", params={"id": "bm9wZQ"})
        garbled = await c.get("/api/event", params={"id": "%%%"})
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Lunch" and body["original"]["title"] == "Lunch"
    assert body["deliveries"][0]["differs"] == [] and body["deliveries"][0]["copy"]["title"] == "Lunch"
    assert missing.status_code == 400 and missing.json()["error"]["kind"] == "bad_request"
    for answer in (unknown, garbled):
        assert answer.status_code == 404 and answer.json()["error"]["kind"] == "not_found"


async def test_blocked(state: Path) -> None:
    async with client(state) as c:
        response = await c.get("/api/blocked")
        unknown = await c.get("/api/blocked", params={"rule": "nope"})
    assert response.status_code == 200 and response.json() == {"blocked": []}
    assert unknown.status_code == 404


async def test_config_is_masked(tmp_path: Path) -> None:
    async with client(tmp_path / "none") as c:  # needs no state
        response = await c.get("/api/config")
        revealing = await c.get("/api/config", params={"reveal": "1"})
    assert response.status_code == 200
    body = response.json()
    assert body == views.config_json(config()) and body["revealed"] is False
    assert revealing.json() == body  # --reveal exists only on the CLI
    assert body["accounts"] == {
        "me": {"name": "me", "provider": "google", "host": "Google Calendar", "credential_configured": True},
        "dav": {"name": "dav", "provider": "caldav", "host": "unused", "credential_configured": True},
    }
    assert body["google"] == {"client_secret_file": {"configured": True}}
    text = response.text
    assert str(Path.home()) not in text and "unused.json" not in text  # the client secret path
    assert '"u"' not in text and "username" not in text  # the CalDAV login (an Apple ID)
    assert "UNUSED" not in text and '"ref"' not in text  # the credential ref
    assert "http://unused" not in text  # the server URL


async def test_config_cuts_push_urls_to_scheme_and_host(tmp_path: Path) -> None:
    from dataclasses import replace

    from calsync.config import NotifyConfig

    pushing = replace(
        config(),
        notify=NotifyConfig(
            bark_url="https://bark.example.com/DEVICEKEY", webhook_url="https://hooks.example.com/h/T0KEN"
        ),
    )
    app = api.create_app(config_provider=lambda: pushing, state_dir=tmp_path, daemon=None, clock=lambda: NOW)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://calsync") as c:
        response = await c.get("/api/config")
    notify = response.json()["notify"]
    assert (notify["bark_url"], notify["webhook_url"]) == (
        "https://bark.example.com",
        "https://hooks.example.com",
    )
    assert "DEVICEKEY" not in response.text and "T0KEN" not in response.text


async def test_config_history(state: Path) -> None:
    with Store(state / "state.db") as store:
        for i in range(3):
            store.record_config_change(
                at=NOW + timedelta(minutes=i),
                source="cli",
                summary=f"change {i}",
                before="secret text",
                after_fingerprint=f"f{i}",
            )
    async with client(state) as c:
        body = (await c.get("/api/config/history")).json()
        one = (await c.get("/api/config/history", params={"limit": 1})).json()
    async with client(state / "none") as c:
        empty = (await c.get("/api/config/history")).json()
    assert [c["summary"] for c in body["changes"]] == ["change 2", "change 1", "change 0"]
    assert list(body["changes"][0]) == ["id", "at", "source", "summary", "after_fingerprint", "undoes"]
    assert "secret text" not in json.dumps(body)
    assert [c["summary"] for c in one["changes"]] == ["change 2"]
    assert empty == {"changes": []}


async def test_calendars(state: Path) -> None:
    from calsync.store import AccountCalendar

    with Store(state / "state.db") as store:
        store.save_account_calendars("me", [AccountCalendar("me", GCAL, "Family", "owner", NOW)], NOW)
    async with client(state) as c:
        body = (await c.get("/api/calendars", params={"account": "me"})).json()
        none = (await c.get("/api/calendars", params={"account": "dav"})).json()
        unknown = await c.get("/api/calendars", params={"account": "nope"})
        missing = await c.get("/api/calendars")
    assert body == {
        "account": "me",
        "fetched_at": NOW.isoformat(),
        "calendars": [{"id": GCAL, "name": "Family", "role": "owner"}],
    }
    assert none == {"account": "dav", "fetched_at": None, "calendars": []}
    assert unknown.status_code == 404 and unknown.json()["error"]["kind"] == "unknown_account"
    assert missing.status_code == 400


NEW_ROUTES = {
    "/api/events",
    "/api/event",
    "/api/blocked",
    "/api/config",
    "/api/config/history",
    "/api/calendars",
}


def test_the_new_routes_are_shared_by_the_socket_and_the_web_app(tmp_path: Path) -> None:
    from calsync.web.app import create_web_app

    socket_app = api.create_app(config_provider=config, state_dir=tmp_path, daemon=None)
    web_app = create_web_app(config_provider=config, state_dir=tmp_path, daemon=None, listen_host="127.0.0.1")
    for app in (socket_app, web_app):
        assert {getattr(r, "path", "") for r in app.routes} >= NEW_ROUTES


async def test_62_calendar_days_across_a_dst_change_are_fine(state: Path) -> None:
    """Local midnights on both sides of a fall-back change are 62 days and one hour apart."""
    async with client(state) as c:
        ok = await c.get(
            "/api/events", params={"from": "2026-10-01T00:00-04:00", "to": "2026-12-02T00:00-05:00"}
        )
        over = await c.get(
            "/api/events", params={"from": "2026-10-01T00:00-04:00", "to": "2026-12-02T00:01-05:00"}
        )
    assert ok.status_code == 200 and over.status_code == 400
