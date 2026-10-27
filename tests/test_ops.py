"""The operation layer (``calsync.ops``) on a temporary config file and state database."""

import stat
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from calsync import configfile, credentials, ops
from calsync.configfile import ConfigConflict, Snapshot, read_snapshot, validate_text
from calsync.errors import ConfigError
from calsync.ops import OpContext, OpResult, run_op
from calsync.store import Store
from tests.harness import NOW

TEXT = """\
# calsync test config
[google]
client_secret_file = "google-client.json"

[accounts.me-google]
provider = "google"
credential = { backend = "keychain", ref = "calsync:me-google" }

[accounts.family-icloud]
provider = "caldav"
server_url = "icloud"
username = "family@example.com"
credential = { backend = "env", ref = "FAMILY_PW" }

[accounts.spare-dav]  # not used by any rule
provider = "caldav"
server_url = "https://dav.example.com/"
username = "spare"
credential = { backend = "keychain", ref = "calsync:spare-dav" }

[accounts.old-dav]
provider = "caldav"
server_url = "https://dav.example.com/"
username = "old"
credential = { backend = "file", ref = "~/old-dav.secret" }

[[rules]]
name = "family"  # the main rule
source = { account = "me-google", calendar = "Family" }
target = { account = "family-icloud", calendar = "Home" }
alarms = "copy"  # keep

[rules.window]
past = "7d"
future = "180d"

[[rules]]
name = "work"
source = { account = "me-google", calendar = "Work" }
target = { account = "family-icloud", calendar = "Work" }
"""


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.deleted: list[str] = []

    def get_password(self, service: str, user: str) -> str | None:
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        self.store[(service, user)] = value

    def delete_password(self, service: str, user: str) -> None:
        import keyring.errors

        self.deleted.append(service)
        if (service, user) not in self.store:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.store[(service, user)]


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)
    return fake


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "config.toml"
        self.path.write_text(TEXT)
        self.path.chmod(0o600)
        self.state_dir = tmp_path / "state"
        self.store = Store(self.state_dir / "state.db")
        self.written: list[Snapshot] = []
        self.ctx = OpContext(
            config_path=self.path,
            state_dir=self.state_dir,
            store=self.store,
            source="cli",
            clock=lambda: NOW,
            on_config_written=self.written.append,
        )

    def run(self, op: str, /, *, dry_run: bool = False, **args: Any) -> OpResult:
        return run_op(self.ctx, op, args, dry_run)

    def config(self) -> Any:
        return validate_text(self.path.read_text(), self.path.parent)

    def rule(self, name: str) -> Any:
        return next(r for r in self.config().rules if r.name == name)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.store.close()


def _problems(env: Env, op: str, /, **args: Any) -> list[str]:
    before = env.path.read_bytes()
    with pytest.raises(ConfigError) as info:
        env.run(op, **args)
    assert env.path.read_bytes() == before  # nothing written
    assert env.store.config_changes() == [] and env.written == []
    return info.value.problems


# ----- the shape every config op shares --------------------------------------------------


def test_pause_writes_keeps_comments_and_records_history(env: Env) -> None:
    result = env.run("rule.pause", name="family")
    assert (result.op, result.changed, result.summary) == ("rule.pause", True, 'rule "family" paused')
    assert result.needs_human is None
    text = env.path.read_text()
    assert "# calsync test config" in text and "# the main rule" in text and "# keep" in text
    assert env.rule("family").paused is True and env.rule("work").paused is False
    assert "+paused = true" in result.diff
    [row] = env.store.config_changes()
    assert (row.source, row.summary, row.before, row.undoes) == ("cli", 'rule "family" paused', TEXT, None)
    assert row.at == NOW
    assert row.after_fingerprint == read_snapshot(env.path).fingerprint
    assert [s.fingerprint for s in env.written] == [row.after_fingerprint]
    assert env.written[0].text == text


