"""Edit and write the TOML configuration safely (spec §4.4).

Edits go through tomlkit, so comments and formatting survive. A write validates the
whole new text with the same rules as ``load_config``, refuses if the file changed
since it was read, keeps the previous bytes in ``<name>.bak`` and replaces the file
atomically. Recording who changed what is the caller's job.

The editing helpers check names (rule, account, setting), not values: values are
validated once, by ``validate_text``, when the new text is written.
"""

import contextlib
import difflib
import hashlib
import os
import stat
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.container import OutOfOrderTableProxy
from tomlkit.exceptions import ConvertError, ParseError
from tomlkit.items import AoT, InlineTable, Item, Table

from calsync.config import Config, parse_config
from calsync.errors import ConfigError

RULE_KEYS = frozenset(
    {
        "window",
        "alarms",
        "paused",
        "retire",
        "fields.content",
        "fields.attendees",
        "fields.conference",
        "delete_guard.max_count",
        "delete_guard.max_ratio",
    }
)
SETTINGS = frozenset(
    {
        "google.client_secret_file",
        "notify.bark_url",
        "notify.webhook_url",
        "notify.public_url",
        "notify.failure_threshold",
        "notify.language",
        "runtime.interval",
        "runtime.jitter",
        "web.allowed_hosts",
    }
)
_CONFLICT = "the file was changed outside calsync; reload and try again"


@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    text: str
    fingerprint: str  # sha256 hex of the file bytes; "" when the file does not exist


class ConfigConflict(ConfigError):
    """The file changed between reading it and writing it."""

    def __init__(self) -> None:
        super().__init__([_CONFLICT])


def _fingerprint(data: bytes | None) -> str:
    return "" if data is None else hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def read_snapshot(path: Path) -> Snapshot:
    data = _read_bytes(path)
    if data is None:
        return Snapshot(path, "", "")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError([f"{path}: {exc}"]) from None
    return Snapshot(path, text, _fingerprint(data))


