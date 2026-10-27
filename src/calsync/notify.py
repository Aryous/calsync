"""Notifications (v3 §11, v2 §4.6): quiet while all is well, a push when something needs a person.

Alerts:

- ``auth`` (per account): an account's credential stopped working;
- ``blocked`` (per rule and blocked set): the mass-delete guard held deletes back (the count and
  up to ``EXAMPLES`` titles);
- ``failing`` (per rule): the rule failed ``failure_threshold`` cycles in a row (blocked and auth
  failures have their own alerts and are not counted as this one);
- ``recovered``: the first ``ok`` run of a rule after an open ``failing`` / ``blocked`` alert of
  that rule (one push for the rule), or of an ``auth`` alert of one of its accounts (one push
  for the account);
- ``reload_failed`` (per config text): an edit made by hand is invalid (the count of problems,
  never their text);
- ``approved``: blocked deletes were approved (rule, count, origin). Never deduplicated.

Deduplication lives in the state DB's ``alert`` table (written only by the run-lock holder): an
open key (``auth:<account>``, ``blocked:<rule>:<fingerprint>``, ``failing:<rule>``,
``reload:<fingerprint>``) is not sent again; ``recovered`` (or a valid config) closes it. A key
is opened before its push is sent, so a push that fails is not retried.

Delivery: a JSON POST to ``bark_url`` and/or ``webhook_url``, each bounded by ``SEND_TIMEOUT``.
A failure is logged as ``notification failed (<channel>, <ExceptionType>)``: never the URL (a
Bark URL carries the device key), never the text. Titles go into push bodies (they leave the
machine through Bark's servers and Apple's), never into log lines.

Bark API, read 2026-10-28: https://github.com/Finb/bark-server/blob/master/docs/API_V2.md and the
server's routes (``route_push.go``, ``router.go``, master at 640deaa, 2026-10-10). ``POST
/<device_key>`` with ``Content-Type: application/json`` is handled by the V2 handler (the key in
the path wins over one in the body); fields used here: ``title``, ``body``, ``group``, ``url``
(opened on tap) and ``level`` (``critical`` | ``active`` | ``timeSensitive`` | ``passive``). The
answer is ``{"code": 200, "message": "success", "timestamp": …}``; an error answers with HTTP
status equal to its ``code``. So ``bark_url`` is the device URL (``https://api.day.app/<key>``)
and a push succeeds on HTTP 2xx with ``code`` 200 (or no JSON body, for a proxy in front).
"""

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from calsync import debug
from calsync.config import Config, NotifyConfig, Rule
from calsync.http import RequestLog, make_client
from calsync.store import Store
from calsync.views import _consecutive_failures, _is_ok  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
    from calsync.engine import RuleResult

log = logging.getLogger(__name__)

SEND_TIMEOUT = 5.0  # seconds, per push (read when used: tests shorten it)
SHUTDOWN_GRACE = 5.0  # seconds a stopping daemon waits for pushes still in flight
EXAMPLES = 5  # titles in a ``blocked`` push
GROUP = "calsync"
URGENT = "timeSensitive"  # Bark's level for alerts that need a person soon
# The user-facing texts. ``{s}`` is the English plural ending, ignored in Chinese.
TEXTS: dict[str, dict[str, str]] = {
    "zh": {
        "blocked.title": "calsync 拦下了一批删除",
        "blocked.body": "「{rule}」要删除 {count} 个日程，可能是误删，已暂停这条线路。",
        "blocked.examples": "例如：{titles}",
        "separator": "、",
        "approved.title": "calsync 放行了删除",
        "approved.body": "已放行 {count} 个删除（来自 {origin}）",
        "origin.cli": "命令行",
        "origin.ui": "界面",
        "origin.daemon": "calsync",
        "auth.title": "「{account}」需要重新连接",
        "auth.body": "运行 calsync auth {account}",
        "failing.title": "「{rule}」连续 {count} 次同步失败",
        "failing.body": "calsync 会继续重试。",
        "recovered.title": "「{name}」恢复正常",
        "recovered.account": "「{name}」恢复正常",
        "recovered.body": "同步已恢复。",
        "reload.title": "配置文件有 {count} 处错误，仍按原配置运行",
        "reload.body": "改好后保存，calsync 会自动重新读取。",
        "test.title": "calsync 测试通知",
        "test.body": "如果你看到这条，推送设置正确。",
    },
    "en": {
        "blocked.title": "calsync held back some deletions",
        "blocked.body": "{rule} would delete {count} event{s}, possibly by mistake; this rule is on hold.",
        "blocked.examples": " For example: {titles}",
        "separator": ", ",
        "approved.title": "calsync let deletions through",
        "approved.body": "{count} deletion{s} approved (from {origin})",
        "origin.cli": "the command line",
        "origin.ui": "the web UI",
        "origin.daemon": "calsync",
        "auth.title": "Account {account} needs to be connected again",
        "auth.body": "Run: calsync auth {account}",
        "failing.title": "{rule} failed to sync {count} time{s} in a row",
        "failing.body": "calsync keeps trying.",
        "recovered.title": "{name} is working again",
        "recovered.account": "Account {name} is working again",
        "recovered.body": "Syncing works again.",
        "reload.title": "The config file has {count} error{s}; the previous config keeps running",
        "reload.body": "Fix it and save; calsync reads it again by itself.",
        "test.title": "calsync test notification",
        "test.body": "If you can see this, notifications are set up correctly.",
    },
}


