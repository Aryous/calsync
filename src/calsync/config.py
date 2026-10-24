"""Load and validate the TOML configuration.

Validation collects every problem before failing so a user fixes their file
in one pass. Nothing in the code refers to a concrete account: everything
comes from here.
"""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from calsync.durations import parse_duration
from calsync.errors import ConfigError
from calsync.model import AlarmPolicy

Backend = Literal["keychain", "env", "file"]
Provider = Literal["google", "caldav"]
_BACKENDS = ("keychain", "env", "file")
_PROVIDERS = ("google", "caldav")


@dataclass(frozen=True, slots=True)
class CredentialRef:
    backend: Backend
    ref: str


@dataclass(frozen=True, slots=True)
class Account:
    name: str
    provider: Provider
    credential: CredentialRef
    server_url: str | None = None
    username: str | None = None
    max_concurrency: int = 2


@dataclass(frozen=True, slots=True)
class CalendarSel:
    account: str
    calendar: str
    create_if_missing: bool = False


@dataclass(frozen=True, slots=True)
class WindowSpec:
    past: timedelta
    future: timedelta


@dataclass(frozen=True, slots=True)
class DeleteGuard:
    max_ratio: float = 0.5
    max_count: int = 10


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    source: CalendarSel
    target: CalendarSel
    window: WindowSpec
    alarms: AlarmPolicy
    delete_guard: DeleteGuard


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    bark_url: str | None = None
    webhook_url: str | None = None
    failure_threshold: int = 3


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    interval: timedelta = timedelta(seconds=60)
    jitter: float = 0.1


@dataclass(frozen=True, slots=True)
class Config:
    accounts: Mapping[str, Account]
    rules: tuple[Rule, ...]
    google_client_secret_file: Path | None = None
    notify: NotifyConfig = NotifyConfig()
    runtime: RuntimeConfig = RuntimeConfig()