def validate_text(text: str, base_dir: Path) -> Config:
    """Parse and validate ``text`` exactly as ``load_config`` would a file in ``base_dir``."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"invalid TOML: {exc}"]) from None
    return parse_config(data, base_dir=base_dir)


def _write_backup(path: Path, data: bytes) -> None:
    fd = os.open(path.with_name(path.name + ".bak"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fh.fileno(), 0o600)  # an existing .bak keeps its mode otherwise
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_config(snapshot: Snapshot, new_text: str) -> Snapshot:
    """Validate, check the fingerprint, back up, then atomically replace the file.
    Returns the new snapshot; identical text writes nothing and returns ``snapshot``.

    A symlinked path is written through to its target (the backup and the temporary file live
    next to the target), so a config kept in a dotfiles repository stays linked. Relative paths
    in the text still resolve against the link's directory, as ``load_config`` reads them. A
    missing file is created 0600, its directory 0700."""
    validate_text(new_text, snapshot.path.parent)
    if new_text == snapshot.text:
        return snapshot
    path = Path(os.path.realpath(snapshot.path))  # resolved once: every step below uses it
    current = _read_bytes(path)
    if _fingerprint(current) != snapshot.fingerprint:
        raise ConfigConflict()
    mode = 0o600
    if current is not None:
        mode = stat.S_IMODE(path.stat().st_mode)
        _write_backup(path, current)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = new_text.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    _fsync_dir(path.parent)
    return Snapshot(snapshot.path, new_text, _fingerprint(data))


_NO_NEWLINE = "\\ No newline at end of file\n"


def diff_text(old: str, new: str, name: str = "config.toml") -> str:
    """A unified diff, like ``diff -u``: a missing newline at the end of a file is shown, so a
    real change never diffs as empty."""
    out: list[str] = []
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=name, tofile=name, lineterm=""
    )
    for line in lines:
        if line.endswith("\n"):
            out.append(line)
        elif line.startswith(("---", "+++", "@@")):
            out.append(line + "\n")
        else:  # the last line of a file that does not end with a newline
            out.append(line + "\n" + _NO_NEWLINE)
    return "".join(out)


# ----- tomlkit editing helpers ---------------------------------------------------


def load_doc(text: str) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(text)
    except ParseError as exc:
        raise ConfigError([f"invalid TOML: {exc}"]) from None


def _value(value: Any, where: str) -> Item:
    """A TOML item for ``value``; tables nested in a rule or account are written inline."""
    try:
        if isinstance(value, Mapping):
            table = tomlkit.inline_table()
            for key, sub in value.items():
                table[key] = _value(sub, f"{where}.{key}")
            return table
        return tomlkit.item(value)
    except ConvertError:
        raise ConfigError([f"{where}: cannot be written as a TOML value"]) from None


def _new_table(values: Mapping[str, Any], where: str) -> Table:
    table = tomlkit.table()
    for key, value in values.items():
        table[key] = _value(value, f"{where}.{key}")
    return table


def _rules(doc: tomlkit.TOMLDocument) -> AoT | None:
    rules = doc.get("rules")
    if rules is None or isinstance(rules, AoT):
        return rules
    raise ConfigError(["rules: must be an array of tables (use [[rules]])"])


def _rule_index(doc: tomlkit.TOMLDocument, name: str) -> int:
    rules = _rules(doc)
    for index, table in enumerate(rules.body if rules is not None else []):
        if table.get("name") == name:
            return index
    raise ConfigError([f'unknown rule "{name}"'])


def rule_table(doc: tomlkit.TOMLDocument, name: str) -> Table:
    """The ``[[rules]]`` entry named ``name``."""
    index = _rule_index(doc, name)
    rules = _rules(doc)
    assert rules is not None  # _rule_index found the rule
    return rules.body[index]


def set_rule_key(doc: tomlkit.TOMLDocument, name: str, key: str, value: Any) -> None:
    """Set one setting of a rule (``RULE_KEYS``); ``None`` removes it (``window`` → full sync)."""
    if key not in RULE_KEYS:
        raise ConfigError([f'unknown setting "{key}"'])
    table = rule_table(doc, name)
    where = f"rules[{name}].{key}"
    head, _, leaf = key.partition(".")
    if leaf:
        sub = table.get(head)
        if value is None:
            if isinstance(sub, Table | InlineTable) and leaf in sub:
                del sub[leaf]
                if not sub:
                    del table[head]
        elif isinstance(sub, Table | InlineTable):
            sub[leaf] = _value(value, where)
        else:
            table[head] = _value({leaf: value}, f"rules[{name}].{head}")
        return
    if value is None:
        if key in table:
            del table[key]
        return
    current = table.get(key)
    if isinstance(value, Mapping) and isinstance(current, Table | InlineTable):
        for old in [k for k in current if k not in value]:  # keep the table's style
            del current[old]
        for sub_key, sub_value in value.items():
            current[sub_key] = _value(sub_value, f"{where}.{sub_key}")
    else:
        table[key] = _value(value, where)


def add_rule(doc: tomlkit.TOMLDocument, table: Mapping[str, Any]) -> None:
    """Append a ``[[rules]]`` entry; nested tables are written inline."""
    rules = _rules(doc)
    entry = _new_table(table, f"rules[{table.get('name')}]")
    if rules is None:
        doc["rules"] = tomlkit.aot()
        rules = _rules(doc)
        assert rules is not None
    elif rules.body and not rules.body[-1].as_string().endswith("\n\n"):
        entry.trivia.indent = "\n"  # a blank line before the new [[rules]] header
    rules.append(entry)


def remove_rule(doc: tomlkit.TOMLDocument, name: str) -> None:
    index = _rule_index(doc, name)
    rules = _rules(doc)
    assert rules is not None
    del rules[index]


def _accounts(doc: tomlkit.TOMLDocument, create: bool) -> Table | None:
    accounts = doc.get("accounts")
    if accounts is None and create:
        doc["accounts"] = tomlkit.table(is_super_table=True)
        accounts = doc["accounts"]
    if accounts is None or isinstance(accounts, Table):
        return accounts
    raise ConfigError(["accounts: must be a table"])


def add_account(doc: tomlkit.TOMLDocument, name: str, table: Mapping[str, Any]) -> None:
    """Add ``[accounts.NAME]``; refuses a name already in use."""
    accounts = _accounts(doc, create=True)
    assert accounts is not None
    if name in accounts:
        raise ConfigError([f'account "{name}" already exists'])
    entry = _new_table(table, f"accounts.{name}")
    entry.add(tomlkit.nl())  # a blank line before whatever header follows
    accounts[name] = entry


def remove_account(doc: tomlkit.TOMLDocument, name: str) -> None:
    accounts = _accounts(doc, create=False)
    if accounts is None or name not in accounts:
        raise ConfigError([f'unknown account "{name}"'])
    del accounts[name]


def set_key(doc: tomlkit.TOMLDocument, dotted: str, value: Any) -> None:
    """Set one top-level setting (``SETTINGS``); ``None`` removes it."""
    if dotted not in SETTINGS:
        raise ConfigError([f'unknown setting "{dotted}"'])
    head, _, leaf = dotted.partition(".")
    section = doc.get(head)
    if section is None:
        if value is None:
            return
        doc[head] = tomlkit.table()
        section = doc[head]
    if isinstance(section, OutOfOrderTableProxy):
        # tomlkit edits such a section unreliably (a key added to it cannot be deleted again).
        raise ConfigError(
            [f"{head}: this section is split across the file; join its parts into one [{head}] table by hand"]
        )
    if not isinstance(section, Table | InlineTable):
        raise ConfigError([f"{head}: must be a table"])
    if value is None:
        if leaf in section:
            del section[leaf]
    else:
        section[leaf] = _value(value, dotted)