@dataclass(frozen=True, slots=True)
class Alert:
    event: str  # auth, blocked, failing, recovered, reload_failed, approved, test
    title: str
    body: str  # may carry event titles: pushed, never logged
    rule: str | None = None
    account: str | None = None
    url: str | None = None  # where the push's tap leads (needs ``public_url``)
    level: str | None = None  # Bark's interruption level
    at: datetime | None = None


class PushRefused(Exception):
    """The channel answered, but not with success (an HTTP error, or Bark's ``code``)."""


class _Texts:
    def __init__(self, config: Config) -> None:
        self._table = TEXTS.get(config.notify.language, TEXTS["zh"])

    def __call__(self, key: str, count: int | None = None, **values: Any) -> str:
        plural = "" if count == 1 else "s"
        return self._table[key].format(count=count, s=plural, **values)


def link(config: Config, kind: str, name: str) -> str | None:
    """The web UI's page for a rule or an account (``public_url`` + ``/#/<kind>/<name>``)."""
    base = config.notify.public_url
    if base is None:
        return None
    return f"{base.rstrip('/')}/#/{kind}/{quote(name, safe='')}"


# ----- what a cycle raises -------------------------------------------------------------------


def evaluate(
    results: Mapping[str, "RuleResult"], *, store: Store, config: Config, now: datetime
) -> list[Alert]:
    """The alerts a recorded cycle raises (its runs are in ``store`` already), with the
    ``alert`` table updated: keys opened for what is sent, closed for what recovered. Called by
    the lock holder only. A failure is logged by type and raises nothing (no alert then)."""
    try:
        return _evaluate(results, store, config, now)
    except Exception as exc:
        log.warning("alerts not evaluated (%s)", type(exc).__name__)
        debug.details(log, "alerts not evaluated", exc)
        return []


def _accounts(rule: Rule | None) -> list[str]:
    if rule is None:
        return []
    return [rule.target.account] if rule.retire else sorted({rule.source.account, rule.target.account})


def _auth_account(r: "RuleResult", rule: Rule | None) -> str | None:
    """The account an auth failure names; a failure inside the rule (no adapter named) is the
    target's, the side a rule writes to."""
    if r.error_kind != "auth" or _is_ok(r.status, r.failed):
        return None
    return r.error_account or (rule.target.account if rule is not None else None)


def _rule_keys(name: str, keys: Iterable[str]) -> set[str]:
    """The open keys of rule ``name`` (a fingerprint never contains a colon)."""
    return {k for k in keys if k == f"failing:{name}" or k.rsplit(":", 1)[0] == f"blocked:{name}"}


