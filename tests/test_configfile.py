import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest
import tomlkit

from calsync.config import WindowSpec
from calsync.configfile import (
    ConfigConflict,
    add_account,
    add_rule,
    diff_text,
    load_doc,
    read_snapshot,
    remove_account,
    remove_rule,
    rule_table,
    set_key,
    set_rule_key,
    validate_text,
    write_config,
)
from calsync.errors import ConfigError

TEXT = """\
# calsync test config
[google]
client_secret_file = "google-client.json"

[accounts.me-google]
provider = "google"
credential = { backend = "keychain", ref = "calsync/me" }

[accounts.family-icloud]
provider = "caldav"
server_url = "icloud"
username = "family@example.com"
credential = { backend = "env", ref = "FAMILY_PW" }

[[rules]]
name = "family"  # the main rule
source = { account = "me-google", calendar = "Family" }
target = { account = "family-icloud", calendar = "Family" }
alarms = "copy"  # keep

[rules.window]
past = "7d"
future = "180d"

[[rules]]
name = "work"
source = { account = "me-google", calendar = "Work" }
target = { account = "family-icloud", calendar = "Work" }
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(TEXT)
    path.chmod(0o600)
    return path


def _edit(path: Path, edit: object) -> str:
    doc = load_doc(path.read_text())
    edit(doc)  # type: ignore[operator]
    return tomlkit.dumps(doc)


def _names(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir())


def test_comments_and_order_survive_an_edit(config_path: Path) -> None:
    snap = read_snapshot(config_path)
    doc = load_doc(snap.text)
    set_rule_key(doc, "family", "paused", True)
    set_rule_key(doc, "family", "alarms", ["-10m", "0m"])
    write_config(snap, tomlkit.dumps(doc))
    out = config_path.read_text()
    assert "# calsync test config" in out
    assert 'alarms = ["-10m", "0m"]  # keep' in out
    assert 'name = "family"  # the main rule' in out
    order = [
        out.index(s) for s in ("[google]", "[accounts.me-google]", "[accounts.family-icloud]", "[[rules]]")
    ]
    assert order == sorted(order)
    assert out.index('name = "family"') < out.index("paused = true") < out.index("[rules.window]")
    assert validate_text(out, config_path.parent).rules[0].paused is True


def test_invalid_value_is_refused_and_file_unchanged(config_path: Path) -> None:
    before = config_path.read_bytes()
    snap = read_snapshot(config_path)
    new = _edit(config_path, lambda d: set_rule_key(d, "family", "delete_guard.max_ratio", 1.5))
    with pytest.raises(ConfigError) as info:
        write_config(snap, new)
    assert any(p.startswith("rules[family].delete_guard.max_ratio") for p in info.value.problems)
    assert config_path.read_bytes() == before
    assert _names(config_path) == ["config.toml"]


def test_external_change_is_a_conflict(config_path: Path) -> None:
    snap = read_snapshot(config_path)
    new = _edit(config_path, lambda d: set_rule_key(d, "family", "paused", True))
    config_path.write_text(TEXT + "\n# edited by hand\n")
    changed = config_path.read_bytes()
    with pytest.raises(ConfigConflict) as info:
        write_config(snap, new)
    assert isinstance(info.value, ConfigError)
    assert info.value.problems == ["the file was changed outside calsync; reload and try again"]
    assert config_path.read_bytes() == changed
    assert _names(config_path) == ["config.toml"]


def test_conflict_when_the_file_appears_or_disappears(tmp_path: Path, config_path: Path) -> None:
    missing = tmp_path / "new.toml"
    snap = read_snapshot(missing)
    assert (snap.text, snap.fingerprint) == ("", "")
    missing.write_text("# someone else\n")
    with pytest.raises(ConfigConflict):
        write_config(snap, TEXT)

    snap = read_snapshot(config_path)
    config_path.unlink()
    with pytest.raises(ConfigConflict):
        write_config(snap, TEXT + "\n")
    assert not config_path.exists()


def test_backup_mode_and_no_temp_left(config_path: Path) -> None:
    before = config_path.read_bytes()
    snap = read_snapshot(config_path)
    new = _edit(config_path, lambda d: set_rule_key(d, "family", "retire", True))
    result = write_config(snap, new)
    bak = config_path.with_name("config.toml.bak")
    assert bak.read_bytes() == before
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert _names(config_path) == ["config.toml", "config.toml.bak"]
    assert result.text == new
    assert result.fingerprint == read_snapshot(config_path).fingerprint


def test_existing_mode_is_kept_and_bak_forced_to_0600(config_path: Path) -> None:
    config_path.chmod(0o640)
    bak = config_path.with_name("config.toml.bak")
    bak.write_text("old")
    bak.chmod(0o644)
    snap = read_snapshot(config_path)
    write_config(snap, _edit(config_path, lambda d: set_rule_key(d, "family", "paused", True)))
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600


def test_new_file_is_0600_without_bak(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    result = write_config(read_snapshot(path), TEXT)
    assert path.read_text() == TEXT
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _names(path) == ["config.toml"]
    assert result.fingerprint == read_snapshot(path).fingerprint


def test_failed_replace_leaves_no_temp(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = config_path.read_bytes()
    snap = read_snapshot(config_path)
    new = _edit(config_path, lambda d: set_rule_key(d, "family", "paused", True))

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_config(snap, new)
    assert config_path.read_bytes() == before
    assert _names(config_path) == ["config.toml", "config.toml.bak"]


def test_identical_text_writes_nothing(config_path: Path) -> None:
    old = 1_000_000_000
    os.utime(config_path, (old, old))
    mtime = config_path.stat().st_mtime_ns
    snap = read_snapshot(config_path)
    assert write_config(snap, snap.text) is snap
    assert config_path.stat().st_mtime_ns == mtime
    assert _names(config_path) == ["config.toml"]


def test_window_none_removes_the_table(config_path: Path) -> None:
    new = _edit(config_path, lambda d: set_rule_key(d, "family", "window", None))
    assert "[rules.window]" not in new
    assert "past" not in new
    assert validate_text(new, config_path.parent).rules[0].window is None


def test_window_set(config_path: Path) -> None:
    window = {"past": "30d", "future": "365d"}
    for rule in ("family", "work"):  # an existing [rules.window] table, and none yet
        new = _edit(config_path, lambda d, r=rule: set_rule_key(d, r, "window", window))
        config_path.write_text(new)
    rules = validate_text(config_path.read_text(), config_path.parent).rules
    expected = WindowSpec(timedelta(days=30), timedelta(days=365))
    assert [r.window for r in rules] == [expected, expected]


def test_rule_keys_round_trip(config_path: Path) -> None:
    def edit(doc: tomlkit.TOMLDocument) -> None:
        set_rule_key(doc, "work", "alarms", ["-10m", "0m"])
        set_rule_key(doc, "work", "fields.content", "busy")
        set_rule_key(doc, "work", "fields.attendees", "notes")
        set_rule_key(doc, "work", "fields.conference", "none")
        set_rule_key(doc, "work", "delete_guard.max_count", 3)
        set_rule_key(doc, "work", "delete_guard.max_ratio", 0.25)

    rule = validate_text(_edit(config_path, edit), config_path.parent).rules[1]
    assert rule.alarms == (timedelta(minutes=-10), timedelta(0))
    assert (rule.fields.content, rule.fields.attendees, rule.fields.conference) == ("busy", "notes", "none")
    assert (rule.delete_guard.max_count, rule.delete_guard.max_ratio) == (3, 0.25)


def test_rule_table_finds_by_name(config_path: Path) -> None:
    doc = load_doc(config_path.read_text())
    assert rule_table(doc, "work")["name"] == "work"


def test_add_and_remove_rule(config_path: Path) -> None:
    def add(doc: tomlkit.TOMLDocument) -> None:
        add_rule(
            doc,
            {
                "name": "school",
                "source": {"account": "me-google", "calendar": "School"},
                "target": {"account": "family-icloud", "calendar": "School", "create_if_missing": True},
                "alarms": ["-5m"],
            },
        )

    added = _edit(config_path, add)
    rules = validate_text(added, config_path.parent).rules
    assert [r.name for r in rules] == ["family", "work", "school"]
    assert rules[2].target.create_if_missing is True
    config_path.write_text(added)
    removed = _edit(config_path, lambda d: remove_rule(d, "work"))
    assert [r.name for r in validate_text(removed, config_path.parent).rules] == ["family", "school"]
    assert "# calsync test config" in removed


def test_add_rule_to_a_file_without_rules(tmp_path: Path) -> None:
    doc = load_doc('[google]\nclient_secret_file = "g.json"\n')
    add_account(doc, "g", {"provider": "google", "credential": {"backend": "keychain", "ref": "x"}})
    add_account(
        doc,
        "d",
        {
            "provider": "caldav",
            "server_url": "icloud",
            "username": "u@example.com",
            "credential": {"backend": "env", "ref": "PW"},
        },
    )
    add_rule(
        doc,
        {
            "name": "r",
            "source": {"account": "g", "calendar": "A"},
            "target": {"account": "d", "calendar": "B"},
        },
    )
    cfg = validate_text(tomlkit.dumps(doc), tmp_path)
    assert sorted(cfg.accounts) == ["d", "g"]
    assert [r.name for r in cfg.rules] == ["r"]


def test_add_and_remove_account(config_path: Path) -> None:
    def add(doc: tomlkit.TOMLDocument) -> None:
        add_account(
            doc,
            "other-dav",
            {
                "provider": "caldav",
                "server_url": "https://dav.example.com/",
                "username": "other@example.com",
                "credential": {"backend": "env", "ref": "OTHER_PW"},
            },
        )

    added = _edit(config_path, add)
    cfg = validate_text(added, config_path.parent)
    assert cfg.accounts["other-dav"].server_url == "https://dav.example.com/"
    assert added.index("[accounts.other-dav]") < added.index("[[rules]]")
    config_path.write_text(added)
    removed = _edit(config_path, lambda d: remove_account(d, "other-dav"))
    assert sorted(validate_text(removed, config_path.parent).accounts) == ["family-icloud", "me-google"]


def test_add_existing_account_is_refused(config_path: Path) -> None:
    doc = load_doc(config_path.read_text())
    with pytest.raises(ConfigError) as info:
        add_account(doc, "me-google", {"provider": "google"})
    assert info.value.problems == ['account "me-google" already exists']


def test_set_key(config_path: Path) -> None:
    def edit(doc: tomlkit.TOMLDocument) -> None:
        set_key(doc, "notify.bark_url", "https://bark.example.com/KEY")
        set_key(doc, "notify.public_url", "http://calsync.example.com:8766")
        set_key(doc, "notify.failure_threshold", 5)
        set_key(doc, "runtime.interval", "2m")
        set_key(doc, "runtime.jitter", 0.2)
        set_key(doc, "web.allowed_hosts", ["mac-mini.local"])

    new = _edit(config_path, edit)
    cfg = validate_text(new, config_path.parent)
    assert cfg.notify.bark_url == "https://bark.example.com/KEY"
    assert cfg.notify.public_url == "http://calsync.example.com:8766"
    assert cfg.notify.failure_threshold == 5
    assert cfg.runtime.interval == timedelta(minutes=2)
    assert cfg.runtime.jitter == 0.2
    assert cfg.web.allowed_hosts == ("mac-mini.local",)
    config_path.write_text(new)
    cleared = _edit(config_path, lambda d: set_key(d, "notify.bark_url", None))
    assert validate_text(cleared, config_path.parent).notify.bark_url is None


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: rule_table(d, "nope"), 'unknown rule "nope"'),
        (lambda d: set_rule_key(d, "nope", "paused", True), 'unknown rule "nope"'),
        (lambda d: remove_rule(d, "nope"), 'unknown rule "nope"'),
        (lambda d: set_rule_key(d, "family", "colour", "red"), 'unknown setting "colour"'),
        (lambda d: set_rule_key(d, "family", "fields.colour", "red"), 'unknown setting "fields.colour"'),
        (lambda d: remove_account(d, "nope"), 'unknown account "nope"'),
        (lambda d: set_key(d, "web.listen", "0.0.0.0:1"), 'unknown setting "web.listen"'),
        (lambda d: set_key(d, "colour", "red"), 'unknown setting "colour"'),
    ],
)
def test_unknown_names(config_path: Path, edit: object, message: str) -> None:
    doc = load_doc(config_path.read_text())
    with pytest.raises(ConfigError) as info:
        edit(doc)  # type: ignore[operator]
    assert info.value.problems == [message]


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: rule_table(d, "r"),
        lambda d: set_rule_key(d, "r", "paused", True),
        lambda d: remove_rule(d, "r"),
    ],
)
def test_unknown_rule_in_a_file_without_rules(edit: object) -> None:
    doc = load_doc('[google]\nclient_secret_file = "g.json"\n')
    with pytest.raises(ConfigError) as info:
        edit(doc)  # type: ignore[operator]
    assert info.value.problems == ['unknown rule "r"']


def test_bad_toml_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_doc("[broken")
    with pytest.raises(ConfigError):
        validate_text("[broken", tmp_path)


def test_validate_text_resolves_paths_against_base_dir(config_path: Path) -> None:
    cfg = validate_text(TEXT, config_path.parent)
    assert cfg.google_client_secret_file == config_path.parent / "google-client.json"


def test_diff_text() -> None:
    diff = diff_text("a = 1\nb = 2\n", "a = 1\nb = 3\n")
    assert "--- config.toml" in diff
    assert "-b = 2" in diff
    assert "+b = 3" in diff
    assert diff_text("x\n", "x\n") == ""
