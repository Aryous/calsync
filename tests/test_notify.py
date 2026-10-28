"""Notifications (v3 §11, v2 §4.6): which alerts a cycle raises (deduplicated in the ``alert``
table), their texts and links, delivery to Bark and a webhook, containment of failures, and
where they are raised: the daemon, `calsync run`, `trigger` and `approve` in-process, and
`calsync notify test`.

Every push goes through ``httpx.MockTransport`` (the sealed test environment refuses the
network); the module's ``TRANSPORT`` is patched for the paths that build their own notifier.
"""

import asyncio
import io
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from calsync import configfile, notify, ops, wiring
from calsync.cli import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, main
from calsync.config import Config, NotifyConfig
from calsync.engine import RuleResult
from calsync.errors import ConfigError
from calsync.planner import Actual, Plan
from calsync.store import RunRecord, Store
from tests.harness import GCAL, NOW, config_for, rule
from tests.test_daemon import World, eventually, until
from tests.test_daemon_reload import ACCOUNTS, FAMILY, TEXT

pytestmark = pytest.mark.anyio

BARK = "https://bark.example.com/DEVICEKEY0123"
HOOK = "https://hooks.example.com/calsync/s3cr3t"
SECRET_TITLE = "Dentist appointment 7731"  # synthetic; must never reach a log line
TITLES = [SECRET_TITLE, *(f"Event {i}" for i in range(1, 7))]  # seven planned deletes
APPROVED = "calsync 放行了删除"  # the ``approved`` push's title


def notified(*, language: str = "zh", public_url: str | None = None, threshold: int = 3) -> Config:
    config = config_for([rule("family"), rule("work", source="Work", target="Work")])
    return replace(
        config,
        notify=NotifyConfig(
            bark_url=BARK,
            webhook_url=HOOK,
            failure_threshold=threshold,
            public_url=public_url,
            language=language,
        ),
    )


def result(
    name: str = "family",
    status: str = "ok",
    *,
    kind: str | None = None,
    account: str | None = None,
    failed: int = 0,
    fingerprint: str | None = None,
    titles: list[str] | None = None,
    deletes: int | None = None,
) -> RuleResult:
    r = RuleResult(name)
    r.status = status  # type: ignore[assignment]
    r.failed = failed
    r.error_kind = kind  # type: ignore[assignment]
    r.error_account = account
    if status == "failed" or (status == "partial" and failed):
        r.error = "TransientError: boom"
    if status == "blocked":
        r.error, r.error_kind = "guard", "guard"
        r.blocked_fingerprint = fingerprint or "fp-a"
        r.blocked_titles = list(titles if titles is not None else TITLES)
        n = deletes if deletes is not None else len(r.blocked_titles)
        r.plan = Plan(delete=[Actual(f"o{i}", f"h{i}", None, None, None) for i in range(n)])
    return r


class Cycles:
    """A state database; ``run`` records one cycle's runs, then evaluates its alerts."""

    def __init__(self, root: Path, config: Config | None = None) -> None:
        self.store_path = root / "state.db"
        self.store = Store(self.store_path)
        self.config = config or notified()
        self.now = NOW

    def run(self, *results: RuleResult) -> list[notify.Alert]:
        self.now += timedelta(minutes=1)
        self.store.record_runs(
            [
                RunRecord(
                    r.rule,
                    self.now,
                    self.now,
                    r.status,
                    0,
                    0,
                    0,
                    r.failed,
                    0,
                    r.error,
                    error_kind=r.error_kind,
                    error_account=r.error_account,
                )
                for r in results
            ]
        )
        return notify.evaluate(
            {r.rule: r for r in results}, store=self.store, config=self.config, now=self.now
        )


@pytest.fixture
def cycles(tmp_path: Path) -> Iterator[Cycles]:
    c = Cycles(tmp_path)
    yield c
    c.store.close()


def kinds(alerts: list[notify.Alert]) -> list[tuple[str, str | None, str | None]]:
    return [(a.event, a.rule, a.account) for a in alerts]


# ----- what a cycle raises ------------------------------------------------------------------


def test_an_auth_failure_alerts_once_per_account_until_it_recovers(cycles: Cycles) -> None:
    auth = result(status="failed", kind="auth", account="me")
    [alert] = cycles.run(auth)
    assert (alert.event, alert.account, alert.rule) == ("auth", "me", "family")
    assert (alert.title, alert.body) == ("「me」需要重新连接", "运行 calsync auth me")
    assert alert.level == "timeSensitive"
    assert cycles.run(auth) == []  # still open: not again
    assert cycles.run(auth, result("work", "failed", kind="auth", account="me")) == []  # same account
    assert set(cycles.store.open_alerts()) == {"auth:me"}
    [back] = cycles.run(result())
    assert (back.event, back.account, back.title) == ("recovered", "me", "「me」恢复正常")
    assert cycles.store.open_alerts() == {}
    assert cycles.run(result()) == []  # recovered once
    assert kinds(cycles.run(auth)) == [("auth", "family", "me")]  # a new problem alerts again


def test_an_auth_failure_without_an_account_names_the_rules_target(cycles: Cycles) -> None:
    [alert] = cycles.run(result(status="failed", kind="auth"))
    assert alert.account == "dav" and alert.body == "运行 calsync auth dav"


def test_a_blocked_set_alerts_once_per_fingerprint(cycles: Cycles) -> None:
    [alert] = cycles.run(result(status="blocked", fingerprint="fp-a", deletes=23))
    assert (alert.event, alert.rule, alert.level) == ("blocked", "family", "timeSensitive")
    assert alert.title == "calsync 拦下了一批删除"
    shown = "、".join(TITLES[:5])
    assert alert.body == f"「family」要删除 23 个日程，可能是误删，已暂停这条线路。例如：{shown}…"
    assert cycles.run(result(status="blocked", fingerprint="fp-a")) == []
    # Another set a minute later replaces the open one, without a push (damped).
    assert cycles.run(result(status="blocked", fingerprint="fp-b")) == []
    assert set(cycles.store.open_alerts()) == {"blocked:family:fp-b"}  # the old set is superseded
    [back] = cycles.run(result())
    assert (back.event, back.rule, back.title) == ("recovered", "family", "「family」恢复正常")
    assert cycles.store.open_alerts() == {}


