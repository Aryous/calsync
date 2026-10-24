"""Load and validate the TOML configuration.

Validation collects every problem before failing so a user fixes their file
in one pass. Nothing in the code refers to a concrete account: everything
comes from here.
"""

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, overload

from calsync.durations import parse_duration
from calsync.errors import ConfigError
from calsync.model import AlarmPolicy, FieldPolicy

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
    window: WindowSpec | None  # None: all of time (full sync)
    alarms: AlarmPolicy
    delete_guard: DeleteGuard
    fields: FieldPolicy = field(default_factory=FieldPolicy)
    on_target_edit: Literal["revert"] = "revert"  # a copy edited on the target is written back


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


def _table(raw: Any, where: str, problems: list[str]) -> Mapping[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return raw
    problems.append(f"{where}: must be a table")
    return {}


@overload
def _number(
    raw: Any,
    where: str,
    problems: list[str],
    *,
    kind: type[int],
    default: int,
    minimum: float | None = ...,
    maximum: float | None = ...,
    exclusive_min: bool = ...,
    exclusive_max: bool = ...,
    expect: str = ...,
) -> int: ...
@overload
def _number(
    raw: Any,
    where: str,
    problems: list[str],
    *,
    kind: type[float],
    default: float,
    minimum: float | None = ...,
    maximum: float | None = ...,
    exclusive_min: bool = ...,
    exclusive_max: bool = ...,
    expect: str = ...,
) -> float: ...
def _number(
    raw: Any,
    where: str,
    problems: list[str],
    *,
    kind: type[int] | type[float],
    default: Any,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_min: bool = False,
    exclusive_max: bool = False,
    expect: str = "",
) -> Any:
    """Validate a numeric value; record a problem and return ``default`` on failure."""
    if raw is None:
        return default
    label = expect or ("an integer" if kind is int else "a number")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        problems.append(f"{where}: must be {label}")
        return default
    if kind is int:
        if isinstance(raw, float) and not raw.is_integer():
            problems.append(f"{where}: must be {label}")
            return default
        value: Any = int(raw)
    else:
        value = float(raw)
    if not math.isfinite(value):
        problems.append(f"{where}: must be {label}")
        return default
    too_low = minimum is not None and (value <= minimum if exclusive_min else value < minimum)
    too_high = maximum is not None and (value >= maximum if exclusive_max else value > maximum)
    if too_low or too_high:
        problems.append(f"{where}: must be {label}")
        return default
    return value


def _optional_str(raw: Any, where: str, problems: list[str]) -> str | None:
    if raw is None or isinstance(raw, str):
        return raw
    problems.append(f"{where}: must be a string")
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
        if not isinstance(server_url, str) or not (
            server_url == "icloud" or server_url.startswith(("https://", "http://"))
        ):
            problems.append(
                f'{where}.server_url: required for caldav accounts: "icloud" (detects the global or '
                "China mainland region) or an http(s) URL"
            )
        if not isinstance(username, str) or not username:
            problems.append(f"{where}.username: required for caldav accounts")
    concurrency = _number(
        raw.get("max_concurrency"),
        f"{where}.max_concurrency",
        problems,
        kind=int,
        default=2,
        minimum=1,
        expect="an integer >= 1",
    )
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
    create = raw.get("create_if_missing", False)
    if not isinstance(create, bool):
        problems.append(f"{where}.create_if_missing: must be true or false")
        return None
    return CalendarSel(account, calendar, create)


def _alarms(raw: Any, where: str, problems: list[str]) -> AlarmPolicy:
    if raw in ("copy", "strip"):
        return raw
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        offsets = [_duration(item, where, problems) for item in raw]
        return tuple(o for o in offsets if o is not None)
    problems.append(f'{where}: expected "copy", "strip" or a list such as ["-5m", "0m"]')
    return "copy"


_FIELD_CHOICES: dict[str, tuple[str, ...]] = {
    "attendees": ("none", "notes"),
    "conference": ("copy", "none"),
    "content": ("full", "busy"),
}


def _fields(raw: Any, where: str, problems: list[str]) -> FieldPolicy:
    table = _table(raw, where, problems)
    values: dict[str, Any] = {}  # each value checked against _FIELD_CHOICES
    for key, value in table.items():
        choices = _FIELD_CHOICES.get(key)
        if choices is None:
            problems.append(f"{where}.{key}: unknown setting")
        elif value not in choices:
            allowed = ", ".join(f'"{c}"' for c in choices)
            problems.append(f"{where}.{key}: expected one of {allowed}")
        else:
            values[key] = value
    return FieldPolicy(**values)


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
    window: WindowSpec | None = None  # omitted: all of time
    window_ok = True
    if "window" in raw:
        window_raw = _table(raw.get("window"), f"{where}.window", problems)
        past = _duration(window_raw.get("past", "7d"), f"{where}.window.past", problems)
        future = _duration(window_raw.get("future", "180d"), f"{where}.window.future", problems)
        for label, value in (("past", past), ("future", future)):
            if value is not None and value < timedelta(0):
                problems.append(f"{where}.window.{label}: must not be negative")
        if past is None or future is None:
            window_ok = False  # keep collecting this rule's other problems
        else:
            window = WindowSpec(past, future)
    alarms = _alarms(raw.get("alarms", "copy"), f"{where}.alarms", problems)
    guard_raw = _table(raw.get("delete_guard"), f"{where}.delete_guard", problems)
    guard = DeleteGuard(
        max_ratio=_number(
            guard_raw.get("max_ratio"),
            f"{where}.delete_guard.max_ratio",
            problems,
            kind=float,
            default=0.5,
            minimum=0,
            maximum=1,
            exclusive_min=True,
            expect="a number in (0, 1]",
        ),
        max_count=_number(
            guard_raw.get("max_count"),
            f"{where}.delete_guard.max_count",
            problems,
            kind=int,
            default=10,
            minimum=0,
            expect="an integer >= 0",
        ),
    )
    fields = _fields(raw.get("fields"), f"{where}.fields", problems)
    if raw.get("on_target_edit", "revert") != "revert":
        problems.append(f'{where}.on_target_edit: only "revert" is supported')
    if source is None or target is None or not window_ok:
        return None
    return Rule(name, source, target, window, alarms, guard, fields)


def parse_config(data: Mapping[str, Any], base_dir: Path) -> Config:
    problems: list[str] = []

    google = _table(data.get("google"), "google", problems)
    secret_file = google.get("client_secret_file")
    client_secret_file = _resolve(secret_file, base_dir) if isinstance(secret_file, str) else None

    notify_raw = _table(data.get("notify"), "notify", problems)
    notify = NotifyConfig(
        bark_url=_optional_str(notify_raw.get("bark_url"), "notify.bark_url", problems),
        webhook_url=_optional_str(notify_raw.get("webhook_url"), "notify.webhook_url", problems),
        failure_threshold=_number(
            notify_raw.get("failure_threshold"),
            "notify.failure_threshold",
            problems,
            kind=int,
            default=3,
            minimum=1,
            expect="an integer >= 1",
        ),
    )

    runtime_raw = _table(data.get("runtime"), "runtime", problems)
    interval = _duration(runtime_raw.get("interval", "60s"), "runtime.interval", problems)
    if interval is not None and interval <= timedelta(0):
        problems.append("runtime.interval: must be positive")
        interval = None
    jitter = _number(
        runtime_raw.get("jitter"),
        "runtime.jitter",
        problems,
        kind=float,
        default=0.1,
        minimum=0,
        maximum=1,
        exclusive_max=True,
        expect="a number in [0, 1)",
    )

    accounts: dict[str, Account] = {}
    for name, raw in _table(data.get("accounts"), "accounts", problems).items():
        account = _account(name, raw, problems)
        if account is not None:
            accounts[name] = account

    rules: list[Rule] = []
    seen: set[str] = set()
    rules_raw = data.get("rules")
    if rules_raw is None:
        rules_raw = []
    elif not isinstance(rules_raw, list):
        problems.append("rules: must be an array of tables (use [[rules]])")
        rules_raw = []
    for index, raw in enumerate(rules_raw):
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
        runtime=RuntimeConfig(
            interval=interval if interval is not None else timedelta(seconds=60), jitter=jitter
        ),
    )