def default_config_path(environ: Mapping[str, str] = os.environ) -> Path:
    explicit = environ.get("CALSYNC_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".config" / "calsync" / "config.toml"


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError([f"config file not found: {path}"]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{path}: {exc}"]) from None
    return parse_config(data, base_dir=path.parent)


def _resolve(raw: str, base_dir: Path) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else base_dir / path


def _duration(raw: Any, where: str, problems: list[str]) -> timedelta | None:
    if not isinstance(raw, str):
        problems.append(f"{where}: expected a duration string such as '5m'")
        return None
    try:
        return parse_duration(raw)
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
        return None


def _account(name: str, raw: Any, problems: list[str]) -> Account | None:
    where = f"accounts.{name}"
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be a table")
        return None
    provider = raw.get("provider")
    if provider not in _PROVIDERS:
        problems.append(f"{where}.provider: must be one of {', '.join(_PROVIDERS)}")
        return None
    cred = raw.get("credential")
    if (
        not isinstance(cred, Mapping)
        or cred.get("backend") not in _BACKENDS
        or not isinstance(cred.get("ref"), str)
        or not cred["ref"]
    ):
        problems.append(f'{where}.credential: expected {{ backend = keychain|env|file, ref = "..." }}')
        return None
    server_url = raw.get("server_url")
    username = raw.get("username")
    if provider == "caldav":
        if not isinstance(server_url, str) or not server_url.startswith(("https://", "http://")):
            problems.append(f"{where}.server_url: required http(s) URL for caldav accounts")
        if not isinstance(username, str) or not username:
            problems.append(f"{where}.username: required for caldav accounts")
    concurrency = raw.get("max_concurrency", 2)
    if not isinstance(concurrency, int) or concurrency < 1:
        problems.append(f"{where}.max_concurrency: must be an integer >= 1")
        concurrency = 1
    return Account(
        name=name,
        provider=provider,
        credential=CredentialRef(backend=cred["backend"], ref=cred["ref"]),
        server_url=server_url,
        username=username,
        max_concurrency=concurrency,
    )


def _selection(
    raw: Any,
    where: str,
    accounts: Mapping[str, Account],
    provider: Provider,
    problems: list[str],
) -> CalendarSel | None:
    if not isinstance(raw, Mapping):
        problems.append(f'{where}: expected {{ account = "...", calendar = "..." }}')
        return None
    account, calendar = raw.get("account"), raw.get("calendar")
    ok = True
    if not isinstance(calendar, str) or not calendar:
        problems.append(f"{where}.calendar: required")
        ok = False
    if not isinstance(account, str) or account not in accounts:
        problems.append(f"{where}.account: unknown account {account!r}")
        return None
    if accounts[account].provider != provider:
        problems.append(f"{where}.account: {account!r} must be a {provider} account")
    if not ok or not isinstance(calendar, str):
        return None
    return CalendarSel(account, calendar, bool(raw.get("create_if_missing", False)))


def _alarms(raw: Any, where: str, problems: list[str]) -> AlarmPolicy:
    if raw in ("copy", "strip"):
        return raw
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        offsets = [_duration(item, where, problems) for item in raw]
        return tuple(o for o in offsets if o is not None)
    problems.append(f'{where}: expected "copy", "strip" or a list such as ["-5m", "0m"]')
    return "copy"


def _rule(index: int, raw: Any, accounts: Mapping[str, Account], problems: list[str]) -> Rule | None:
    where = f"rules[{index}]"
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be a table")
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        problems.append(f"{where}.name: required")
        return None
    where = f"rules[{name}]"
    source = _selection(raw.get("source"), f"{where}.source", accounts, "google", problems)
    target = _selection(raw.get("target"), f"{where}.target", accounts, "caldav", problems)
    window_raw = raw.get("window", {})
    past = _duration(window_raw.get("past", "7d"), f"{where}.window.past", problems)
    future = _duration(window_raw.get("future", "180d"), f"{where}.window.future", problems)
    for label, value in (("past", past), ("future", future)):
        if value is not None and value < timedelta(0):
            problems.append(f"{where}.window.{label}: must not be negative")
    alarms = _alarms(raw.get("alarms", "copy"), f"{where}.alarms", problems)
    guard_raw = raw.get("delete_guard", {})
    guard = DeleteGuard(
        max_ratio=float(guard_raw.get("max_ratio", 0.5)),
        max_count=int(guard_raw.get("max_count", 10)),
    )
    if not 0 < guard.max_ratio <= 1:
        problems.append(f"{where}.delete_guard.max_ratio: must be in (0, 1]")
    if source is None or target is None or past is None or future is None:
        return None
    return Rule(name, source, target, WindowSpec(past, future), alarms, guard)


def parse_config(data: Mapping[str, Any], base_dir: Path) -> Config:
    problems: list[str] = []

    google = data.get("google", {})
    secret_file = google.get("client_secret_file")
    client_secret_file = _resolve(secret_file, base_dir) if isinstance(secret_file, str) else None

    notify_raw = data.get("notify", {})
    notify = NotifyConfig(
        bark_url=notify_raw.get("bark_url"),
        webhook_url=notify_raw.get("webhook_url"),
        failure_threshold=int(notify_raw.get("failure_threshold", 3)),
    )

    runtime_raw = data.get("runtime", {})
    interval = _duration(runtime_raw.get("interval", "60s"), "runtime.interval", problems)
    jitter = float(runtime_raw.get("jitter", 0.1))
    if not 0 <= jitter < 1:
        problems.append("runtime.jitter: must be in [0, 1)")

    accounts: dict[str, Account] = {}
    for name, raw in (data.get("accounts") or {}).items():
        account = _account(name, raw, problems)
        if account is not None:
            accounts[name] = account

    rules: list[Rule] = []
    seen: set[str] = set()
    for index, raw in enumerate(data.get("rules") or []):
        rule = _rule(index, raw, accounts, problems)
        if rule is None:
            continue
        if rule.name in seen:
            problems.append(f"rules[{rule.name}]: duplicate rule name")
        seen.add(rule.name)
        rules.append(rule)

    if any(a.provider == "google" for a in accounts.values()) and client_secret_file is None:
        problems.append("google.client_secret_file: required when a google account is configured")

    if problems:
        raise ConfigError(problems)
    return Config(
        accounts=accounts,
        rules=tuple(rules),
        google_client_secret_file=client_secret_file,
        notify=notify,
        runtime=RuntimeConfig(interval=interval or timedelta(seconds=60), jitter=jitter),
    )