def test_a_growing_blocked_set_pushes_once_per_half_hour(cycles: Cycles) -> None:
    """A set that grows by one each minute: one push, then the next only 30 minutes later."""
    counts = [
        len(cycles.run(result(status="blocked", fingerprint=f"fp-{i}", deletes=40 + i))) for i in range(31)
    ]
    assert counts == [1] + [0] * 29 + [1]  # the push at minute 0 is carried until minute 30
    assert set(cycles.store.open_alerts()) == {"blocked:family:fp-30"}
    [back] = cycles.run(result())
    assert back.event == "recovered"  # a carried key recovers as any other
    assert cycles.store.open_alerts() == {}


def test_a_blocked_set_that_doubles_pushes_at_once(cycles: Cycles) -> None:
    assert len(cycles.run(result(status="blocked", fingerprint="a", deletes=10))) == 1
    assert cycles.run(result(status="blocked", fingerprint="b", deletes=19)) == []
    [doubled] = cycles.run(result(status="blocked", fingerprint="c", deletes=20))  # twice the push's 10
    assert "要删除 20 个日程" in doubled.body
    assert cycles.run(result(status="blocked", fingerprint="d", deletes=39)) == []  # vs. 20 now
    assert len(cycles.run(result(status="blocked", fingerprint="e", deletes=40))) == 1


def test_a_new_set_after_the_damping_pushes_again(cycles: Cycles) -> None:
    assert len(cycles.run(result(status="blocked", fingerprint="a", deletes=12))) == 1
    cycles.now += notify.DAMPING - timedelta(minutes=2)
    assert cycles.run(result(status="blocked", fingerprint="b", deletes=13)) == []  # 29 minutes on
    cycles.now += timedelta(minutes=1)
    assert len(cycles.run(result(status="blocked", fingerprint="c", deletes=13))) == 1  # 30 minutes on


def test_an_approval_is_the_blocks_only_push(cycles: Cycles) -> None:
    assert len(cycles.run(result(status="blocked"))) == 1
    notify.approving(cycles.store, "family")  # as approve does, with the lock
    assert cycles.store.open_alerts() == {}
    assert cycles.run(result()) == []  # the approval cycle: no recovered (approved was the push)


def test_an_approval_cycle_still_recovers_a_failing_rule(cycles: Cycles) -> None:
    for _ in range(3):
        cycles.run(result(status="failed"))
    assert len(cycles.run(result(status="blocked"))) == 1
    assert set(cycles.store.open_alerts()) == {"failing:family", "blocked:family:fp-a"}
    notify.approving(cycles.store, "family")
    assert kinds(cycles.run(result())) == [("recovered", "family", None)]


def test_an_approval_cycle_that_fails_and_blocks_again_pushes_again(cycles: Cycles) -> None:
    assert len(cycles.run(result(status="blocked", fingerprint="fp-a"))) == 1
    notify.approving(cycles.store, "family")
    assert cycles.run(result(status="failed")) == []  # the approval cycle failed (one plain failure)
    [again] = cycles.run(result(status="blocked", fingerprint="fp-a"))  # the same set: not silent
    assert again.event == "blocked"
    notify.approving(cycles.store, "family")
    assert kinds(cycles.run(result(status="failed", kind="auth", account="me"))) == [("auth", "family", "me")]
    assert kinds(cycles.run(result(status="blocked", fingerprint="fp-a"))) == [("blocked", "family", None)]


def test_a_blocked_body_without_titles_and_with_few(cycles: Cycles) -> None:
    [alert] = cycles.run(result(status="blocked", titles=[], deletes=12))
    assert alert.body == "「family」要删除 12 个日程，可能是误删，已暂停这条线路。"
    [two] = cycles.run(result("work", "blocked", titles=["A\nB", "C"]))
    assert two.body.endswith("例如：A B、C")  # one line each, no ellipsis when all are shown


def test_failing_alerts_once_at_the_threshold(cycles: Cycles) -> None:
    failing = result(status="failed")
    assert cycles.run(failing) == [] and cycles.run(failing) == []
    [alert] = cycles.run(failing)
    assert (alert.event, alert.rule, alert.level) == ("failing", "family", None)
    assert alert.title == "「family」连续 3 次同步失败"
    assert cycles.run(failing) == []
    [back] = cycles.run(result())
    assert back.event == "recovered"
    assert cycles.run(failing) == [] and cycles.run(failing) == []  # counted afresh
    assert kinds(cycles.run(failing)) == [("failing", "family", None)]


def test_a_partial_run_with_failed_items_counts_as_a_failure(cycles: Cycles) -> None:
    for _ in range(2):
        assert cycles.run(result(status="partial", failed=1)) == []
    assert kinds(cycles.run(result(status="partial", failed=2))) == [("failing", "family", None)]
    assert cycles.run(result(status="partial"))[0].event == "recovered"  # no failed items: healthy


def test_blocked_and_auth_cycles_never_raise_failing(cycles: Cycles) -> None:
    for _ in range(5):
        cycles.run(result(status="blocked"), result("work", "failed", kind="auth", account="me"))
    assert set(cycles.store.open_alerts()) == {"blocked:family:fp-a", "auth:me"}


def test_failing_counts_plain_failures_only(cycles: Cycles) -> None:
    for _ in range(60):  # an hour of auth failures: one auth push
        cycles.run(result(status="failed", kind="auth", account="me"))
    # The account works again for "work"; "family" then fails once, transiently: 1, not 61.
    alerts = cycles.run(result(status="failed"), result("work"))
    assert kinds(alerts) == [("recovered", None, "me")]
    assert cycles.run(result(status="failed")) == []
    [alert] = cycles.run(result(status="failed"))  # three plain failures: one push
    assert (alert.event, alert.title) == ("failing", "「family」连续 3 次同步失败")
    assert cycles.run(result(status="failed")) == []


def test_failing_waits_while_the_rule_is_blocked(cycles: Cycles) -> None:
    cycles.run(result(status="blocked"))
    for _ in range(4):
        assert cycles.run(result(status="failed")) == []  # on hold: the blocked push stands


