"""The operation layer (spec §1.3): every state-changing capability, once.

The CLI and the daemon's API are shells over ``run_op``. The caller holds the run lock (the
CLI for one operation, the daemon for its whole life) and hands in a writable ``Store``.

A config-changing operation reads the file with its fingerprint, edits it with tomlkit (comments
and layout survive), validates the whole new text like ``load_config``, and with ``dry_run``
returns the diff and stops. Otherwise ``configfile.write_config`` writes it (refusing if the
file changed meanwhile), a ``config_change`` row records who changed what, and
``on_config_written`` tells the daemon. An operation that would change nothing writes and
records nothing (``changed`` False).

Arguments come from the command line or JSON: values may be the CLI's text (``"30d:365d"``,
``"true"``, ``"-10m,0m"``) or already typed. Summaries are one plain sentence with config
names only, never a value: a value can be a push URL with a device key in it.
"""

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, overload

import tomlkit

from calsync import configfile, credentials, debug
from calsync.config import Config, CredentialRef
from calsync.errors import AuthError, CalsyncError, ConfigError, RulePaused, plain_error
from calsync.store import CONFIG_CHANGE_RETENTION, AccountCalendar, Store

if TYPE_CHECKING:
    from calsync.engine import CycleResult, RuleResult
    from calsync.notify import Alert
    from calsync.wiring import Adapters

log = logging.getLogger(__name__)

Source = Literal["cli", "ui", "daemon"]
# Arguments only calsync itself passes (``run_op`` called in-process by the daemon or by
# ``calsync run``); the transports (the socket, the CLI) refuse them.
INTERNAL_ARGS = frozenset({"after_retire"})
DRY_RUN = "dry run, nothing written: "
AUTH_REASON = "a person signs in (Google) or types the app-specific password (iCloud)"


class DaemonHooks(Protocol):
    """What operations may ask of the daemon they run in (cycles, approvals, adapters)."""

    @property
    def config(self) -> Config: ...

    def approve(
        self, rule: str, fingerprint: str, count: int, origin: Source, *, wait: bool
    ) -> "asyncio.Future[CycleResult | None] | None":
        """Store the approval for the rule's next cycle and request that cycle; with ``wait``,
        a future of that cycle's result (None: the cycle failed)."""
        ...

    def borrow_adapters(
        self, config: Config, names: set[str]
    ) -> "contextlib.AbstractAsyncContextManager[Adapters]":
        """Adapters for ``names`` while the operation runs (it holds the daemon's lock): the
        daemon's own where it has them, the others built for the operation and closed after."""
        ...

    @overload
    def request_cycle(self, rule: str | None = None, *, wait: Literal[False] = False) -> bool: ...

    @overload
    def request_cycle(
        self, rule: str | None = None, *, wait: Literal[True]
    ) -> "asyncio.Future[CycleResult | None]": ...

    def request_cycle(
        self, rule: str | None = None, *, wait: bool = False
    ) -> "bool | asyncio.Future[CycleResult | None]":
        """Request a cycle for ``rule`` (None: every rule); with ``wait``, a future of the first
        cycle that starts after the request and runs it (None: that cycle failed). ``ValueError``
        for an unknown rule, ``RulePaused`` for a paused one, ``DaemonStopping`` once the
        scheduler has stopped."""
        ...


class OpsUnavailable(CalsyncError):
    """This daemon runs no operations (``--no-sync``: no lock, no store)."""


class DaemonStopping(OpsUnavailable):
    """The daemon's scheduler has stopped: no cycle will run (the API answers 503)."""

    def __init__(self) -> None:
        super().__init__("The daemon is stopping.")


@dataclass
class OpContext:
    config_path: Path
    state_dir: Path
    store: Store  # writable; the caller holds the run lock
    source: Source
    clock: Callable[[], datetime]
    on_config_written: Callable[[configfile.Snapshot], None] = lambda s: None  # the daemon reloads here
    daemon: DaemonHooks | None = None
    # Set by an operation that has more to do once the daemon's lock is released (``approve``
    # waiting for a cycle); the daemon awaits it and answers with its result.
    followup: Callable[[], Awaitable["OpResult"]] | None = None


@dataclass(frozen=True)
class OpResult:
    op: str
    changed: bool
    summary: str  # one plain sentence, e.g. 'rule "family" paused'
    diff: str = ""  # unified config diff ("" when nothing changed)
    data: dict[str, Any] = field(default_factory=dict)
    needs_human: dict[str, str] | None = None  # {"command": ..., "reason": ...}

    def to_json(self) -> dict[str, Any]:
        """The stable JSON shape. ``needs_human`` follows D10: a flag, plus the command a
        person runs and why (``null`` when no person is needed)."""
        human = self.needs_human or {}
        return {
            "op": self.op,
            "changed": self.changed,
            "summary": self.summary,
            "diff": self.diff,
            "data": self.data,
            "needs_human": self.needs_human is not None,
            "command": human.get("command"),
            "reason": human.get("reason"),
        }

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> "OpResult":
        needs_human = None
        if body.get("needs_human"):
            needs_human = {"command": str(body.get("command") or ""), "reason": str(body.get("reason") or "")}
        return cls(
            op=str(body["op"]),
            changed=bool(body["changed"]),
            summary=str(body["summary"]),
            diff=str(body.get("diff") or ""),
            data=dict(body.get("data") or {}),
            needs_human=needs_human,
        )


# ----- arguments -------------------------------------------------------------------------


class _Args:
    """One operation's arguments: unknown names, missing and mistyped values are refused."""

    def __init__(self, op: str, args: Any, allowed: set[str]) -> None:
        if not isinstance(args, Mapping):
            raise ConfigError([f"{op}: the arguments must be an object"])
        unknown = sorted(str(k) for k in args if k not in allowed)
        if unknown:
            raise ConfigError([f'{op}: unknown argument "{unknown[0]}"'])
        self.op = op
        self._args: Mapping[str, Any] = args

    def text(self, key: str) -> str:
        value = self.opt_text(key)
        if value is None:
            raise ConfigError([f'{self.op}: "{key}" is required'])
        return value

    def opt_text(self, key: str) -> str | None:
        value = self._args.get(key)
        if value is not None and not isinstance(value, str):
            raise ConfigError([f'{self.op}: "{key}" must be a string'])
        return value

    def flag(self, key: str) -> bool:
        value = self._args.get(key, False)
        if value is None:
            return False
        if not isinstance(value, bool):
            raise ConfigError([f'{self.op}: "{key}" must be true or false'])
        return value

    def raw(self, key: str) -> Any:
        if key not in self._args:
            raise ConfigError([f'{self.op}: "{key}" is required'])
        return self._args[key]