def test_an_op_that_changes_nothing_writes_and_records_nothing(env: Env) -> None:
    env.run("rule.pause", name="family")
    mtime = env.path.stat().st_mtime_ns
    again = env.run("rule.pause", name="family")
    assert (again.changed, again.summary, again.diff) == (False, 'rule "family" is already paused', "")
    assert env.path.stat().st_mtime_ns == mtime
    assert len(env.store.config_changes()) == 1 and len(env.written) == 1
    assert env.run("rule.resume", name="work").summary == 'rule "work" is not paused'


def test_resume(env: Env) -> None:
    env.run("rule.pause", name="family")
    result = env.run("rule.resume", name="family")
    assert (result.changed, result.summary) == (True, 'rule "family" resumed')
    assert env.rule("family").paused is False
    assert env.path.read_text() == TEXT  # the key is removed again, not set to false


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("rule.pause", {"name": "family"}),
        ("rule.set", {"name": "family", "key": "window", "value": "all"}),
        ("rule.add", {"name": "kids", "source": "me-google:Kids", "target": "family-icloud:Kids"}),
        ("rule.remove", {"name": "work"}),
        ("account.add", {"name": "kid-dav", "provider": "caldav", "server_url": "icloud", "username": "k"}),
        ("account.remove", {"name": "spare-dav", "delete_credential": True}),
        ("config.set", {"key": "runtime.interval", "value": "2m"}),
    ],
)
def test_dry_run_writes_nothing_and_returns_the_diff(
    env: Env, keychain: FakeKeyring, name: str, args: dict[str, Any]
) -> None:
    keychain.store[("calsync:spare-dav", "calsync")] = "pw"
    before = env.path.read_bytes()
    result = run_op(env.ctx, name, args, True)
    assert result.changed is False and result.summary.startswith("dry run, nothing written: ")
    assert result.diff.startswith("--- config.toml\n+++ config.toml\n")
    assert env.path.read_bytes() == before
    assert sorted(p.name for p in env.path.parent.iterdir()) == ["config.toml", "state"]
    assert env.store.config_changes() == [] and env.written == []
    assert keychain.deleted == [] and ("calsync:spare-dav", "calsync") in keychain.store


def test_dry_run_still_validates(env: Env) -> None:
    problems = _problems(env, "rule.set", dry_run=True, name="family", key="fields.content", value="partial")
    assert problems == ['rules[family].fields.content: expected one of "full", "busy"']