def test_failing_after_an_auth_failure_is_still_pushed(cycles: Cycles) -> None:
    """``auth:me`` stays open until a rule on "me" succeeds; a plain failure that follows (the
    user reconnected, but the target calendar is gone) must still reach the user."""
    [alert] = cycles.run(result("work", "failed", kind="auth", account="me"))
    assert alert.event == "auth"
    assert cycles.run(result("work", "failed")) == []
    assert cycles.run(result("work", "failed")) == []
    [alert] = cycles.run(result("work", "failed"))  # three plain failures: one push
    assert alert.event == "failing"


def test_status_still_counts_every_run_that_is_not_ok(cycles: Cycles) -> None:
    from calsync.views import consecutive_failures

    cycles.run(result(status="blocked"))
    cycles.run(result(status="failed", kind="auth", account="me"))
    cycles.run(result(status="failed"))
    assert consecutive_failures(cycles.store, "family") == 3


def test_keys_of_rules_and_accounts_no_longer_configured_close_silently(tmp_path: Path) -> None:
    from calsync.store import OpenAlert

    c = Cycles(tmp_path)
    try:
        old = {
            "failing:gone": OpenAlert(NOW),
            "blocked:gone:fp": OpenAlert(NOW),
            "auth:broken": OpenAlert(NOW),
        }
        c.store.update_alerts(opened={**old, "auth:me": OpenAlert(NOW), "failing:work": OpenAlert(NOW)})
        assert c.run(result(status="failed")) == []
        assert set(c.store.open_alerts()) == {"auth:me", "failing:work"}
    finally:
        c.store.close()


def test_closed_alerts_are_pruned_after_30_days(cycles: Cycles) -> None:
    import sqlite3

    from calsync.store import OpenAlert

    old, recent = NOW - timedelta(days=31), NOW - timedelta(days=29)
    cycles.store.update_alerts(opened={"failing:x": OpenAlert(old), "failing:y": OpenAlert(recent)})
    cycles.store.update_alerts(closed=["failing:x", "failing:y"])
    cycles.run(result())
    with sqlite3.connect(cycles.store_path) as db:
        assert [r[0] for r in db.execute("SELECT key FROM alert")] == ["failing:y"]


def test_two_rules_that_recover_together_each_say_so_once(cycles: Cycles) -> None:
    for _ in range(3):
        cycles.run(result(status="failed"), result("work", "failed"))
    alerts = cycles.run(result(), result("work"))
    assert kinds(alerts) == [("recovered", "family", None), ("recovered", "work", None)]


def test_a_rule_that_never_alerted_recovers_silently(cycles: Cycles) -> None:
    assert cycles.run(result(status="failed")) == []
    assert cycles.run(result()) == []


def test_the_threshold_comes_from_the_config(tmp_path: Path) -> None:
    c = Cycles(tmp_path, notified(threshold=1))
    try:
        assert kinds(c.run(result(status="failed"))) == [("failing", "family", None)]
    finally:
        c.store.close()


def test_a_store_failure_is_contained(cycles: Cycles, caplog: pytest.LogCaptureFixture) -> None:
    cycles.store.close()
    caplog.set_level(logging.DEBUG, logger="calsync")
    assert notify.evaluate({"family": result()}, store=cycles.store, config=cycles.config, now=NOW) == []
    assert "alerts not evaluated (ProgrammingError)" in caplog.messages


# ----- approved, reload_failed ------------------------------------------------------------------


def test_every_approval_notifies(cycles: Cycles) -> None:
    first = notify.approved("family", 23, "cli", cycles.config, NOW)
    second = notify.approved("family", 23, "ui", cycles.config, NOW)
    assert first.title == "calsync 放行了删除"
    assert first.body == "已放行 23 个删除（来自 命令行）"
    assert second.body == "已放行 23 个删除（来自 界面）"
    assert (first.event, first.rule) == ("approved", "family")
    assert cycles.store.open_alerts() == {}  # nothing to deduplicate


def released(origin: str = "ui", *, deletes: int = 3, status: str = "ok", failed: int = 0) -> RuleResult:
    """A run whose cycle let a blocked set through: an approval (``approved_by`` its origin) or
    ``calsync run --allow-deletes`` (``run``)."""
    r = result(status=status, failed=failed)
    r.approved, r.approved_by = origin != "run", origin
    r.plan = Plan(delete=[Actual(f"o{i}", f"h{i}", None, None, None) for i in range(deletes)])
    return r


def test_the_cycle_that_releases_a_set_pushes_the_approval(cycles: Cycles) -> None:
    assert len(cycles.run(result(status="blocked"))) == 1
    notify.approving(cycles.store, "family")  # as approve does, with the lock
    [alert] = cycles.run(released())
    assert (alert.event, alert.rule, alert.body) == ("approved", "family", "已放行 3 个删除（来自 界面）")
    assert alert.title == "calsync 放行了删除"
    assert cycles.store.open_alerts() == {}  # nothing to deduplicate: every release pushes


def test_a_release_whose_writes_then_fail_is_still_pushed(cycles: Cycles) -> None:
    """The set was let through (its ``approved`` row is written): pushed whatever the writes did."""
    alerts = cycles.run(released("cli", status="partial", failed=1))
    assert kinds(alerts) == [("approved", "family", None)]


def test_a_cycle_that_releases_nothing_pushes_no_approval(cycles: Cycles) -> None:
    assert len(cycles.run(result(status="blocked"))) == 1
    notify.approving(cycles.store, "family")
    # Stale (blocks again with another set), failed, synced without the approval: no ``approved``.
    assert kinds(cycles.run(result(status="blocked", fingerprint="fp-b"))) == [("blocked", "family", None)]
    notify.approving(cycles.store, "family")
    assert cycles.run(result(status="failed")) == []
    assert cycles.run(result()) == []