def _bad(op: str, key: str, expected: str, text: str) -> ConfigError:
    return ConfigError([f'{op}: {key}: expected {expected}, got "{text}"'])


def _window(op: str, text: str) -> dict[str, str] | None:
    """``all`` (None: full sync) or ``PAST:FUTURE`` such as ``30d:365d``."""
    if text.strip() == "all":
        return None
    past, sep, future = text.partition(":")
    if not sep or not past.strip() or not future.strip():
        raise _bad(op, "window", '"all" or PAST:FUTURE such as 30d:365d', text)
    return {"past": past.strip(), "future": future.strip()}


def _alarms(text: str) -> str | list[str]:
    """``copy``, ``strip`` or a comma list of offsets such as ``-10m,0m``."""
    if text.strip() in ("copy", "strip"):
        return text.strip()
    return [part.strip() for part in text.split(",") if part.strip()]


def _bool(op: str, key: str, text: str) -> bool:
    if text.strip().lower() in ("true", "false"):
        return text.strip().lower() == "true"
    raise _bad(op, key, "true or false", text)


def _number(op: str, key: str, text: str) -> int | float:
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        raise _bad(op, key, "a number", text) from None


_RULE_NUMBERS = {"delete_guard.max_count", "delete_guard.max_ratio"}
_RULE_FLAGS = {"paused", "retire"}


def _rule_value(op: str, key: str, value: Any) -> Any:
    """A ``rule.set`` value: typed values pass (``parse_config`` checks them), text is parsed."""
    if not isinstance(value, str):
        return None if key in _RULE_FLAGS and value is False else value
    if key == "window":
        return _window(op, value)
    if key == "alarms":
        return _alarms(value)
    if key in _RULE_FLAGS:
        return _bool(op, key, value) or None  # false: the key is removed (false is the default)
    if value == "":
        return None
    if key in _RULE_NUMBERS:
        return _number(op, key, value)
    return value


_SETTING_NUMBERS = {"notify.failure_threshold", "runtime.jitter"}


def _setting_value(op: str, key: str, value: Any) -> Any:
    """A ``config.set`` value; empty text removes the setting (back to its default)."""
    if not isinstance(value, str):
        return value
    if value.strip() == "":
        return None
    if key == "web.allowed_hosts":
        return [part.strip() for part in value.split(",") if part.strip()]
    if key in _SETTING_NUMBERS:
        return _number(op, key, value)
    return value


def _selection(op: str, key: str, text: str) -> tuple[str, str]:
    """``ACCOUNT:CALENDAR`` (the calendar may contain colons; the account may not)."""
    account, sep, calendar = text.partition(":")
    if not sep or not account.strip() or not calendar.strip():
        raise ConfigError([f'{op}: {key}: expected ACCOUNT:CALENDAR, got "{text}"'])
    return account.strip(), calendar.strip()


# ----- the config-changing skeleton -------------------------------------------------------


@dataclass
class _Edit:
    summary: str  # what the change does
    same: str = ""  # the summary when the text does not change ("" -> summary)
    data: dict[str, Any] = field(default_factory=dict)
    needs_human: dict[str, str] | None = None
    after_write: Callable[[], str] | None = None  # runs after a real write; its text ends the summary


def _raw(doc: tomlkit.TOMLDocument) -> dict[str, Any]:
    return doc.unwrap()


def _config_op(
    ctx: OpContext,
    op: str,
    dry_run: bool,
    edit: Callable[[tomlkit.TOMLDocument], _Edit],
    *,
    new_text: Callable[[configfile.Snapshot], str] | None = None,
    undoes: int | None = None,
) -> OpResult:
    snapshot = configfile.read_snapshot(ctx.config_path)
    doc = configfile.load_doc(snapshot.text)
    change = edit(doc)
    text = new_text(snapshot) if new_text is not None else tomlkit.dumps(doc)
    configfile.validate_text(text, ctx.config_path.parent)  # also in a dry run
    if text == snapshot.text and undoes is None:
        data = {**change.data, "would_change": False} if dry_run else change.data
        return OpResult(op, False, change.same or change.summary, "", data, change.needs_human)
    diff = configfile.diff_text(snapshot.text, text)
    shown = _shown_diff(ctx, snapshot.text, text, diff)
    if dry_run:
        data = {**change.data, "would_change": diff != ""}
        return OpResult(op, False, DRY_RUN + change.summary, shown, data, change.needs_human)
    written = configfile.write_config(snapshot, text)
    recorded = True
    try:
        ctx.store.record_config_change(
            at=ctx.clock(),
            source=ctx.source,
            summary=change.summary,
            before=snapshot.text,
            after_fingerprint=written.fingerprint,
            undoes=undoes,
        )
    except Exception as exc:  # the file is written: report it, do not pretend nothing happened
        log.warning("config history not recorded (%s)", type(exc).__name__)
        recorded = False
    changed = text != snapshot.text
    if changed:
        ctx.on_config_written(written)
    summary = change.summary
    if change.after_write is not None:
        summary += change.after_write()
    if not recorded:
        summary += "; history not recorded"
    return OpResult(op, changed, summary, shown, change.data, change.needs_human)


def _shown_diff(ctx: OpContext, old: str, new: str, diff: str) -> str:
    """The diff an operation answers with. The web UI (``source == "ui"``) sees the diff of the
    masked texts, masked as ``GET /api/config`` masks the config (a push URL's device key, a
    username, a credential ref, a server URL never reach a browser); the CLI sees the file."""
    if ctx.source != "ui":
        return diff
    from calsync.views import mask_config_text

    return configfile.diff_text(mask_config_text(old), mask_config_text(new))


# ----- rules -------------------------------------------------------------------------------


_RULE_ADD_ARGS = {
    "name",
    "source",
    "target",
    "create_target",
    "window",
    "alarms",
    "content",
    "attendees",
    "conference",
}


def _rule_table(
    op: str, args: dict[str, Any]
) -> tuple[str, dict[str, Any], tuple[str, str], tuple[str, str]]:
    """``rule.add``'s arguments as the new ``[[rules]]`` table: (name, table, source, target)."""
    a = _Args(op, args, _RULE_ADD_ARGS)
    name = a.text("name")
    source, target = _selection(op, "source", a.text("source")), _selection(op, "target", a.text("target"))
    table: dict[str, Any] = {
        "name": name,
        "source": {"account": source[0], "calendar": source[1]},
        "target": {"account": target[0], "calendar": target[1]},
    }
    if a.flag("create_target"):
        table["target"]["create_if_missing"] = True
    window = a.opt_text("window")
    if window is not None and (spec := _window(op, window)) is not None:
        table["window"] = spec
    alarms = a.opt_text("alarms")
    if alarms is not None:
        table["alarms"] = _alarms(alarms)
    fields = {
        key: value for key in ("content", "attendees", "conference") if (value := a.opt_text(key)) is not None
    }
    if fields:
        table["fields"] = fields
    return name, table, source, target


