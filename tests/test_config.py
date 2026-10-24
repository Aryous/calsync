from datetime import timedelta
from pathlib import Path

import pytest

from calsync.config import CredentialRef, default_config_path, load_config, parse_config
from calsync.durations import parse_duration
from calsync.errors import ConfigError

VALID = {
    "google": {"client_secret_file": "google-client.json"},
    "notify": {"bark_url": "https://api.day.app/KEY"},
    "runtime": {"interval": "90s"},
    "accounts": {
        "me-google": {"provider": "google", "credential": {"backend": "keychain", "ref": "calsync/me"}},
        "family-icloud": {
            "provider": "caldav",
            "server_url": "https://caldav.icloud.com/",
            "username": "family@example.com",
            "credential": {"backend": "env", "ref": "FAMILY_PW"},
            "max_concurrency": 3,
        },
    },
    "rules": [
        {
            "name": "family",
            "source": {"account": "me-google", "calendar": "Family"},
            "target": {"account": "family-icloud", "calendar": "Family", "create_if_missing": True},
            "window": {"past": "7d", "future": "180d"},
            "alarms": ["-5m", "0m"],
        }
    ],
}


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("5m", 300), ("2h", 7200), ("7d", 604800), ("1w", 604800), ("-15m", -900), ("0m", 0)],
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["", "5", "m5", "5 minutes", "1.5h"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration(text)


def test_parse_valid_config(tmp_path: Path) -> None:
    cfg = parse_config(VALID, base_dir=tmp_path)
    assert cfg.google_client_secret_file == tmp_path / "google-client.json"
    assert cfg.runtime.interval == timedelta(seconds=90)
    assert cfg.accounts["family-icloud"].max_concurrency == 3
    assert cfg.accounts["me-google"].credential == CredentialRef("keychain", "calsync/me")
    (rule,) = cfg.rules
    assert rule.target.create_if_missing is True
    assert rule.alarms == (timedelta(minutes=-5), timedelta(0))
    assert rule.window.future == timedelta(days=180)
    assert rule.delete_guard.max_count == 10


def test_collects_all_problems(tmp_path: Path) -> None:
    broken = {
        "accounts": {
            "g": {"provider": "google", "credential": {"backend": "keychain", "ref": "x"}},
            "bad": {"provider": "exchange", "credential": {"backend": "keychain", "ref": "x"}},
            "dav": {"provider": "caldav", "credential": {"backend": "file", "ref": "/tmp/x"}},
        },
        "rules": [
            {
                "name": "a",
                "source": {"account": "nope", "calendar": "c"},
                "target": {"account": "dav", "calendar": "c"},
            },
            {
                "name": "b",
                "source": {"account": "g", "calendar": "c"},
                "target": {"account": "g", "calendar": "c"},
                "alarms": 5,
            },
            {
                "name": "c",
                "source": {"account": "g", "calendar": "c"},
                "target": {"account": "dav", "calendar": "c"},
                "window": {"past": "soon"},
            },
        ],
    }
    with pytest.raises(ConfigError) as info:
        parse_config(broken, base_dir=tmp_path)
    text = "\n".join(info.value.problems)
    assert "accounts.bad.provider" in text
    assert "accounts.dav.server_url" in text
    assert "accounts.dav.username" in text
    assert "rules[a].source.account: unknown account 'nope'" in text
    assert "rules[b].target.account: 'g' must be a caldav account" in text
    assert "rules[b].alarms" in text
    assert "rules[c].window.past" in text
    assert "google.client_secret_file" in text


def test_duplicate_rule_names(tmp_path: Path) -> None:
    data = {**VALID, "rules": VALID["rules"] * 2}
    with pytest.raises(ConfigError, match="duplicate rule name"):
        parse_config(data, base_dir=tmp_path)


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "missing.toml")


def test_load_config_reads_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[accounts.d]\nprovider = "caldav"\nserver_url = "https://dav.example.com/"\n'
        'username = "u"\ncredential = { backend = "env", ref = "PW" }\n'
    )
    cfg = load_config(path)
    assert cfg.rules == ()
    assert cfg.accounts["d"].server_url == "https://dav.example.com/"


def test_default_config_path_env_override() -> None:
    assert default_config_path({"CALSYNC_CONFIG": "/etc/calsync.toml"}) == Path("/etc/calsync.toml")
    assert default_config_path({}).name == "config.toml"