def test_allow_deletes_past_a_block_pushes_an_approval_and_no_recovered(cycles: Cycles) -> None:
    assert len(cycles.run(result(status="blocked"))) == 1
    [alert] = cycles.run(released("run", deletes=12))  # no approve: the block's key is open
    assert (alert.event, alert.body) == ("approved", "已放行 12 个删除（来自 calsync run --allow-deletes）")
    assert cycles.store.open_alerts() == {}  # the block closed with the release, silently
    english = notify.approved("family", 12, "run", notified(language="en"), NOW)
    assert english.body == "12 deletions approved (from calsync run --allow-deletes)"


def test_a_reload_failure_alerts_once_and_is_damped(cycles: Cycles) -> None:
    def failed(fingerprint: str, at: timedelta) -> list[notify.Alert]:
        return notify.reload_failed(cycles.store, fingerprint, 2, cycles.config, NOW + at)

    [alert] = failed("f1", timedelta())
    assert (alert.event, alert.title) == ("reload_failed", "配置文件有 2 处错误，仍按原配置运行")
    assert failed("f1", timedelta(minutes=1)) == []
    assert failed("f2", timedelta(minutes=5)) == []  # another text replaces it within 30 minutes
    assert set(cycles.store.open_alerts()) == {"reload"}  # one key
    assert failed("f2", timedelta(minutes=40)) == []  # the same text: reported (as carried)
    assert len(failed("f3", timedelta(minutes=30))) == 1  # 30 minutes after the push
    notify.reload_ok(cycles.store, NOW)
    assert cycles.store.open_alerts() == {}  # closed, without a push
    assert len(failed("f1", timedelta(minutes=31))) == 1


# ----- texts and links --------------------------------------------------------------------------


def test_english_texts(tmp_path: Path) -> None:
    c = Cycles(tmp_path, notified(language="en", threshold=1))
    try:
        [auth] = c.run(result(status="failed", kind="auth", account="me"))
        assert (auth.title, auth.body) == ("Account me needs to be connected again", "Run: calsync auth me")
        [blocked] = c.run(result("work", "blocked", titles=["A", "B"]))
        assert blocked.title == "calsync held back some deletions"
        assert blocked.body == (
            "work would delete 2 events, possibly by mistake; this rule is on hold. For example: A, B"
        )
        assert [a.title for a in c.run(result())] == ["Account me is working again"]
        [failing] = c.run(result(status="failed"))
        assert failing.title == "family failed to sync 1 time in a row"
        [reload] = notify.reload_failed(c.store, "f", 1, c.config, NOW)
        assert reload.title == "The config file has 1 error; the previous config keeps running"
    finally:
        c.store.close()
    approved = notify.approved("family", 1, "ui", notified(language="en"), NOW)
    assert (approved.title, approved.body) == (
        "calsync let deletions through",
        "1 deletion approved (from the web UI)",
    )


def test_links_point_at_the_ui_when_public_url_is_set(tmp_path: Path) -> None:
    c = Cycles(tmp_path, notified(public_url="https://calsync.example.com/"))
    try:
        [auth] = c.run(result(status="failed", kind="auth", account="me"))
        assert auth.url == "https://calsync.example.com/#/account/me"
        [blocked] = c.run(result("work", "blocked"))
        assert blocked.url == "https://calsync.example.com/#/rule/work"
    finally:
        c.store.close()
    assert notify.link(notified(public_url="http://h.example.com:8766"), "rule", "a b/ü") == (
        "http://h.example.com:8766/#/rule/a%20b%2F%C3%BC"
    )
    assert notify.approved("family", 1, "cli", notified(), NOW).url is None


# ----- delivery ----------------------------------------------------------------------------------


class Pushes:
    """A fake Bark server and webhook receiver; ``answer`` decides each response."""

    def __init__(self, answer: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.answer = answer or self.ok

    @staticmethod
    def ok(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bark.example.com":
            return httpx.Response(200, json={"code": 200, "message": "success", "timestamp": 1})
        return httpx.Response(204)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def bodies(self, host: str = "bark.example.com") -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.host == host]


def an_alert(**changes: Any) -> notify.Alert:
    base = notify.Alert(
        "blocked",
        "calsync 拦下了一批删除",
        f"例如：{SECRET_TITLE}",
        rule="family",
        account=None,
        url="https://calsync.example.com/#/rule/family",
        level="timeSensitive",
        at=NOW,
    )
    return replace(base, **changes)


async def test_bark_and_webhook_payloads() -> None:
    pushes = Pushes()
    await notify.Notifier(notified().notify, transport=pushes.transport()).send([an_alert()])
    bark, hook = pushes.requests
    assert (bark.method, str(bark.url)) == ("POST", BARK)  # the device key stays in the path
    assert bark.headers["content-type"] == "application/json"
    assert json.loads(bark.content) == {
        "title": "calsync 拦下了一批删除",
        "body": f"例如：{SECRET_TITLE}",
        "group": "calsync",
        "url": "https://calsync.example.com/#/rule/family",
        "level": "timeSensitive",
    }
    assert (hook.method, str(hook.url), hook.headers["content-type"]) == ("POST", HOOK, "application/json")
    assert json.loads(hook.content) == {
        "event": "blocked",
        "rule": "family",
        "account": None,
        "title": "calsync 拦下了一批删除",
        "body": f"例如：{SECRET_TITLE}",
        "url": "https://calsync.example.com/#/rule/family",
        "at": "2026-10-24T08:00:00+00:00",
    }


async def test_bark_without_url_or_level_omits_them() -> None:
    pushes = Pushes()
    config = NotifyConfig(bark_url=BARK)
    await notify.Notifier(config, transport=pushes.transport()).send([an_alert(url=None, level=None)])
    [body] = pushes.bodies()
    assert set(body) == {"title", "body", "group"}


async def test_nothing_configured_sends_nothing() -> None:
    pushes = Pushes()
    await notify.Notifier(NotifyConfig(), transport=pushes.transport()).send([an_alert()])
    assert pushes.requests == []


def refuse_bark(request: httpx.Request) -> httpx.Response:
    if request.url.host == "bark.example.com":
        return httpx.Response(400, json={"code": 400, "message": f"bad {request.url}", "timestamp": 1})
    return httpx.Response(204)


def bark_says_no(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"code": 500, "message": "push failed", "timestamp": 1})