def _evaluate(
    results: Mapping[str, "RuleResult"], store: Store, config: Config, now: datetime
) -> list[Alert]:
    text = _Texts(config)
    was_open = store.open_alerts()  # one snapshot: the outcome never depends on the rules' order
    by_name = {r.name: r for r in config.rules}
    opened: list[str] = []
    closed: set[str] = set()
    alerts: list[Alert] = []
    # An account that failed this cycle has not recovered, whatever another rule says.
    failing_auth = {
        a for name, r in results.items() if (a := _auth_account(r, by_name.get(name))) is not None
    }

    def raise_(key: str, alert: Alert) -> None:
        if key in was_open or key in opened:
            return
        opened.append(key)
        alerts.append(alert)

    for name, r in results.items():
        rule = by_name.get(name)
        if _is_ok(r.status, r.failed):
            mine = _rule_keys(name, was_open)
            if mine:
                alerts.append(_recovered(text, config, now, rule=name))
            for account in _accounts(rule):
                key = f"auth:{account}"
                if key in was_open and key not in closed and account not in failing_auth:
                    alerts.append(_recovered(text, config, now, account=account))
                    closed.add(key)
            closed |= mine
        elif r.status == "blocked":
            if r.blocked_fingerprint is None:
                continue
            key = f"blocked:{name}:{r.blocked_fingerprint}"
            closed |= {k for k in _rule_keys(name, was_open) if k.startswith("blocked:") and k != key}
            raise_(key, _blocked(text, config, now, r))
        elif r.error_kind == "auth":
            account = _auth_account(r, rule)
            if account is None:
                continue
            alert = Alert(
                "auth",
                text("auth.title", account=account),
                text("auth.body", account=account),
                rule=name,
                account=account,
                url=link(config, "account", account),
                level=URGENT,
                at=now,
            )
            raise_(f"auth:{account}", alert)
        else:
            failures = _consecutive_failures(store, name)
            if failures >= config.notify.failure_threshold:
                alert = Alert(
                    "failing",
                    text("failing.title", failures, rule=name),
                    text("failing.body"),
                    rule=name,
                    url=link(config, "rule", name),
                    at=now,
                )
                raise_(f"failing:{name}", alert)
    if opened or closed:
        store.update_alerts(opened=opened, closed=closed - set(opened), at=now)
    return alerts


def _recovered(
    text: _Texts, config: Config, now: datetime, *, rule: str | None = None, account: str | None = None
) -> Alert:
    name = rule if rule is not None else account
    assert name is not None
    return Alert(
        "recovered",
        text("recovered.title" if rule is not None else "recovered.account", name=name),
        text("recovered.body"),
        rule=rule,
        account=account,
        url=link(config, "rule", rule) if rule is not None else link(config, "account", name),
        at=now,
    )


def _flat(title: str) -> str:
    return " ".join(title.split())


def _blocked(text: _Texts, config: Config, now: datetime, r: "RuleResult") -> Alert:
    count = len(r.plan.delete) if r.plan is not None else len(r.blocked_titles)
    shown = [t for t in (_flat(t) for t in r.blocked_titles) if t][:EXAMPLES]
    body = text("blocked.body", count, rule=r.rule)
    if shown:
        more = "…" if count > len(shown) else ""
        body += text("blocked.examples", titles=text("separator").join(shown) + more)
    return Alert(
        "blocked",
        text("blocked.title"),
        body,
        rule=r.rule,
        url=link(config, "rule", r.rule),
        level=URGENT,
        at=now,
    )


def approved(rule: str, count: int, origin: str, config: Config, now: datetime) -> Alert:
    """``approved``: sent for every approval (no key)."""
    text = _Texts(config)
    where = text(f"origin.{origin}") if f"origin.{origin}" in TEXTS["en"] else origin
    return Alert(
        "approved",
        text("approved.title"),
        text("approved.body", count, origin=where),
        rule=rule,
        url=link(config, "rule", rule),
        at=now,
    )


def reload_failed(
    store: Store, fingerprint: str, problems: int, config: Config, now: datetime
) -> list[Alert]:
    """``reload_failed`` for the config text ``fingerprint`` (once per text, until a config
    applies). Contained like ``evaluate``."""
    try:
        key = f"reload:{fingerprint}"
        if key in store.open_alerts():
            return []
        store.update_alerts(opened=[key], at=now)
    except Exception as exc:
        log.warning("alerts not evaluated (%s)", type(exc).__name__)
        debug.details(log, "alerts not evaluated", exc)
        return []
    text = _Texts(config)
    return [Alert("reload_failed", text("reload.title", problems), text("reload.body"), at=now)]


def reload_ok(store: Store, now: datetime) -> None:
    """A config applied (or the file is back to what runs): the ``reload_failed`` keys close,
    without a push."""
    try:
        keys = [k for k in store.open_alerts() if k.startswith("reload:")]
        if keys:
            store.update_alerts(closed=keys, at=now)
    except Exception as exc:
        log.warning("alerts not updated (%s)", type(exc).__name__)
        debug.details(log, "alerts not updated", exc)


