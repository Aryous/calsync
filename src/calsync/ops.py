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

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import tomlkit

from calsync import configfile, credentials
from calsync.config import CredentialRef
from calsync.errors import CalsyncError, ConfigError
from calsync.store import CONFIG_CHANGE_RETENTION, Store

log = logging.getLogger(__name__)

Source = Literal["cli", "ui", "daemon"]
DRY_RUN = "dry run, nothing written: "
AUTH_REASON = "a person signs in (Google) or types the app-specific password (iCloud)"


class DaemonHooks(Protocol):
    """What operations may ask of the daemon they run in (cycles, approvals, adapters).
    Empty until the operations that need it arrive."""


class OpsUnavailable(CalsyncError):
    """This daemon runs no operations (``--no-sync``: no lock, no store)."""


@dataclass
class OpContext:
    config_path: Path
    state_dir: Path
    store: Store  # writable; the caller holds the run lock
    source: Source
    clock: Callable[[], datetime]
    on_config_written: Callable[[configfile.Snapshot], None] = lambda s: None  # the daemon reloads here
    daemon: DaemonHooks | None = None


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
    if dry_run:
        data = {**change.data, "would_change": diff != ""}
        return OpResult(op, False, DRY_RUN + change.summary, diff, data, change.needs_human)
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
    return OpResult(op, changed, summary, diff, change.data, change.needs_human)


# ----- rules -------------------------------------------------------------------------------


def _rule_add(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    op = "rule.add"
    a = _Args(
        op,
        args,
        {
            "name",
            "source",
            "target",
            "create_target",
            "window",
            "alarms",
            "content",
            "attendees",
            "conference",
        },
    )
    name = a.text("name")
    source_text, target_text = a.text("source"), a.text("target")
    source, target = _selection(op, "source", source_text), _selection(op, "target", target_text)
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

    def edit(doc: tomlkit.TOMLDocument) -> _Edit:
        for existing in _raw(doc).get("rules") or []:
            if not isinstance(existing, Mapping):
                continue
            if _pair(existing.get("source")) == source and _pair(existing.get("target")) == target:
                src, tgt = ":".join(source), ":".join(target)
                raise ConfigError([f'a rule already syncs "{src}" to "{tgt}": "{existing.get("name")}"'])
        configfile.add_rule(doc, table)
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
    a = _Args(op, args, {"name", "delete_copies"})
    name = a.text("name")
    delete_copies = a.flag("delete_copies")

    def edit(doc: tomlkit.TOMLDocument) -> _Edit:
        calendar = _target_calendar(doc, name)
        if ctx.source == "daemon":
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


def _account_add(ctx: OpContext, args: dict[str, Any], dry_run: bool) -> OpResult:
    op = "account.add"
    a = _Args(op, args, {"name", "provider", "server_url", "username"})
    name, provider = a.text("name"), a.text("provider")
    table: dict[str, Any] = {"provider": provider}
    for key in ("server_url", "username"):
        if (value := a.opt_text(key)) is not None:
            table[key] = value
    # No operation takes a secret (D10): a person stores it with `calsync auth`.
    table["credential"] = {"backend": "keychain", "ref": f"calsync:{name}"}

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
}


def run_op(ctx: OpContext, name: str, args: dict[str, Any], dry_run: bool) -> OpResult:
    """Run the operation ``name``. ``ConfigError`` for an unknown name, bad arguments, an
    invalid result or a conflict (``configfile.ConfigConflict``); nothing is written then."""
    op = OPS.get(name)
    if op is None:
        raise ConfigError([f'unknown operation "{name}"'])
    return op(ctx, args, dry_run)