def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"cannot reach {request.url}")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (refuse_bark, ["notification failed (bark, PushRefused)"]),
        (
            bark_says_no,  # Bark's own code decides; a webhook's 200 is success whatever the body
            ["notification failed (bark, PushRefused)"],
        ),
        (
            unreachable,
            ["notification failed (bark, ConnectError)", "notification failed (webhook, ConnectError)"],
        ),
    ],
)
async def test_a_failed_push_is_contained_and_logged_by_type(
    caplog: pytest.LogCaptureFixture, answer: Callable[[httpx.Request], httpx.Response], expected: list[str]
) -> None:
    caplog.set_level(logging.DEBUG, logger="calsync")
    pushes = Pushes(answer)
    await notify.Notifier(notified().notify, transport=pushes.transport()).send([an_alert()])  # no raise
    failures = [m for m in caplog.messages if m.startswith("notification failed")]
    assert failures == expected
    assert_private(caplog)


async def test_a_hanging_push_times_out(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify, "SEND_TIMEOUT", 0.05)
    caplog.set_level(logging.DEBUG, logger="calsync")

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    config = NotifyConfig(bark_url=BARK)
    await asyncio.wait_for(notify.Notifier(config, transport=httpx.MockTransport(hang)).send([an_alert()]), 2)
    assert "notification failed (bark, TimeoutError)" in caplog.messages
    assert_private(caplog)


def assert_private(caplog: pytest.LogCaptureFixture) -> None:
    """No title, URL, device key or body text in any calsync log line."""
    text = caplog.text
    for secret in (SECRET_TITLE, "DEVICEKEY0123", "s3cr3t", "example.com", "例如", "拦下"):
        assert secret not in text, secret


# ----- notify test ------------------------------------------------------------------------------


async def test_the_test_push_reports_each_channel() -> None:
    pushes = Pushes(refuse_bark)
    report = await notify.Notifier(notified().notify, transport=pushes.transport()).test("zh")
    assert report == {"bark": "failed (PushRefused)", "webhook": "ok"}
    assert pushes.bodies()[0] == {
        "title": "calsync 测试通知",
        "body": "如果你看到这条，推送设置正确。",
        "group": "calsync",
    }
    assert json.loads(pushes.requests[1].content)["event"] == "test"
    only = await notify.Notifier(NotifyConfig(webhook_url=HOOK), transport=Pushes().transport()).test("en")
    assert only == {"bark": "not configured", "webhook": "ok"}


def cli(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, out=out, stdin=io.StringIO(""))
    return code, out.getvalue()


class Files:
    """A config file (``notify`` appended) and a state directory, with no daemon."""

    def __init__(self, root: Path, notify_text: str = "") -> None:
        self.path = root / "config.toml"
        self.path.write_text(TEXT + notify_text)
        self.state = root / "s"

    def cli(self, *argv: str) -> tuple[int, str]:
        return cli(["--config", str(self.path), "--state-dir", str(self.state), *argv])


BOTH = f'\n[notify]\nbark_url = "{BARK}"\nwebhook_url = "{HOOK}"\n'


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch) -> Pushes:
    p = Pushes()
    monkeypatch.setattr(notify, "TRANSPORT", p.transport())
    return p


def test_notify_test_exit_codes(short_dir: Path, pushes: Pushes) -> None:
    code, out = Files(short_dir).cli("notify", "test")
    assert (
        code == EXIT_CONFIG
        and out == "bark: not configured\nwebhook: not configured\nno channel is configured\n"
    )
    files = Files(short_dir, BOTH)
    code, out = files.cli("notify", "test")
    assert (code, out) == (EXIT_OK, "bark: ok\nwebhook: ok\n")
    code, out = files.cli("notify", "test", "--json")
    body = json.loads(out)
    assert code == EXIT_OK and body["op"] == "notify.test" and body["changed"] is False
    assert body["data"] == {"channels": {"bark": "ok", "webhook": "ok"}}
    assert BARK not in out and HOOK not in out
    pushes.answer = refuse_bark
    code, out = files.cli("notify", "test")
    assert (code, out) == (EXIT_FAILED, "bark: failed (PushRefused)\nwebhook: ok\n")
    assert len(pushes.requests) == 6


def test_notify_test_refuses_a_dry_run() -> None:
    from calsync.ops import run_op

    with pytest.raises(ConfigError, match="no dry run"):
        run_op(None, "notify.test", {}, True)  # type: ignore[arg-type]  # refused before ctx is used


# ----- config ------------------------------------------------------------------------------------


def test_language_is_validated_and_settable(short_dir: Path) -> None:
    assert configfile.validate_text(TEXT, short_dir).notify.language == "zh"
    with pytest.raises(ConfigError, match=r'notify.language: must be "zh" or "en"'):
        configfile.validate_text(TEXT + '\n[notify]\nlanguage = "fr"\n', short_dir)
    files = Files(short_dir)
    code, out = files.cli("config", "set", "notify.language", "en", "--json")
    assert code == EXIT_OK, out
    assert configfile.validate_text(files.path.read_text(), short_dir).notify.language == "en"
    code, out = files.cli("config", "get")
    assert json.loads(out)["notify"]["language"] == "en"


# ----- where alerts are raised ----------------------------------------------------------------------