# ----- delivery --------------------------------------------------------------------------------

# Tests replace it (the sealed environment has no network); None: the real network.
TRANSPORT: httpx.AsyncBaseTransport | None = None
CHANNELS = ("bark", "webhook")


class Notifier:
    """Sends alerts to the channels ``config`` names. Never raises for a failed push."""

    def __init__(self, config: NotifyConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._urls = {"bark": config.bark_url, "webhook": config.webhook_url}
        self._transport = transport

    def configured(self) -> list[str]:
        return [c for c in CHANNELS if self._urls[c]]

    def _client(self) -> httpx.AsyncClient:
        # Its own request log: pushes are not part of a cycle's request budget.
        transport = self._transport if self._transport is not None else TRANSPORT
        return make_client(RequestLog(), transport=transport, timeout=SEND_TIMEOUT)

    async def send(self, alerts: Sequence[Alert]) -> None:
        """Push every alert to every configured channel, concurrently, each bounded by
        ``SEND_TIMEOUT``."""
        channels = self.configured()
        if not alerts or not channels:
            return
        async with self._client() as client:
            await asyncio.gather(*(self._push(client, c, a) for a in alerts for c in channels))

    async def test(self, language: str) -> dict[str, str]:
        """One test push to each channel: ``ok``, ``failed (<Type>)`` or ``not configured``."""
        table = TEXTS.get(language, TEXTS["zh"])
        alert = Alert("test", table["test.title"], table["test.body"], at=datetime.now(UTC))
        report = {c: "not configured" for c in CHANNELS}
        channels = self.configured()
        if channels:
            async with self._client() as client:
                outcomes = await asyncio.gather(*(self._push(client, c, alert) for c in channels))
            for channel, failure in zip(channels, outcomes, strict=True):
                report[channel] = "ok" if failure is None else f"failed ({failure})"
        return report

    async def _push(self, client: httpx.AsyncClient, channel: str, alert: Alert) -> str | None:
        """Send one push; None on success, else the failure's type name (logged, never raised)."""
        url = self._urls[channel]
        assert url is not None
        try:
            async with asyncio.timeout(SEND_TIMEOUT):
                response = await client.post(url, json=_payload(channel, alert))
            _check(channel, response)
        except Exception as exc:  # the type only: the text can carry the URL (the device key)
            log.warning("notification failed (%s, %s)", channel, type(exc).__name__)
            debug.details(log, f"notification failed ({channel})", exc)
            return type(exc).__name__
        log.info("notification sent (%s, %s)", channel, alert.event)
        return None


def _payload(channel: str, alert: Alert) -> dict[str, Any]:
    if channel == "webhook":
        return {
            "event": alert.event,
            "rule": alert.rule,
            "account": alert.account,
            "title": alert.title,
            "body": alert.body,
            "url": alert.url,
            "at": (alert.at or datetime.now(UTC)).astimezone(UTC).isoformat(),
        }
    body: dict[str, Any] = {"title": alert.title, "body": alert.body, "group": GROUP}
    if alert.url is not None:
        body["url"] = alert.url
    if alert.level is not None:
        body["level"] = alert.level
    return body


def _check(channel: str, response: httpx.Response) -> None:
    if not 200 <= response.status_code < 300:
        raise PushRefused(f"HTTP {response.status_code}")
    if channel != "bark":
        return
    try:
        body = response.json()
    except ValueError:
        return  # not Bark's JSON (a proxy in front): the status decides
    if isinstance(body, dict) and body.get("code", 200) != 200:
        raise PushRefused(f"code {body.get('code')}")


class Outbox:
    """Pushes of a long-lived process (the daemon), sent in the background so a slow channel
    never delays the schedule; ``drain`` lets them finish for ``SHUTDOWN_GRACE`` seconds at
    most, then cancels the rest."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def post(self, config: NotifyConfig, alerts: Sequence[Alert]) -> None:
        notifier = Notifier(config)
        if not alerts or not notifier.configured():
            return
        task = asyncio.get_running_loop().create_task(notifier.send(alerts))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if not self._tasks:
            return
        tasks = set(self._tasks)
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if pending:
            log.warning("%d notification(s) not sent: the daemon stopped", len(pending))