def test_a_conflict_is_refused_and_nothing_recorded(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    real = configfile.write_config

    def edited_meanwhile(snapshot: Snapshot, text: str) -> Snapshot:
        env.path.write_text(TEXT + "# edited by hand\n")
        return real(snapshot, text)

    monkeypatch.setattr(configfile, "write_config", edited_meanwhile)
    with pytest.raises(ConfigConflict):
        env.run("rule.pause", name="family")
    assert env.path.read_text() == TEXT + "# edited by hand\n"
    assert env.store.config_changes() == [] and env.written == []


def test_unknown_op_and_bad_arguments(env: Env) -> None:
    assert _problems(env, "rule.explode", name="family") == ['unknown operation "rule.explode"']
    assert _problems(env, "rule.pause") == ['rule.pause: "name" is required']
    assert _problems(env, "rule.pause", name=3) == ['rule.pause: "name" must be a string']
    assert _problems(env, "rule.pause", name="family", colour="red") == [
        'rule.pause: unknown argument "colour"'
    ]
    assert _problems(env, "rule.remove", name="work", delete_copies="yes") == [
        'rule.remove: "delete_copies" must be true or false'
    ]
    assert _problems(env, "rule.pause", name="nope") == ['unknown rule "nope"']
    with pytest.raises(ConfigError) as info:
        run_op(env.ctx, "rule.pause", ["family"], False)  # type: ignore[arg-type]
    assert info.value.problems == ["rule.pause: the arguments must be an object"]


def test_op_names() -> None:
    assert set(ops.OPS) == {
        "config.set",
        "config.undo",
        "rule.add",
        "rule.set",
        "rule.remove",
        "rule.pause",
        "rule.resume",
        "account.add",
        "account.remove",
    }


def test_to_json_shape() -> None:
    plain = OpResult("rule.pause", True, 'rule "family" paused', diff="d", data={"k": 1})
    assert plain.to_json() == {
        "op": "rule.pause",
        "changed": True,
        "summary": 'rule "family" paused',
        "diff": "d",
        "data": {"k": 1},
        "needs_human": False,
        "command": None,
        "reason": None,
    }
    human = OpResult("account.add", True, "s", needs_human={"command": "calsync auth x", "reason": "r"})
    assert human.to_json()["needs_human"] is True
    assert (human.to_json()["command"], human.to_json()["reason"]) == ("calsync auth x", "r")
    for result in (plain, human):
        assert OpResult.from_json(result.to_json()) == result


# ----- rules -----------------------------------------------------------------------------


def test_rule_add(env: Env) -> None:
    result = env.run(
        "rule.add",
        name="kids",
        source="me-google:Kids",
        target="family-icloud:Kids at home",
        create_target=True,
        window="30d:365d",
        alarms="-10m,0m",
        content="busy",
        attendees="none",
        conference="none",
    )
    assert (result.changed, result.summary) == (
        True,
        'rule "kids" added: "me-google:Kids" to "family-icloud:Kids at home"',
    )
    kids = env.rule("kids")
    assert (kids.source.account, kids.source.calendar) == ("me-google", "Kids")
    assert (kids.target.account, kids.target.calendar, kids.target.create_if_missing) == (
        "family-icloud",
        "Kids at home",
        True,
    )
    assert (kids.window.past, kids.window.future) == (timedelta(days=30), timedelta(days=365))
    assert kids.alarms == (timedelta(minutes=-10), timedelta(0))
    assert (kids.fields.content, kids.fields.attendees, kids.fields.conference) == ("busy", "none", "none")
    assert "# the main rule" in env.path.read_text()


def test_rule_add_defaults_to_full_sync(env: Env) -> None:
    env.run("rule.add", name="kids", source="me-google:Kids", target="family-icloud:Kids")
    kids = env.rule("kids")
    assert kids.window is None and kids.alarms == "copy" and kids.target.create_if_missing is False
    env.run("rule.add", name="all-time", source="me-google:Old", target="family-icloud:Old", window="all")
    assert env.rule("all-time").window is None


def test_rule_add_problems(env: Env) -> None:
    assert _problems(env, "rule.add", name="x", source="ghost:Kids", target="family-icloud:Kids") == [
        "rules[x].source.account: unknown account 'ghost'"
    ]
    assert _problems(env, "rule.add", name="x", source="family-icloud:Kids", target="family-icloud:Kids") == [
        "rules[x].source.account: 'family-icloud' must be a google account"
    ]
    assert _problems(env, "rule.add", name="x", source="me-google", target="family-icloud:Kids") == [
        'rule.add: source: expected ACCOUNT:CALENDAR, got "me-google"'
    ]
    assert _problems(
        env, "rule.add", name="x", source="me-google:Kids", target="family-icloud:Kids", window="30d"
    ) == ['rule.add: window: expected "all" or PAST:FUTURE such as 30d:365d, got "30d"']
    assert _problems(env, "rule.add", name="x", source="me-google:Family", target="family-icloud:Home") == [
        'a rule already syncs "me-google:Family" to "family-icloud:Home": "family"'
    ]
    assert _problems(env, "rule.add", name="work", source="me-google:Kids", target="family-icloud:Kids") == [
        "rules[work]: duplicate rule name"
    ]


@pytest.mark.parametrize(
    ("key", "value", "check"),
    [
        ("window", "all", lambda r: r.window is None),
        (
            "window",
            "30d:365d",
            lambda r: (r.window.past, r.window.future) == (timedelta(days=30), timedelta(days=365)),
        ),
        ("alarms", "strip", lambda r: r.alarms == "strip"),
        ("alarms", "-10m,0m", lambda r: r.alarms == (timedelta(minutes=-10), timedelta(0))),
        ("paused", "true", lambda r: r.paused is True),
        ("retire", "true", lambda r: r.retire is True),
        ("delete_guard.max_count", "25", lambda r: r.delete_guard.max_count == 25),
        ("delete_guard.max_ratio", "0.25", lambda r: r.delete_guard.max_ratio == 0.25),
        ("fields.content", "busy", lambda r: r.fields.content == "busy"),
        ("fields.attendees", "none", lambda r: r.fields.attendees == "none"),
    ],
)
def test_rule_set(env: Env, key: str, value: str, check: Any) -> None:
    result = env.run("rule.set", name="family", key=key, value=value)
    assert result.changed and result.summary == f'rule "family": {key} set'
    assert check(env.rule("family"))


def test_rule_set_takes_typed_values_too(env: Env) -> None:
    env.run("rule.set", name="family", key="delete_guard.max_count", value=3)
    env.run("rule.set", name="family", key="paused", value=True)
    assert env.rule("family").delete_guard.max_count == 3 and env.rule("family").paused


def test_rule_set_problems(env: Env) -> None:
    assert _problems(env, "rule.set", name="family", key="window", value="30d") == [
        'rule.set: window: expected "all" or PAST:FUTURE such as 30d:365d, got "30d"'
    ]
    assert _problems(env, "rule.set", name="family", key="paused", value="yes") == [
        'rule.set: paused: expected true or false, got "yes"'
    ]
    assert _problems(env, "rule.set", name="family", key="delete_guard.max_count", value="many") == [
        'rule.set: delete_guard.max_count: expected a number, got "many"'
    ]
    assert _problems(env, "rule.set", name="family", key="delete_guard.max_count", value="-1") == [
        "rules[family].delete_guard.max_count: must be an integer >= 0"
    ]
    assert _problems(env, "rule.set", name="family", key="colour", value="red") == [
        'unknown setting "colour"'
    ]


def test_rule_remove_keeps_the_copies(env: Env) -> None:
    result = env.run("rule.remove", name="work")
    assert result.summary == 'rule "work" removed; its copies stay in "Work"'
    assert [r.name for r in env.config().rules] == ["family"]


def test_rule_remove_with_delete_copies_retires_it(env: Env) -> None:
    result = env.run("rule.remove", name="work", delete_copies=True)
    assert result.summary == 'rule "work" retires: its copies in "Work" are deleted, then the rule is removed'
    assert env.rule("work").retire is True


# ----- accounts --------------------------------------------------------------------------


def test_account_add_needs_a_human_to_sign_in(env: Env) -> None:
    result = env.run(
        "account.add", name="kid-icloud", provider="caldav", server_url="icloud", username="kid@example.com"
    )
    assert (result.changed, result.summary) == (True, 'account "kid-icloud" added')
    assert result.needs_human == {
        "command": "calsync auth kid-icloud",
        "reason": "a person signs in (Google) or types the app-specific password (iCloud)",
    }
    account = env.config().accounts["kid-icloud"]
    assert (account.provider, account.server_url, account.username) == ("caldav", "icloud", "kid@example.com")
    assert (account.credential.backend, account.credential.ref) == ("keychain", "calsync:kid-icloud")
    assert "# not used by any rule" in env.path.read_text()


def test_account_add_problems(env: Env) -> None:
    assert _problems(env, "account.add", name="me-google", provider="google") == [
        'account "me-google" already exists'
    ]
    assert _problems(env, "account.add", name="x", provider="exchange") == [
        "accounts.x.provider: must be one of google, caldav"
    ]
    assert _problems(env, "account.add", name="x", provider="caldav") == [
        'accounts.x.server_url: required for caldav accounts: "icloud" (detects the global or China '
        "mainland region) or an http(s) URL",
        "accounts.x.username: required for caldav accounts",
    ]


def test_account_add_creates_a_missing_config(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "state.db")
    try:
        path = tmp_path / "fresh" / "config.toml"
        ctx = OpContext(path, tmp_path / "state", store, "cli", lambda: NOW)
        args = {
            "name": "dav",
            "provider": "caldav",
            "server_url": "https://dav.example.com/",
            "username": "u",
        }
        result = run_op(ctx, "account.add", args, False)
        assert result.changed
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert list(validate_text(path.read_text(), path.parent).accounts) == ["dav"]
        [row] = store.config_changes()
        assert row.before == ""
    finally:
        store.close()


def test_account_remove_is_refused_while_a_rule_uses_it(env: Env) -> None:
    assert _problems(env, "account.remove", name="family-icloud") == [
        'account "family-icloud" is used by rules "family", "work"; remove them first'
    ]
    assert _problems(env, "account.remove", name="ghost") == ['unknown account "ghost"']


def test_account_remove_keeps_the_credential_by_default(env: Env, keychain: FakeKeyring) -> None:
    keychain.store[("calsync:spare-dav", "calsync")] = "pw"
    result = env.run("account.remove", name="spare-dav")
    assert result.summary == 'account "spare-dav" removed; its credential was kept'
    assert "spare-dav" not in env.config().accounts
    assert keychain.deleted == [] and ("calsync:spare-dav", "calsync") in keychain.store


def test_account_remove_deletes_the_keychain_entry(env: Env, keychain: FakeKeyring) -> None:
    keychain.store[("calsync:spare-dav", "calsync")] = "pw"
    result = env.run("account.remove", name="spare-dav", delete_credential=True)
    assert result.summary == 'account "spare-dav" removed; its keychain entry was deleted'
    assert keychain.store == {}
    [row] = env.store.config_changes()
    assert row.summary == 'account "spare-dav" removed'


def test_account_remove_with_a_missing_keychain_entry(env: Env, keychain: FakeKeyring) -> None:
    result = env.run("account.remove", name="spare-dav", delete_credential=True)
    assert result.summary == 'account "spare-dav" removed; its keychain entry was deleted'


def test_account_remove_leaves_file_and_env_credentials_to_a_person(env: Env, keychain: FakeKeyring) -> None:
    result = env.run("account.remove", name="old-dav", delete_credential=True)
    assert result.summary == 'account "old-dav" removed; its credential (file) must be removed by hand'
    assert keychain.deleted == []


def test_a_keychain_failure_after_the_write_is_reported_not_raised(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyring.errors

    class Locked(FakeKeyring):
        def delete_password(self, service: str, user: str) -> None:
            raise keyring.errors.KeyringLocked("locked")

    monkeypatch.setattr(credentials, "_keyring", lambda: Locked())
    result = env.run("account.remove", name="spare-dav", delete_credential=True)
    assert result.changed and "spare-dav" not in env.config().accounts
    assert result.summary == (
        'account "spare-dav" removed; its keychain entry "calsync:spare-dav" could not be deleted '
        "(KeyringLocked): remove it by hand"
    )


# ----- settings --------------------------------------------------------------------------


def test_config_set(env: Env) -> None:
    result = env.run("config.set", key="notify.bark_url", value="https://bark.example.com/DEVICEKEY")
    assert result.summary == "notify.bark_url set"  # never the value: it can be a secret URL
    assert "DEVICEKEY" not in env.store.config_changes()[0].summary
    env.run("config.set", key="web.allowed_hosts", value="mac-mini.local, nas.local")
    env.run("config.set", key="runtime.jitter", value="0.2")
    env.run("config.set", key="notify.failure_threshold", value="5")
    config = env.config()
    assert config.notify.bark_url == "https://bark.example.com/DEVICEKEY"
    assert config.web.allowed_hosts == ("mac-mini.local", "nas.local")
    assert (config.runtime.jitter, config.notify.failure_threshold) == (0.2, 5)
    cleared = env.run("config.set", key="notify.bark_url", value="")
    assert cleared.summary == "notify.bark_url cleared"
    assert env.config().notify.bark_url is None
    assert (
        env.run("config.set", key="runtime.jitter", value="0.2").summary
        == "runtime.jitter is already set to that value"
    )


def test_config_set_problems(env: Env) -> None:
    assert _problems(env, "config.set", key="runtime.interval", value="soon") == [
        "runtime.interval: invalid duration 'soon'; expected e.g. 30s, 5m, 2h, 7d, -15m"
    ]
    assert _problems(env, "config.set", key="notify.public_url", value="calsync.example.com") == [
        "notify.public_url: must be an http(s) URL"
    ]
    assert _problems(env, "config.set", key="web.listen", value="0.0.0.0:1") == [
        'unknown setting "web.listen"'
    ]


# ----- undo ------------------------------------------------------------------------------


def test_undo_walks_back_one_change_at_a_time(env: Env) -> None:
    env.run("rule.pause", name="family")
    after_pause = env.path.read_text()
    env.run("rule.set", name="work", key="alarms", value="strip")
    first = env.run("config.undo")
    assert first.changed and first.summary == 'undid change 2 (rule "work": alarms set)'
    assert first.data == {"undoes": 2}
    assert env.path.read_text() == after_pause
    second = env.run("config.undo")
    assert second.summary == 'undid change 1 (rule "family" paused)'
    assert env.path.read_text() == TEXT
    rows = env.store.config_changes()
    assert [(r.id, r.undoes) for r in rows] == [(4, 1), (3, 2), (2, None), (1, None)]
    assert rows[0].summary == 'undid change 1 (rule "family" paused)' and rows[0].source == "cli"
    assert len(env.written) == 4
    nothing = env.run("config.undo")
    assert (nothing.changed, nothing.summary) == (False, "nothing to undo")
    assert len(env.store.config_changes()) == 4


def test_undo_dry_run(env: Env) -> None:
    env.run("rule.pause", name="family")
    paused = env.path.read_text()
    result = env.run("config.undo", dry_run=True)
    assert result.summary == 'dry run, nothing written: undid change 1 (rule "family" paused)'
    assert "-paused = true" in result.diff
    assert env.path.read_text() == paused and len(env.store.config_changes()) == 1


def test_undo_refuses_to_discard_a_later_edit(env: Env) -> None:
    env.run("rule.pause", name="family")
    edited = env.path.read_text() + "# edited by hand\n"
    env.path.write_text(edited)
    with pytest.raises(ConfigError) as info:
        env.run("config.undo")
    assert info.value.problems == [
        "the file was changed after change 1; undoing it would discard those edits — edit the file by hand"
    ]
    assert env.path.read_text() == edited


def test_rule_set_false_removes_the_flag(env: Env) -> None:
    env.run("rule.set", name="family", key="paused", value="true")
    result = env.run("rule.set", name="family", key="paused", value="false")
    assert result.changed and env.path.read_text() == TEXT
    assert env.run("rule.set", name="family", key="retire", value=False).changed is False


# ----- fix round 1 -----------------------------------------------------------------------


class StuckKeyring(FakeKeyring):
    """macOS's backend: every keychain error on delete (denied, locked) is a PasswordDeleteError."""

    def delete_password(self, service: str, user: str) -> None:
        import keyring.errors

        self.deleted.append(service)
        raise keyring.errors.PasswordDeleteError("denied")


def test_a_keychain_entry_that_survives_the_delete_is_reported(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = StuckKeyring()
    fake.store[("calsync:spare-dav", "calsync")] = "pw"
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)
    result = env.run("account.remove", name="spare-dav", delete_credential=True)
    assert result.changed and "spare-dav" not in env.config().accounts
    assert result.summary == (
        'account "spare-dav" removed; its keychain entry "calsync:spare-dav" could not be deleted '
        "(PasswordDeleteError): remove it by hand"
    )
    assert ("calsync:spare-dav", "calsync") in fake.store


SHARED = """
[accounts.spare-twin]
provider = "caldav"
server_url = "https://dav.example.com/"
username = "twin"
credential = { backend = "keychain", ref = "calsync:spare-dav" }
"""


def test_a_credential_another_account_uses_is_kept(env: Env, keychain: FakeKeyring) -> None:
    env.path.write_text(TEXT + SHARED)
    keychain.store[("calsync:spare-dav", "calsync")] = "pw"
    dry = env.run("account.remove", dry_run=True, name="spare-dav", delete_credential=True)
    kept = '; its credential is also used by account "spare-twin", so it was kept'
    assert dry.summary == 'dry run, nothing written: account "spare-dav" removed' + kept.replace(
        "was kept", "would be kept"
    )
    result = env.run("account.remove", name="spare-dav", delete_credential=True)
    assert result.summary == 'account "spare-dav" removed' + kept
    assert keychain.deleted == [] and ("calsync:spare-dav", "calsync") in keychain.store


class BrokenHistory(Store):
    def record_config_change(self, **kwargs: Any) -> int:  # type: ignore[override]
        raise RuntimeError("disk full")


def test_a_write_whose_history_cannot_be_recorded(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "config.toml"
    path.write_text(TEXT)
    store = BrokenHistory(tmp_path / "state" / "state.db")
    written: list[Snapshot] = []
    try:
        ctx = OpContext(path, tmp_path / "state", store, "cli", lambda: NOW, on_config_written=written.append)
        with caplog.at_level("WARNING"):
            result = run_op(ctx, "rule.pause", {"name": "family"}, False)
    finally:
        store.close()
    assert result.changed and result.summary == 'rule "family" paused; history not recorded'
    assert "paused = true" in path.read_text() and len(written) == 1
    assert [r.getMessage() for r in caplog.records] == ["config history not recorded (RuntimeError)"]


def test_the_change_that_created_the_file_cannot_be_undone(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "state.db")
    try:
        path = tmp_path / "fresh" / "config.toml"
        ctx = OpContext(path, tmp_path / "state", store, "cli", lambda: NOW)
        args = {
            "name": "dav",
            "provider": "caldav",
            "server_url": "https://dav.example.com/",
            "username": "u",
        }
        run_op(ctx, "account.add", args, False)
        created = path.read_text()
        for dry_run in (True, False):
            with pytest.raises(ConfigError) as info:
                run_op(ctx, "config.undo", {}, dry_run)
            assert info.value.problems == [
                "this change created the config file; there is nothing earlier to go back to"
            ]
        assert path.read_text() == created and len(store.config_changes()) == 1
    finally:
        store.close()


def test_a_dry_run_says_whether_it_would_change_anything(env: Env) -> None:
    assert env.run("rule.pause", dry_run=True, name="family").data == {"would_change": True}
    assert env.run("rule.resume", dry_run=True, name="family").data == {"would_change": False}
    assert env.run("config.undo", dry_run=True).data == {"would_change": False}
    env.run("rule.pause", name="family")
    assert env.run("config.undo", dry_run=True).data == {"undoes": 1, "would_change": True}
    assert env.run("rule.resume", name="family").data == {}  # not a dry run


def test_config_set_google_client_secret_file(env: Env) -> None:
    result = env.run("config.set", key="google.client_secret_file", value="other-client.json")
    assert result.summary == "google.client_secret_file set"
    assert env.config().google_client_secret_file == env.path.parent / "other-client.json"