async def test_the_daemon_alerts_without_waiting_for_the_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="calsync")
    release = asyncio.Event()
    seen: list[httpx.Request] = []

    async def slow(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        await release.wait()
        return httpx.Response(200, json={"code": 200, "message": "success", "timestamp": 1})

    monkeypatch.setattr(notify, "TRANSPORT", httpx.MockTransport(slow))
    world = World(block=True)
    world.fake.put_event(GCAL, _event("x", SECRET_TITLE))
    world.deny.add("me")  # the first build has a revoked credential: an auth failure
    text = ACCOUNTS + FAMILY + f'\n[notify]\nbark_url = "{BARK}"\n'
    daemon = world.daemon(configfile.validate_text(text, tmp_path), tmp_path)
    task = asyncio.create_task(daemon.run(world.stop))
    await until(lambda: len(world.delays) == 1)  # the first cycle ended while its push hangs
    await eventually(lambda: len(seen) == 1)
    assert json.loads(seen[0].content)["title"] == "「me」需要重新连接"
    daemon.request_cycle()  # the scheduler is not held up by the push
    await until(lambda: len(world.delays) == 2)
    release.set()
    await eventually(lambda: len(seen) == 2)  # the rule recovered (rebuilt adapters)
    assert json.loads(seen[1].content)["title"] == "「me」恢复正常"  # the account works again
    world.stop.set()
    await asyncio.wait_for(task, 5)
    assert_private(caplog)


async def test_the_daemon_cancels_hanging_pushes_when_it_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify, "SEND_TIMEOUT", 60.0)
    started = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(notify, "TRANSPORT", httpx.MockTransport(hang))
    monkeypatch.setattr(notify, "SHUTDOWN_GRACE", 0.05)
    world = World(block=True)
    world.deny.add("me")
    text = ACCOUNTS + FAMILY + f'\n[notify]\nbark_url = "{BARK}"\n'
    daemon = world.daemon(configfile.validate_text(text, tmp_path), tmp_path)
    task = asyncio.create_task(daemon.run(world.stop))
    await asyncio.wait_for(started.wait(), 5)
    world.stop.set()
    await asyncio.wait_for(task, 2)  # not the 60 s the push would take


async def test_the_daemon_notifies_approvals_and_reload_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_approve import Running

    pushes = Pushes()
    monkeypatch.setattr(notify, "TRANSPORT", pushes.transport())
    text = TEXT + f'\n[notify]\nbark_url = "{BARK}"\n'
    d = Running(tmp_path)
    d.path.write_text(text)
    d.daemon = d.world.daemon(configfile.validate_text(text, tmp_path), tmp_path, config_path=d.path)
    await d.start()
    await eventually(lambda: len(pushes.bodies()) == 1)
    assert pushes.bodies()[0]["title"] == "calsync 拦下了一批删除"
    assert (await d.approve(rule="family", wait=True)).data["status"] == "approved"
    await eventually(lambda: len(pushes.bodies()) == 2)
    assert pushes.bodies()[1]["body"] == "已放行 12 个删除（来自 命令行）"  # from the cycle that released it
    d.path.write_text(text + "\n[[rules]]\nname = 5\n")  # an invalid edit
    d.daemon.request_cycle()  # the check before the cycle sees it
    await eventually(lambda: len(pushes.bodies()) == 3)
    # One push per act: the approval cycle that went well sent no "recovered" after "approved".
    assert pushes.bodies()[2]["title"].startswith("配置文件有 ")
    await d.cycle("work")
    assert len(pushes.bodies()) == 3  # the same text is not reported twice
    await d.stop()
    assert [b["title"] for b in pushes.bodies()].count(APPROVED) == 1


def _notified_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Pushes]:
    """``test_approve.Running`` with a Bark channel, and the pushes it sends."""
    from tests.test_approve import Running

    pushes = Pushes()
    monkeypatch.setattr(notify, "TRANSPORT", pushes.transport())
    text = TEXT + f'\n[notify]\nbark_url = "{BARK}"\n'
    d = Running(tmp_path)
    d.path.write_text(text)
    d.daemon = d.world.daemon(configfile.validate_text(text, tmp_path), tmp_path, config_path=d.path)
    return d, pushes