def _add_rule(
    doc: tomlkit.TOMLDocument, table: dict[str, Any], source: tuple[str, str], target: tuple[str, str]
) -> None:
    """Add the rule ``table`` to ``doc``; refused if a rule already syncs ``source`` to ``target``."""
    for existing in _raw(doc).get("rules") or []:
        if not isinstance(existing, Mapping):
            continue
        if _pair(existing.get("source")) == source and _pair(existing.get("target")) == target:
            src, tgt = ":".join(source), ":".join(target)
            raise ConfigError([f'a rule already syncs "{src}" to "{tgt}": "{existing.get("name")}"'])
    configfile.add_rule(doc, table)


def _rule_add(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    op = "rule.add"
    name, table, source, target = _rule_table(op, args)

    def edit(doc: tomlkit.TOMLDocument) -> _Edit:
        _add_rule(doc, table, source, target)
        return _Edit(f'rule "{name}" added: "{":".join(source)}" to "{":".join(target)}"')

    return _config_op(ctx, op, dry_run, edit)


def _pair(raw: Any) -> tuple[str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    return (str(raw.get("account")), str(raw.get("calendar")))


def _rule_set(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    op = "rule.set"
    a = _Args(op, args, {"name", "key", "value"})
    name, key = a.text("name"), a.text("key")
    value = a.raw("value")

    def edit(doc: tomlkit.TOMLDocument) -> _Edit:
        if key not in configfile.RULE_KEYS:
            raise ConfigError([f'unknown setting "{key}"'])
        configfile.set_rule_key(doc, name, key, _rule_value(op, key, value))
        return _Edit(f'rule "{name}": {key} set', same=f'rule "{name}": {key} is already set to that value')

    return _config_op(ctx, op, dry_run, edit)


def _target_calendar(doc: tomlkit.TOMLDocument, name: str) -> str:
    target = configfile.rule_table(doc, name).unwrap().get("target")
    return str(target.get("calendar")) if isinstance(target, Mapping) else "its target"


def _rule_remove(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    op = "rule.remove"
    a = _Args(op, args, {"name", "delete_copies", "after_retire"})
    name = a.text("name")
    delete_copies = a.flag("delete_copies")
    after_retire = a.flag("after_retire")

    def edit(doc: tomlkit.TOMLDocument) -> _Edit:
        calendar = _target_calendar(doc, name)
        if after_retire:
            # The engine finished a retirement (``retire = true``, no copies left): only then
            # does calsync remove a rule by itself. A hand edit that stopped it wins.
            if configfile.rule_table(doc, name).get("retire") is not True:
                raise ConfigError([f'rule "{name}" no longer retires'])
            configfile.remove_rule(doc, name)
            return _Edit(f'rule "{name}" removed after its copies were deleted')
        if delete_copies:
            # The engine deletes the copies (the guard applies), then removes the rule.
            configfile.set_rule_key(doc, name, "retire", True)
            return _Edit(
                f'rule "{name}" retires: its copies in "{calendar}" are deleted, then the rule is removed',
                same=f'rule "{name}" is already retiring',
            )
        configfile.remove_rule(doc, name)
        return _Edit(f'rule "{name}" removed; its copies stay in "{calendar}"')

    return _config_op(ctx, op, dry_run, edit)


def _pause(paused: bool) -> Callable[[OpContext, dict[str, Any], bool], OpResult]:
    op = "rule.pause" if paused else "rule.resume"

    def run(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
        name = _Args(op, args, {"name"}).text("name")

        def edit(doc: tomlkit.TOMLDocument) -> _Edit:
            configfile.set_rule_key(doc, name, "paused", True if paused else None)
            if paused:
                return _Edit(f'rule "{name}" paused', same=f'rule "{name}" is already paused')
            return _Edit(f'rule "{name}" resumed', same=f'rule "{name}" is not paused')

        return _config_op(ctx, op, dry_run, edit)

    return run


# ----- accounts ----------------------------------------------------------------------------


def account_credential(name: str) -> CredentialRef:
    """The credential ``account.add`` gives account ``name``: its keychain entry."""
    return CredentialRef("keychain", f"calsync:{name}")


def _account_add(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    op = "account.add"
    a = _Args(op, args, {"name", "provider", "server_url", "username"})
    name, provider = a.text("name"), a.text("provider")
    table: dict[str, Any] = {"provider": provider}
    for key in ("server_url", "username"):
        if (value := a.opt_text(key)) is not None:
            table[key] = value
    # No operation takes a secret (D10): a person stores it with `calsync auth`.
    ref = account_credential(name)
    table["credential"] = {"backend": ref.backend, "ref": ref.ref}

    def edit(doc: tomlkit.TOMLDocument) -> _Edit:
        configfile.add_account(doc, name, table)
        return _Edit(
            f'account "{name}" added',
            needs_human={"command": f"calsync auth {name}", "reason": AUTH_REASON},
        )

    return _config_op(ctx, op, dry_run, edit)


def _credential(raw: Any) -> CredentialRef | None:
    if isinstance(raw, Mapping) and raw.get("backend") in ("keychain", "env", "file"):
        ref = raw.get("ref")
        if isinstance(ref, str) and ref:
            return CredentialRef(raw["backend"], ref)
    return None


def _account_remove(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    op = "account.remove"
    a = _Args(op, args, {"name", "delete_credential"})
    name = a.text("name")
    delete_credential = a.flag("delete_credential")

    def edit(doc: tomlkit.TOMLDocument) -> _Edit:
        raw = _raw(doc)
        users = [str(r.get("name")) for r in raw.get("rules") or [] if name in _accounts_of(r)]
        if users:
            listed = ", ".join(f'"{u}"' for u in users)
            raise ConfigError([f'account "{name}" is used by rules {listed}; remove them first'])
        accounts = raw.get("accounts") or {}
        account = accounts.get(name)
        configfile.remove_account(doc, name)
        ref = _credential(account.get("credential")) if isinstance(account, Mapping) else None
        if not delete_credential:
            return _Edit(f'account "{name}" removed', after_write=lambda: "; its credential was kept")
        sharers = sorted(
            other
            for other, entry in accounts.items()
            if other != name and isinstance(entry, Mapping) and _credential(entry.get("credential")) == ref
        )
        if ref is not None and sharers:  # deleting it would sign those accounts out too
            shared[:] = sharers
            return _Edit(f'account "{name}" removed', after_write=lambda: _shared_tail(sharers, "was kept"))
        return _Edit(f'account "{name}" removed', after_write=lambda: _delete_credential(ref))

    shared: list[str] = []
    result = _config_op(ctx, op, dry_run, edit)
    if dry_run and result.summary.startswith(DRY_RUN):
        if shared:
            tail = _shared_tail(shared, "would be kept")
        elif delete_credential:
            tail = "; its credential would be deleted"
        else:
            tail = "; its credential would be kept"
        return OpResult(result.op, result.changed, result.summary + tail, result.diff, result.data)
    return result


def _shared_tail(sharers: list[str], kept: str) -> str:
    """Another account uses the same credential: deleting it would sign that one out too."""
    listed = ", ".join(f'"{s}"' for s in sharers)
    noun = "account" if len(sharers) == 1 else "accounts"
    return f"; its credential is also used by {noun} {listed}, so it {kept}"


def _accounts_of(rule: Any) -> set[str]:
    """The accounts a raw ``[[rules]]`` entry names."""
    if not isinstance(rule, Mapping):
        return set()
    sides = (rule.get("source"), rule.get("target"))
    return {str(side.get("account")) for side in sides if isinstance(side, Mapping)}


def _delete_credential(ref: CredentialRef | None) -> str:
    """The end of ``account.remove``'s summary. The config is already written: a failure here is
    reported, not raised."""
    if ref is None:
        return "; it had no credential"
    try:
        deleted = credentials.delete_secret(ref)
    except CalsyncError as exc:
        cause = exc.__cause__
        kind = type(cause).__name__ if cause is not None else type(exc).__name__
        return f'; its keychain entry "{ref.ref}" could not be deleted ({kind}): remove it by hand'
    if not deleted:
        return f"; its credential ({ref.backend}) must be removed by hand"
    return "; its keychain entry was deleted"


# ----- settings and undo -------------------------------------------------------------------


def _config_set(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    op = "config.set"
    a = _Args(op, args, {"key", "value"})
    key = a.text("key")
    value = a.raw("value")

    def edit(doc: tomlkit.TOMLDocument) -> _Edit:
        if key not in configfile.SETTINGS:
            raise ConfigError([f'unknown setting "{key}"'])
        parsed = _setting_value(op, key, value)
        configfile.set_key(doc, key, parsed)
        verb = "cleared" if parsed is None else "set"
        return _Edit(f"{key} {verb}", same=f"{key} is already set to that value")

    return _config_op(ctx, op, dry_run, edit)


def _config_undo(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    op = "config.undo"
    _Args(op, args, set())
    rows = ctx.store.config_changes(CONFIG_CHANGE_RETENTION)
    undone = {r.undoes for r in rows if r.undoes is not None}
    target = next((r for r in rows if r.undoes is None and r.id not in undone and r.before is not None), None)
    if target is None:
        return OpResult(op, False, "nothing to undo", data={"would_change": False} if dry_run else {})
    before = target.before
    assert before is not None
    if before == "":
        raise ConfigError(["this change created the config file; there is nothing earlier to go back to"])

    def edit(doc: tomlkit.TOMLDocument) -> _Edit:
        return _Edit(f"undid change {target.id} ({target.summary})", data={"undoes": target.id})

    def restore(snapshot: configfile.Snapshot) -> str:
        if snapshot.fingerprint != target.after_fingerprint:
            raise ConfigError(
                [
                    f"the file was changed after change {target.id}; undoing it would discard those "
                    "edits — edit the file by hand"
                ]
            )
        return before

    return _config_op(ctx, op, dry_run, edit, new_text=restore, undoes=target.id)


# ----- approving blocked deletes ----------------------------------------------------------


def _approve(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    """Let the deletes the guard blocked for one rule through, exactly that set (by fingerprint).

    Without ``fingerprint`` it is the current block's. Through the daemon, the approval is kept
    for the rule's next cycle (requested at once); without ``wait`` the answer is ``pending``.
    In-process, one cycle for that rule runs here. ``data.status``: ``approved`` (the set was
    deleted), ``stale`` (the planned deletes are another set now: nothing deleted, the new
    fingerprint and count in ``data``), ``not_blocked`` (nothing to approve; nothing stored),
    ``failed`` (the approval cycle failed), ``pending`` and, in a dry run, ``would_approve``."""
    from calsync import views
    from calsync.config import load_config
    from calsync.wiring import state_paths

    op = "approve"
    a = _Args(op, args, {"rule", "fingerprint", "wait"})
    name = a.text("rule")
    wanted = a.opt_text("fingerprint")
    wait = a.flag("wait")
    config = ctx.daemon.config if ctx.daemon is not None else load_config(ctx.config_path)
    rule = next((r for r in config.rules if r.name == name), None)
    if rule is None:
        raise ConfigError([f'unknown rule "{name}"'])
    if rule.paused:
        raise ConfigError([f'rule "{name}" is paused; resume it first'])
    try:
        with views.StateReader(state_paths(ctx.state_dir)[0]) as reader:
            view = views.build_blocked(config, reader, name)
    except views.StateUnavailable:
        view = views.BlockedView(())
    if not view.rules:
        return not_blocked(name)
    blocked = view.rules[0]
    fingerprint = blocked.fingerprint
    if fingerprint is None:
        raise unrecorded_block(name)
    if wanted is not None and wanted != fingerprint:
        return _stale(op, name, fingerprint, blocked.count, wanted)
    data = {**_status(name, "pending"), "fingerprint": fingerprint, "count": blocked.count}
    if dry_run:
        summary = f'{DRY_RUN}rule "{name}": {_copies(blocked.count)} would be approved for deletion'
        return OpResult(op, False, summary, data={**data, "status": "would_approve", "would_change": True})
    if ctx.daemon is not None:
        try:
            waiter = ctx.daemon.approve(name, fingerprint, blocked.count, ctx.source, wait=wait)
        except ValueError as exc:  # unknown or paused since: the daemon's own check
            raise ConfigError([str(exc)]) from None
        if waiter is not None:

            async def finish() -> OpResult:
                return _outcome(op, name, fingerprint, blocked.count, (await waiter))

            ctx.followup = finish
        return OpResult(
            op,
            True,
            f'rule "{name}": {_copies(blocked.count)} approved for deletion; the next cycle deletes them',
            data=data,
        )
    log.info("rule %s: %d deletes approved (%s)", name, blocked.count, ctx.source)
    from calsync import notify

    approval = notify.approved(name, blocked.count, ctx.source, config, ctx.clock())
    result, removed = asyncio.run(_cycle_here(ctx, config, [name], {name: fingerprint}, alerts=[approval]))
    outcome = _outcome(op, name, fingerprint, blocked.count, result)
    if removed:
        return dataclasses.replace(outcome, summary=outcome.summary + "; " + "; ".join(removed))
    return outcome


def not_blocked(rule: str) -> OpResult:
    """``approve``'s answer for a rule with nothing blocked (the CLI's prompt answers it too)."""
    return OpResult(
        "approve",
        False,
        f'rule "{rule}" is not blocked: nothing to approve',
        data=_status(rule, "not_blocked"),
    )


def unrecorded_block(rule: str) -> ConfigError:
    """A blocked rule whose blocked set (so its fingerprint) is no longer recorded."""
    return ConfigError(
        [
            f'rule "{rule}" is blocked, but its blocked set is no longer recorded; restart the daemon '
            f"(or run `calsync run --rule {rule}` with the daemon stopped) to record it, then approve"
        ]
    )


def _status(rule: str, status: str) -> dict[str, Any]:
    return {"rule": rule, "status": status}


def _copies(count: int) -> str:
    return f"{count} {'copy' if count == 1 else 'copies'}"


def _stale(op: str, rule: str, fingerprint: str | None, count: int, approved: str) -> OpResult:
    summary = (
        f'rule "{rule}": the blocked set changed ({_copies(count)} would be deleted now); nothing was '
        f"deleted — check `calsync blocked --rule {rule}`, then approve again if intended"
    )
    data = {
        **_status(rule, "stale"),
        "fingerprint": fingerprint,
        "count": count,
        "approved_fingerprint": approved,
    }
    return OpResult(op, False, summary, data=data)


def _outcome(op: str, rule: str, fingerprint: str, count: int, cycle: "CycleResult | None") -> OpResult:
    """What became of an approval, from the cycle that consumed it (None: the cycle failed)."""
    r: RuleResult | None = cycle.rules.get(rule) if cycle is not None else None
    if r is None:
        summary = f'rule "{rule}": the sync cycle failed; nothing was deleted — see the daemon log, then approve again'
        return OpResult(
            op, False, summary, data={**_status(rule, "failed"), "fingerprint": fingerprint, "count": count}
        )
    if r.status == "blocked":
        planned = len(r.plan.delete) if r.plan is not None else 0
        return _stale(op, rule, r.blocked_fingerprint, planned, fingerprint)
    if r.approved and r.status == "ok":
        data = {**_status(rule, "approved"), "fingerprint": fingerprint, "count": count, "deleted": r.deleted}
        return OpResult(op, True, f'rule "{rule}": {_copies(r.deleted)} deleted', data=data)
    if r.status != "ok":
        summary = (
            f'rule "{rule}": the approval cycle ended {r.status} ({r.error_kind or "other"}); '
            f"{_copies(r.deleted)} deleted — check `calsync status`, then `calsync blocked --rule {rule}`"
        )
        data = {
            **_status(rule, "failed"),
            "fingerprint": fingerprint,
            "count": count,
            "deleted": r.deleted,
            "run_status": r.status,
            "error_kind": r.error_kind,
        }
        return OpResult(op, r.deleted > 0, summary, data=data)
    # The guard no longer held anything back (the set shrank, or the guard was raised): the
    # rule synced as usual and the approval was not needed.
    summary = f'rule "{rule}" was no longer blocked; it synced as usual ({_copies(r.deleted)} deleted)'
    return OpResult(op, r.deleted > 0, summary, data={**_status(rule, "not_blocked"), "deleted": r.deleted})


async def _cycle_here(
    ctx: OpContext,
    config: Config,
    rules: list[str],
    approvals: Mapping[str, str] | None = None,
    *,
    alerts: Sequence["Alert"] = (),
) -> tuple["CycleResult", list[str]]:
    """One cycle for ``rules`` (paused ones are skipped) in this process (the caller holds the
    run lock), with ``approvals``; then, as ``calsync run`` does, the removal of the rules
    that retired, and the pushes: ``alerts`` and what the cycle raised (``notify``), sent
    before returning."""
    from calsync import notify, wiring
    from calsync.cycle import remove_retired, retired, run_cycle

    adapters = await wiring.build_adapters(config, wiring.accounts_in_use(config, rules))
    try:
        engine = wiring.build_engine(config, adapters, ctx.store, clock=ctx.clock)
        result = await run_cycle(
            engine,
            ctx.store,
            config,
            adapters,
            rules,
            dry_run=False,
            allow_deletes=False,
            approvals=approvals,
        )
    finally:
        await adapters.aclose()
    raised = notify.evaluate(result.rules, store=ctx.store, config=config, now=ctx.clock())
    removal = dataclasses.replace(ctx, source="daemon")
    removed = [
        done.summary for n in retired(config, result, ctx.store) if (done := remove_retired(removal, n))
    ]
    await notify.Notifier(config.notify).send([*alerts, *raised])
    return result, removed


# ----- a cycle now ---------------------------------------------------------------------------

TRIGGER_TIMEOUT = 120.0  # seconds ``trigger`` waits by default
TRIGGER_TIMEOUT_MAX = 600.0
TRIGGER_NO_DRY_RUN = "trigger has no dry run; use calsync run --dry-run"
RESULT_KEYS = ("status", "created", "updated", "deleted", "failed", "error_kind")


def _timeout(op: str, raw: Any) -> float:
    if raw is None:
        return TRIGGER_TIMEOUT
    if isinstance(raw, str):
        try:
            raw = float(raw)
        except ValueError:
            raise _bad(op, "timeout", "a number of seconds", raw) from None
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not 0 < raw <= TRIGGER_TIMEOUT_MAX:
        raise ConfigError(
            [f'{op}: "timeout" must be a number of seconds above 0 and at most {TRIGGER_TIMEOUT_MAX:g}']
        )
    return float(raw)


def _ran(cycle: "CycleResult") -> dict[str, Any]:
    """A finished cycle as ``trigger`` reports it: each rule it ran, and its request count."""
    rules = {name: {key: getattr(r, key) for key in RESULT_KEYS} for name, r in cycle.rules.items()}
    return {"ran": True, "rules": rules, "requests": cycle.requests}


def _ran_summary(cycle: "CycleResult", lead: str = "the cycle ran") -> str:
    """``the cycle ran: family ok, work failed (auth)`` (rule names and statuses only)."""
    if not cycle.rules:
        return f"{lead} no rule"
    parts = [
        f"{name} {r.status}" + (f" ({r.error_kind})" if r.error_kind and r.status != "ok" else "")
        for name, r in cycle.rules.items()
    ]
    return f"{lead}: " + ", ".join(parts)


def _wrote(cycle: "CycleResult") -> bool:
    return any(r.created or r.updated or r.deleted for r in cycle.rules.values())


async def _trigger(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    """A sync cycle now: ``rule`` (None: every rule that is not paused).

    Through the daemon, the cycle is requested (never under the daemon's lock); with ``wait``
    the answer waits, at most ``timeout`` seconds (default 120, at most 600), for the first
    cycle that starts after the request and runs the rule(s): ``data`` is ``{"ran": true,
    "rules": {name: {status, created, updated, deleted, failed, error_kind}}, "requests": N}``
    (the rules that cycle ran), or ``{"ran": false, "reason": "timeout" | "cycle_failed"}``.
    Without ``wait`` it is ``{"accepted": true, "rule": ...}``. In-process (no daemon) the cycle
    runs here, holding the run lock, whatever ``wait`` says (``timeout`` does not cut a cycle
    short); a retiring rule that finished is removed as ``calsync run`` does. ``RulePaused``
    for a paused rule, ``ConfigError`` for an unknown one or bad arguments."""
    from calsync.config import load_config

    op = "trigger"
    if dry_run:  # it writes no config; what a cycle would do is `calsync run --dry-run`
        raise ConfigError([TRIGGER_NO_DRY_RUN])
    a = _Args(op, args, {"rule", "wait", "timeout"})
    name = a.opt_text("rule")
    wait = a.flag("wait")
    timeout = _timeout(op, args.get("timeout"))
    config = ctx.daemon.config if ctx.daemon is not None else load_config(ctx.config_path)
    by_name = {r.name: r for r in config.rules}
    if name is not None and name not in by_name:
        raise ConfigError([f'unknown rule "{name}"'])
    if name is not None and by_name[name].paused:
        raise RulePaused(name)
    rules = [name] if name is not None else [r.name for r in config.rules if not r.paused]
    if not rules:
        return OpResult(
            op, False, "every rule is paused: nothing to run", data={"ran": False, "reason": "paused"}
        )
    if ctx.daemon is None:
        result, removed = await _cycle_here(ctx, config, rules)
        summary = _ran_summary(result, "no daemon runs, so the cycle ran here")
        return OpResult(op, _wrote(result), "; ".join([summary, *removed]), data=_ran(result))
    try:
        if not wait:
            if not ctx.daemon.request_cycle(name):
                raise OpsUnavailable("The daemon runs with --no-sync: it runs no sync cycles.")
            return OpResult(op, False, "cycle requested", data={"accepted": True, "rule": name})
        waiter = ctx.daemon.request_cycle(name, wait=True)
    except RulePaused:
        raise
    except ValueError as exc:  # unknown since: the daemon's own check
        raise ConfigError([str(exc)]) from None

    async def finish() -> OpResult:
        try:
            # ``asyncio.wait`` leaves the future alone on a timeout; cancelling it below only
            # stops this waiting (a cycle in progress is never touched).
            await asyncio.wait({waiter}, timeout=timeout)
        finally:
            if not waiter.done():
                waiter.cancel()
        if waiter.cancelled():
            return OpResult(
                op,
                False,
                f"no cycle finished within {timeout:g} s; the requested cycle still runs",
                data={"ran": False, "reason": "timeout"},
            )
        cycle = waiter.result()  # a refusal (paused, removed, stopping) is raised here
        if cycle is None:
            summary = "the sync cycle failed; see the daemon log"
            return OpResult(op, False, summary, data={"ran": False, "reason": "cycle_failed"})
        return OpResult(op, _wrote(cycle), _ran_summary(cycle), data=_ran(cycle))

    ctx.followup = finish
    return OpResult(op, False, "cycle requested", data={"accepted": True, "rule": name})


# ----- notifications --------------------------------------------------------------------------

NOTIFY_TEST_NO_DRY_RUN = "notify.test has no dry run; it only sends a test push"


async def _notify_test(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    """One test push to every configured channel. ``data``: ``{"channels": {"bark": …,
    "webhook": …}}``, each ``ok``, ``failed (<Type>)`` or ``not configured``. It changes
    nothing; each push waits at most ``notify.SEND_TIMEOUT`` seconds."""
    from calsync import notify

    op = "notify.test"
    if dry_run:
        raise ConfigError([NOTIFY_TEST_NO_DRY_RUN])
    _Args(op, args, set())
    config = _config_now(ctx)
    report = await notify.Notifier(config.notify).test(config.notify.language)
    summary = "test push: " + ", ".join(f"{channel} {outcome}" for channel, outcome in report.items())
    return OpResult(op, False, summary, data={"channels": report})


# ----- the calendar cache -----------------------------------------------------------------


def _config_now(ctx: OpContext) -> Config:
    """The config to act on: the daemon's (what it runs), or the file's."""
    from calsync.config import load_config

    return ctx.daemon.config if ctx.daemon is not None else load_config(ctx.config_path)


@contextlib.asynccontextmanager
async def _adapters(ctx: OpContext, config: Config, names: set[str]) -> AsyncIterator["Adapters"]:
    """Adapters for ``names``: borrowed from the daemon, or built here and closed after."""
    if ctx.daemon is not None:
        async with ctx.daemon.borrow_adapters(config, names) as adapters:
            yield adapters
        return
    from calsync import wiring

    adapters = await wiring.build_adapters(config, names)
    try:
        yield adapters
    finally:
        await adapters.aclose()


# Operations that talk to the accounts hold the daemon's ``_cycle_lock`` (no cycle runs
# meanwhile), so each network wait is bounded: a server that never answers must not stop the
# sync. Read when used (tests patch them). A timeout is a ``TimeoutError``, reported as any
# failure of that account.
PREVIEW_TIMEOUT = 120.0  # seconds: the preview's live pull of a source calendar
REFRESH_TIMEOUT = 120.0  # seconds: one account's calendar listing (ops and the daemon's refresh)


@dataclass(frozen=True)
class Refreshed:
    """What ``refresh_calendars`` did: the calendars listed per account (stored unless
    ``save=False``) and their count, and the accounts that failed."""

    counts: dict[str, int] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    rows: dict[str, list[AccountCalendar]] = field(default_factory=dict)


async def refresh_calendars(
    store: Store, adapters: "Adapters", names: Sequence[str], at: datetime, *, save: bool = True
) -> Refreshed:
    """Fetch each account's calendar list into the cache (``account_calendar``), stamped ``at``;
    with ``save=False`` only list it (a dry run). Each listing waits at most ``REFRESH_TIMEOUT``.
    The accounts are listed concurrently. A failing account keeps its old rows; its failure is logged by type (the text can carry a
    URL: ``debug.details``). Google rows carry calendarList's ``accessRole``; CalDAV rows no role
    (discovery reads no privilege set, and no request is added just for it)."""
    done = Refreshed()

    async def listing(name: str) -> list[AccountCalendar]:
        adapters.adapter(name)  # its setup error, if any
        if name in adapters.sources:
            found = await asyncio.wait_for(adapters.sources[name].list_calendars(), REFRESH_TIMEOUT)
            return [AccountCalendar(name, c.id, c.name, c.role, at) for c in found]
        listed = await asyncio.wait_for(adapters.targets[name].list_calendars(), REFRESH_TIMEOUT)
        return [AccountCalendar(name, c.href, c.name, None, at) for c in listed]

    # Every account at once (each has its own client): accounts that do not answer cost one
    # timeout together. Written one account at a time, in ``names`` order.
    outcomes = await asyncio.gather(*(listing(n) for n in names), return_exceptions=True)
    for name, outcome in zip(names, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):  # cancelled, KeyboardInterrupt: not an account failure
                raise outcome
            log.warning(
                "the calendar list of account %s could not be refreshed (%s)", name, type(outcome).__name__
            )
            debug.details(log, f"the calendar list of account {name} could not be refreshed", outcome)
            done.errors[name] = outcome
            continue
        if save:
            store.save_account_calendars(name, outcome, at)
        done.counts[name] = len(outcome)
        done.rows[name] = outcome
    return done


def _kind(exc: BaseException) -> str:
    from calsync.engine import error_kind_for

    return error_kind_for(exc)


async def _calendars_refresh(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    """Fetch the calendar lists now: of ``account``, or of every configured account. A failing
    account is reported in ``data`` (a plain error, its kind), never raised: through the daemon
    an escaping error would only be an HTTP 500. An auth failure asks for ``calsync auth``."""
    op = "calendars.refresh"
    account = _Args(op, args, {"account"}).opt_text("account")
    config = _config_now(ctx)
    if account is not None and account not in config.accounts:
        raise ConfigError([f'unknown account "{account}"'])
    names = [account] if account is not None else sorted(config.accounts)
    if dry_run:
        listed = ", ".join(f'"{n}"' for n in names) or "no account"
        summary = f"{DRY_RUN}the calendar lists of {listed} would be fetched"
        return OpResult(op, False, summary, data={"would_refresh": names, "would_change": bool(names)})
    at = ctx.clock()
    async with _adapters(ctx, config, set(names)) as adapters:
        done = await refresh_calendars(ctx.store, adapters, names, at)
        setup = set(adapters.errors)
    outcome: dict[str, dict[str, Any]] = {}
    for name in names:
        exc = done.errors.get(name)
        outcome[name] = {
            "refreshed": exc is None,
            "calendars": done.counts.get(name),
            "fetched_at": at.isoformat() if exc is None else None,
            "error": None
            if exc is None
            else plain_error(exc, f'account "{name}"', account=name, setup=name in setup),
            "error_kind": None if exc is None else _kind(exc),
        }
    human = next((n for n in names if isinstance(done.errors.get(n), AuthError)), None)
    needs_human = {"command": f"calsync auth {human}", "reason": AUTH_REASON} if human else None
    return OpResult(
        op,
        bool(done.counts),
        _refresh_summary(names, done),
        data={"accounts": outcome},
        needs_human=needs_human,
    )


def _refresh_summary(names: Sequence[str], done: Refreshed) -> str:
    failed = [n for n in names if n in done.errors]
    if len(names) == 1:
        name = names[0]
        if failed:
            return f'the calendar list of account "{name}" could not be fetched ({type(done.errors[name]).__name__})'
        count = done.counts[name]
        return f'the calendar list of account "{name}" was fetched: {count} {"calendar" if count == 1 else "calendars"}'
    summary = f"the calendar lists of {len(names) - len(failed)} of {len(names)} accounts were fetched"
    if failed:
        summary += "; failed: " + ", ".join(f'"{n}" ({type(done.errors[n]).__name__})' for n in failed)
    return summary


# ----- the rule preview ---------------------------------------------------------------------


class _Unreachable(Exception):
    """An account's adapter failed during a preview: reported in the result, never raised."""

    def __init__(self, exc: Exception, account: str, setup: bool) -> None:
        super().__init__(type(exc).__name__)
        self.exc, self.account, self.setup = exc, account, setup


def _cached_calendar(state_dir: Path, account: str, ref: str) -> tuple[str, str] | None:
    """The cached calendar of ``account`` that ``ref`` names (``_match_calendar``); None if the
    cache has none."""
    from calsync import views
    from calsync.wiring import state_paths

    try:
        with views.StateReader(state_paths(state_dir)[0]) as reader:
            rows = [(c.id, c.name) for c in reader.account_calendars(account)]
    except views.StateUnavailable:
        return None
    return _match_calendar(rows, account, ref)


def _match_calendar(rows: Sequence[tuple[str, str]], account: str, ref: str) -> tuple[str, str] | None:
    """The calendar among ``rows`` (id, name) that ``ref`` names: by href first (``same_href``, as
    ``CalDavTarget.ensure_calendar`` matches), then by name; None if none does. Several by name:
    ``ConfigError`` (the sync would refuse too)."""
    from calsync.naming import same_href

    matches = [r for r in rows if same_href(r[0], ref)] or [r for r in rows if r[1] == ref]
    if len(matches) > 1:
        raise ConfigError(
            [f'calendar "{ref}" matches {len(matches)} calendars of account "{account}"; use its href']
        )
    return matches[0] if matches else None


async def _preview_source(
    ctx: OpContext, config: Config, account: str, ref: str
) -> tuple[list[dict[str, Any]], Any, bool]:
    """The source calendar's raw events and metadata, and whether they come from the mirror. A
    calendar not mirrored yet is pulled once, live, with empty state; nothing of it is stored."""
    from calsync import views
    from calsync.errors import NotFoundError
    from calsync.sources.google import meta_from_state, resolve_calendar_ref
    from calsync.wiring import state_paths

    try:
        with views.StateReader(state_paths(ctx.state_dir)[0]) as reader:
            state = reader.source_state(account)
            meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
            tokens = state.get("events_tokens") if isinstance(state.get("events_tokens"), dict) else {}
            try:
                cid: str | None = resolve_calendar_ref(ref, meta or {})
            except ConfigError:
                cid = None
            if cid is not None and cid in (tokens or {}):
                return reader.source_events(account, cid), meta_from_state((meta or {}).get(cid)), True
    except views.StateUnavailable:
        pass
    async with _adapters(ctx, config, {account}) as adapters:
        setup = account in adapters.errors
        try:
            adapters.adapter(account)
            pull = await asyncio.wait_for(adapters.sources[account].pull([ref], {}), PREVIEW_TIMEOUT)
        except Exception as exc:
            raise _Unreachable(exc, account, setup) from exc
    failure = pull.errors.get(ref)
    if isinstance(failure, ConfigError | NotFoundError):
        raise ConfigError(
            [f'calendar "{ref}" was not found in account "{account}"; see `calsync calendars {account}`']
        )
    if failure is not None:
        raise _Unreachable(failure, account, False)
    found = pull.calendars[ref]
    return found.upserts, found.meta, False


async def _preview_target(
    ctx: OpContext, config: Config, account: str, ref: str, dry_run: bool
) -> tuple[str, str] | None:
    """The target calendar ``ref`` names, from the calendar cache, which is refreshed once if
    it does not have it (``dry_run``: listed only, the cache is not written); None: not there."""
    found = _cached_calendar(ctx.state_dir, account, ref)
    if found is not None:
        return found
    async with _adapters(ctx, config, {account}) as adapters:
        done = await refresh_calendars(ctx.store, adapters, [account], ctx.clock(), save=not dry_run)
        setup = account in adapters.errors
    if account in done.errors:
        raise _Unreachable(done.errors[account], account, setup)
    return _match_calendar([(c.id, c.name) for c in done.rows[account]], account, ref)


def _preview_summary(data: dict[str, Any], target: str) -> str:
    what = f"{data['series']} series"
    if data["instances_in_window"] is not None:
        what += f" ({data['instances_in_window']} in the window)"
    if data["target_exists"]:
        return f'would create {what} in "{target}" (calendar exists)'
    if data["target_would_be_created"]:
        return f'would create {what} in "{target}" (calendar would be created)'
    return f'would create {what} in "{target}" (calendar not found; add --create-target to create it)'


async def _rule_preview(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    """What ``rule.add`` with these arguments would sync, writing nothing (no config, no history;
    at most the calendar cache is refreshed, and not even that with ``dry_run``). The live pull
    waits at most ``PREVIEW_TIMEOUT``, the target's listing ``REFRESH_TIMEOUT``. Validated like ``rule.add`` (``ConfigError``).
    ``data``: ``series`` (the series the first cycle would create; in the window when the rule
    has one), ``instances_in_window`` (None: the rule syncs everything), ``target_exists``,
    ``target_would_be_created``, ``source_mirrored``. An account that cannot be reached is
    reported (``error``, ``error_kind``, ``error_account``, the counts None), never raised."""
    from calsync.model import Window, apply_alarm_policy, apply_field_policy, expand_instances, series_span
    from calsync.sources.google_map import build_series

    op = "rule.preview"
    name, table, source, target = _rule_table(op, args)
    doc = configfile.load_doc(configfile.read_snapshot(ctx.config_path).text)
    _add_rule(doc, table, source, target)
    config = configfile.validate_text(tomlkit.dumps(doc), ctx.config_path.parent)
    rule = next(r for r in config.rules if r.name == name)
    data: dict[str, Any] = dict.fromkeys(
        ("series", "instances_in_window", "target_exists", "target_would_be_created", "source_mirrored")
    )
    try:
        raw, meta, data["source_mirrored"] = await _preview_source(
            ctx, config, rule.source.account, rule.source.calendar
        )
        found = await _preview_target(ctx, config, rule.target.account, rule.target.calendar, dry_run)
    except _Unreachable as failure:
        log.warning("rule preview: account %s unreachable (%s)", failure.account, type(failure.exc).__name__)
        debug.details(log, f"rule preview: account {failure.account} unreachable", failure.exc)
        error = plain_error(
            failure.exc, f'account "{failure.account}"', account=failure.account, setup=failure.setup
        )
        data.update(error=error, error_kind=_kind(failure.exc), error_account=failure.account)
        human = isinstance(failure.exc, AuthError)
        return OpResult(
            op,
            False,
            f"could not preview: {error}",
            data=data,
            needs_human={"command": f"calsync auth {failure.account}", "reason": AUTH_REASON}
            if human
            else None,
        )
    shaped = [
        apply_field_policy(apply_alarm_policy(s, rule.alarms), rule.fields) for s in build_series(raw, meta)
    ]
    if rule.window is not None:
        window = Window.around(ctx.clock(), rule.window.past, rule.window.future)
        shaped = [s for s in shaped if window.intersects(series_span(s))]
        data["instances_in_window"] = sum(len(expand_instances(s, window.start, window.end)) for s in shaped)
    data["series"] = len(shaped)
    data["target_exists"] = found is not None
    data["target_would_be_created"] = found is None and rule.target.create_if_missing
    return OpResult(op, False, _preview_summary(data, found[1] if found else rule.target.calendar), data=data)


AsyncOp = Callable[[OpContext, dict[str, Any], bool], Coroutine[Any, Any, OpResult]]

# Operations that talk to the accounts. The daemon awaits them under its lock (``run_op_async``);
# in a process of their own (``run_op``) they get an event loop of their own.
ASYNC_OPS: dict[str, AsyncOp] = {
    "calendars.refresh": _calendars_refresh,
    "rule.preview": _rule_preview,
    "trigger": _trigger,
    "notify.test": _notify_test,
}
# Operations the daemon runs without its lock: they touch neither the store nor the config, only
# ask it for a cycle (and may wait for it) or send a test push.
UNLOCKED_OPS = frozenset({"trigger", "notify.test"})


def _own_loop(op: AsyncOp) -> Callable[[OpContext, dict[str, Any], bool], OpResult]:
    def run(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
        return asyncio.run(op(ctx, args, dry_run))

    return run


OPS: dict[str, Callable[[OpContext, dict[str, Any], bool], OpResult]] = {
    "config.set": _config_set,
    "config.undo": _config_undo,
    "rule.add": _rule_add,
    "rule.set": _rule_set,
    "rule.remove": _rule_remove,
    "rule.pause": _pause(True),
    "rule.resume": _pause(False),
    "account.add": _account_add,
    "account.remove": _account_remove,
    "approve": _approve,
    **{name: _own_loop(op) for name, op in ASYNC_OPS.items()},
}


def internal_arg(args: Mapping[str, Any]) -> str | None:
    """The first internal argument in ``args`` (a transport refuses it), or None."""
    return next((key for key in sorted(INTERNAL_ARGS) if key in args), None)


def run_op(ctx: OpContext, name: str, args: dict[str, Any], dry_run: bool) -> OpResult:
    """Run the operation ``name``. ``ConfigError`` for an unknown name, bad arguments, an
    invalid result or a conflict (``configfile.ConfigConflict``); nothing is written then."""
    op = OPS.get(name)
    if op is None:
        raise ConfigError([f'unknown operation "{name}"'])
    return op(ctx, args, dry_run)


async def run_op_async(ctx: OpContext, name: str, args: dict[str, Any], dry_run: bool) -> OpResult:
    """``run_op`` on a running event loop (the daemon's): an operation that talks to the accounts
    is awaited there, the others run as they are."""
    op = ASYNC_OPS.get(name)
    if op is not None:
        return await op(ctx, args, dry_run)
    return run_op(ctx, name, args, dry_run)