@pytest.mark.parametrize("case", ["stale", "failed", "synced", "paused", "removed", "stopping"])
async def test_the_daemon_pushes_no_approval_that_releases_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """The ``approved`` push comes from the cycle that lets the set through, never from the
    approval itself: one that goes stale, fails, is not needed, or is abandoned sends none."""
    from calsync import daemon as daemon_module
    from tests.test_approve import COUNT, seed
    from tests.test_daemon_reload import write

    d, pushes = _notified_daemon(tmp_path, monkeypatch)
    fingerprint = await d.start(cancel=COUNT - 1 if case == "stale" else COUNT)
    await eventually(lambda: len(pushes.bodies()) == 1)  # blocked
    if case in ("stale", "failed", "synced"):
        if case == "stale":
            d.world.on_google = lambda: d.world.fake.cancel_event(GCAL, d.ids[-1])
        elif case == "synced":

            def shrink() -> None:
                seed(d.world.fake)
                for event_id in d.ids[:2]:
                    d.world.fake.cancel_event(GCAL, event_id)

            d.world.on_google = shrink
        else:

            async def broken(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("the state database is gone")

            monkeypatch.setattr(daemon_module, "run_cycle", broken)
        assert (await d.approve(rule="family", wait=True)).data["status"] == case
    else:
        daemon = d.daemon
        async with daemon._cycle_lock:  # pyright: ignore[reportPrivateUsage]  # no cycle meanwhile
            daemon.approve("family", fingerprint, COUNT, "cli", wait=False)
            if case == "stopping":
                d.world.stop.set()
            else:
                text = d.path.read_text()
                paused = text.replace('name = "family"', 'name = "family"\npaused = true', 1)
                write(d.path, paused if case == "paused" else text.replace(FAMILY, "", 1))
                await daemon._check_config()  # pyright: ignore[reportPrivateUsage]
    await d.stop()  # the pushes in flight are sent first
    assert APPROVED not in [b["title"] for b in pushes.bodies()]
    assert d.family_copies() == (COUNT - 2 if case == "synced" else COUNT)


async def test_the_daemon_pushes_and_records_a_guard_loosened_while_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raising the guard of a blocked rule lets its deletes through without ``approve``: it is
    pushed (every time) and recorded with its origin, like an approval."""
    from calsync.views import StateReader
    from tests.test_approve import Running

    pushes = Pushes()
    monkeypatch.setattr(notify, "TRANSPORT", pushes.transport())
    text = TEXT + f'\n[notify]\nbark_url = "{BARK}"\n'
    d = Running(tmp_path)
    d.path.write_text(text)
    d.daemon = d.world.daemon(configfile.validate_text(text, tmp_path), tmp_path, config_path=d.path)
    await d.start()
    await eventually(lambda: len(pushes.bodies()) == 1)  # blocked
    args = {"name": "family", "key": "delete_guard.max_count", "value": 100}
    await asyncio.wait_for(d.daemon.run_op("rule.set", args, False, source="ui"), 5)
    await eventually(lambda: len(pushes.bodies()) == 2)
    assert pushes.bodies()[1]["title"] == "calsync 的删除保护被放宽了"
    assert "来自 界面" in pushes.bodies()[1]["body"]
    with StateReader(tmp_path / "state.db") as reader:
        [row] = [c for c in reader.changes("family") if c.action == "guard_loosened"]
    assert (row.object_id, row.title) == ("guard", "delete guard loosened (ui)")
    await d.stop()


def _event(event_id: str, title: str) -> dict[str, Any]:
    from tests.factories import google_event

    return google_event(event_id, title, "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00")


class Here:
    """A config file with both channels and no daemon; the accounts are ``World``'s fakes."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, pushes: Pushes) -> None:
        self.world = World()
        self.world.fake.cancel_event(GCAL, "lunch")
        monkeypatch.setattr(wiring, "build_adapters", self.world.factory)
        self.files = Files(root, BOTH)
        self.pushes = pushes

    def titles(self) -> list[str]:
        return [b["title"] for b in self.pushes.bodies()]


def test_calsync_run_trigger_and_approve_send_in_process(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, pushes: Pushes, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.test_approve import seed

    caplog.set_level(logging.DEBUG, logger="calsync")
    here = Here(short_dir, monkeypatch, pushes)
    ids = seed(here.world.fake)
    here.world.fake.put_event(GCAL, _event("secret", SECRET_TITLE))
    assert here.files.cli("run")[0] == EXIT_OK
    assert pushes.requests == []  # all well: quiet
    for event_id in [*ids, "secret"]:
        here.world.fake.cancel_event(GCAL, event_id)
    assert here.files.cli("run", "--dry-run")[0] == EXIT_FAILED
    assert pushes.requests == []  # a dry run records nothing and alerts nothing
    assert here.files.cli("run")[0] == EXIT_FAILED  # blocked
    assert here.titles() == ["calsync 拦下了一批删除"]
    assert SECRET_TITLE in pushes.bodies()[0]["body"]  # "secret" sorts first by object id: shown
    assert len(pushes.bodies("hooks.example.com")) == 1
    assert here.files.cli("trigger", "--wait")[0] == EXIT_FAILED  # still blocked: nothing new
    assert len(here.titles()) == 1
    code, out = here.files.cli("approve", "family", "--yes")
    assert code == EXIT_OK, out
    assert here.titles()[1:] == [APPROVED]  # one push per act: no "recovered" after it
    assert pushes.bodies()[1]["body"] == "已放行 13 个删除（来自 命令行）"
    assert_private(caplog)


@pytest.mark.parametrize("case", ["stale", "synced"])
def test_an_in_process_approval_that_releases_nothing_pushes_no_approval(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, pushes: Pushes, case: str
) -> None:
    from tests.test_approve import COUNT, seed

    here = Here(short_dir, monkeypatch, pushes)
    ids = seed(here.world.fake)
    assert here.files.cli("run")[0] == EXIT_OK
    for event_id in ids[: COUNT - 1] if case == "stale" else ids:
        here.world.fake.cancel_event(GCAL, event_id)
    assert here.files.cli("run")[0] == EXIT_FAILED  # blocked
    assert here.titles() == ["calsync 拦下了一批删除"]

    def change() -> None:  # as the approval cycle reads Google
        if case == "stale":
            here.world.fake.cancel_event(GCAL, ids[-1])  # one more: another set
        else:
            seed(here.world.fake)  # all back, but two: under the guard
            for event_id in ids[:2]:
                here.world.fake.cancel_event(GCAL, event_id)

    here.world.on_google = change
    code, out = here.files.cli("approve", "family", "--yes", "--json")
    assert (code, json.loads(out)["data"]["status"]) == (EXIT_FAILED if case == "stale" else EXIT_OK, case)
    assert APPROVED not in here.titles()


def test_run_allow_deletes_past_a_block_is_recorded_and_pushed(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, pushes: Pushes
) -> None:
    """``calsync run --allow-deletes`` is a release like an approval: an ``approved`` row with
    origin ``run`` and its push (not a "recovered" one that reads as good news)."""
    from tests.test_approve import seed

    here = Here(short_dir, monkeypatch, pushes)
    ids = seed(here.world.fake)
    assert here.files.cli("run")[0] == EXIT_OK
    for event_id in ids:
        here.world.fake.cancel_event(GCAL, event_id)
    assert here.files.cli("run")[0] == EXIT_FAILED  # blocked
    assert here.files.cli("run", "--allow-deletes")[0] == EXIT_OK
    assert here.titles() == ["calsync 拦下了一批删除", APPROVED]
    assert pushes.bodies()[1]["body"] == "已放行 12 个删除（来自 calsync run --allow-deletes）"
    code, out = here.files.cli("changes", "--rule", "family", "--json")
    rows = [c for c in json.loads(out)["changes"] if c["action"] == "approved"]
    assert code == EXIT_OK and [c["title"] for c in rows] == ["12 deletes approved (run)"]
    # A cycle the guard would not have blocked releases nothing: no row, no push.
    seed(here.world.fake)
    assert here.files.cli("run")[0] == EXIT_OK
    for event_id in ids[:2]:
        here.world.fake.cancel_event(GCAL, event_id)
    assert here.files.cli("run", "--allow-deletes")[0] == EXIT_OK
    assert len(here.titles()) == 2
    code, out = here.files.cli("changes", "--rule", "family", "--json")
    assert len([c for c in json.loads(out)["changes"] if c["action"] == "approved"]) == 1


def test_an_approval_whose_cycle_fails_leaves_a_new_block_audible(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, pushes: Pushes
) -> None:
    from tests.test_approve import seed

    here = Here(short_dir, monkeypatch, pushes)
    ids = seed(here.world.fake)
    assert here.files.cli("run")[0] == EXIT_OK
    for event_id in ids:
        here.world.fake.cancel_event(GCAL, event_id)
    assert here.files.cli("run")[0] == EXIT_FAILED  # blocked
    here.world.deny.add("me")  # the approval cycle fails
    code, out = here.files.cli("approve", "family", "--yes")
    assert code != EXIT_OK, out
    # Nothing was released: no "approved" push, only what the failed cycle raised.
    assert here.titles()[1:] == ["「me」需要重新连接"]
    assert here.files.cli("run")[0] == EXIT_FAILED  # the same set blocks again: pushed again
    assert here.titles()[2:] == ["calsync 拦下了一批删除", "「me」恢复正常"]


def test_a_push_that_raises_never_fails_a_command(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, pushes: Pushes, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(self: notify.Notifier, alerts: Any) -> None:
        raise RuntimeError("boom " + SECRET_TITLE)

    caplog.set_level(logging.DEBUG, logger="calsync")
    monkeypatch.setattr(notify.Notifier, "send", broken)
    here = Here(short_dir, monkeypatch, pushes)
    here.world.deny.add("me")  # the first build only
    assert here.files.cli("run")[0] == 3  # "auth" raised; the auth exit code, as without alerts
    assert here.files.cli("trigger", "--rule", "family")[0] == EXIT_OK  # "recovered" raised
    assert caplog.messages.count("notification failed (RuntimeError)") == 2
    assert_private(caplog)


def test_a_failing_push_never_fails_calsync_run(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, pushes: Pushes
) -> None:
    pushes.answer = unreachable
    here = Here(short_dir, monkeypatch, pushes)
    here.world.deny.add("me")
    code, out = here.files.cli("run")
    assert code == 3 and "rule=family status=failed" in out  # the auth exit code, as without alerts
    assert len(pushes.requests) == 2


async def test_notify_test_through_the_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pushes = Pushes(refuse_bark)
    monkeypatch.setattr(notify, "TRANSPORT", pushes.transport())
    path = tmp_path / "config.toml"
    path.write_text(TEXT + BOTH)
    world = World(block=True)
    daemon = world.daemon(configfile.validate_text(TEXT + BOTH, tmp_path), tmp_path, config_path=path)
    task = asyncio.create_task(daemon.run(world.stop))
    await until(lambda: len(world.delays) == 1)
    result = await asyncio.wait_for(daemon.run_op("notify.test", {}, False, source="ui"), 5)
    assert result.data == {"channels": {"bark": "failed (PushRefused)", "webhook": "ok"}}
    assert result.summary == "test push: bark failed (PushRefused), webhook ok"
    world.stop.set()
    await asyncio.wait_for(task, 5)


async def test_a_daemon_without_sync_sends_a_test_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from calsync.daemon import Daemon

    pushes = Pushes()
    monkeypatch.setattr(notify, "TRANSPORT", pushes.transport())
    viewer = Daemon(configfile.validate_text(TEXT + BOTH, tmp_path), tmp_path, sync=False)
    result = await asyncio.wait_for(viewer.run_op("notify.test", {}, False, source="ui"), 5)
    assert result.data == {"channels": {"bark": "ok", "webhook": "ok"}}
    assert len(pushes.requests) == 2
    with pytest.raises(ConfigError, match="no dry run"):
        await viewer.run_op("notify.test", {}, True, source="ui")
    # A paired browser cannot push without limit: one test push per cooldown.
    with pytest.raises(ops.TooSoon):
        await viewer.run_op("notify.test", {}, False, source="ui")
    assert len(pushes.requests) == 2
    viewer._last_test_push -= ops.NOTIFY_TEST_COOLDOWN  # pyright: ignore[reportPrivateUsage, reportOptionalOperand]
    await asyncio.wait_for(viewer.run_op("notify.test", {}, False, source="ui"), 5)
    assert len(pushes.requests) == 4


async def test_a_test_push_that_is_refused_or_fails_leaves_the_cooldown_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a test push that reached someone starts the cooldown: a refused request (bad
    arguments) or a channel that failed (a wrong Bark URL, fixed and tried again) does not."""
    from calsync.daemon import Daemon

    pushes = Pushes(lambda request: httpx.Response(500))
    monkeypatch.setattr(notify, "TRANSPORT", pushes.transport())
    viewer = Daemon(configfile.validate_text(TEXT + BOTH, tmp_path), tmp_path, sync=False)
    with pytest.raises(ConfigError, match="unknown argument"):
        await viewer.run_op("notify.test", {"extra": 1}, False, source="ui")
    failed = await asyncio.wait_for(viewer.run_op("notify.test", {}, False, source="ui"), 5)
    assert failed.data == {"channels": {"bark": "failed (PushRefused)", "webhook": "failed (PushRefused)"}}
    pushes.answer = Pushes.ok  # the URL is fixed
    done = await asyncio.wait_for(viewer.run_op("notify.test", {}, False, source="ui"), 5)
    assert done.data == {"channels": {"bark": "ok", "webhook": "ok"}}
    with pytest.raises(ops.TooSoon):  # this one reached the person: the cooldown starts
        await viewer.run_op("notify.test", {}, False, source="ui")
    assert len(pushes.requests) == 4


async def test_concurrent_test_pushes_send_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from calsync.daemon import Daemon

    release = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return Pushes.ok(request)

    monkeypatch.setattr(notify, "TRANSPORT", httpx.MockTransport(slow))
    viewer = Daemon(configfile.validate_text(TEXT + BOTH, tmp_path), tmp_path, sync=False)
    first = asyncio.create_task(viewer.run_op("notify.test", {}, False, source="ui"))
    await asyncio.sleep(0)
    with pytest.raises(ops.TooSoon):  # one is in flight
        await viewer.run_op("notify.test", {}, False, source="ui")
    release.set()
    assert (await asyncio.wait_for(first, 5)).data["channels"]["bark"] == "ok"


def test_trigger_in_process_sends(short_dir: Path, monkeypatch: pytest.MonkeyPatch, pushes: Pushes) -> None:
    here = Here(short_dir, monkeypatch, pushes)
    here.world.deny.add("me")
    assert here.files.cli("trigger", "--rule", "family")[0] == 3
    assert here.titles() == ["「me」需要重新连接"]
    assert here.files.cli("trigger", "--rule", "family")[0] == EXIT_OK
    assert here.titles() == ["「me」需要重新连接", "「me」恢复正常"]
