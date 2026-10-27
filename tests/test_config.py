import copy
from datetime import timedelta
from pathlib import Path

import pytest

from calsync.config import CredentialRef, WindowSpec, default_config_path, load_config, parse_config
from calsync.durations import parse_duration
from calsync.errors import ConfigError
from calsync.model import FieldPolicy

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
    assert rule.window is not None
    assert rule.window.future == timedelta(days=180)
    assert rule.delete_guard.max_count == 10


def test_window_is_optional(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    del data["rules"][0]["window"]
    cfg = parse_config(data, tmp_path)
    assert cfg.rules[0].window is None


def test_window_given_is_kept(tmp_path: Path) -> None:
    cfg = parse_config(copy.deepcopy(VALID), tmp_path)
    assert cfg.rules[0].window == WindowSpec(timedelta(days=7), timedelta(days=180))


def test_bad_window_still_reports_later_rule_problems(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["rules"][0]["window"] = {"past": "soon"}
    data["rules"][0]["alarms"] = 5
    with pytest.raises(ConfigError) as info:
        parse_config(data, tmp_path)
    text = "\n".join(info.value.problems)
    assert "rules[family].window.past" in text
    assert "rules[family].alarms" in text


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


def _mutated(**changes: object) -> dict[str, object]:
    data = copy.deepcopy(VALID)
    for dotted, value in changes.items():
        parts = dotted.split("__")
        node: object = data
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]  # type: ignore[index]
        node[parts[-1]] = value  # type: ignore[index]
    return data


BAD_VALUES = [
    ({"runtime__interval": "0s"}, "runtime.interval: must be positive"),
    ({"runtime__interval": "-30s"}, "runtime.interval: must be positive"),
    ({"runtime__jitter": "x"}, "runtime.jitter"),
    ({"runtime": "60s"}, "runtime: must be a table"),
    ({"notify": 5}, "notify: must be a table"),
    ({"google": 5}, "google: must be a table"),
    ({"notify__failure_threshold": "three"}, "notify.failure_threshold"),
    ({"accounts__family-icloud__max_concurrency": True}, "accounts.family-icloud.max_concurrency"),
    ({"rules__0__delete_guard": {"max_count": 3.7}}, "rules[family].delete_guard.max_count"),
    ({"rules__0__delete_guard": {"max_count": -1}}, "rules[family].delete_guard.max_count"),
    ({"rules__0__delete_guard": {"max_ratio": 0}}, "rules[family].delete_guard.max_ratio"),
    ({"rules__0__delete_guard": 5}, "rules[family].delete_guard: must be a table"),
    ({"rules__0__window": "7d"}, "rules[family].window: must be a table"),
    (
        {"rules__0__target__create_if_missing": "false"},
        "rules[family].target.create_if_missing: must be true or false",
    ),
    ({"accounts": "x"}, "accounts: must be a table"),
]


@pytest.mark.parametrize(("changes", "expected"), BAD_VALUES)
def test_wrong_types_become_problems(tmp_path: Path, changes: dict[str, object], expected: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_config(_mutated(**changes), base_dir=tmp_path)
    assert expected in "\n".join(info.value.problems)


def test_wrong_types_reported_together(tmp_path: Path) -> None:
    data = _mutated(
        runtime__jitter="x",
        notify__failure_threshold="three",
        rules__0__delete_guard={"max_count": -1},
    )
    with pytest.raises(ConfigError) as info:
        parse_config(data, base_dir=tmp_path)
    text = "\n".join(info.value.problems)
    assert "runtime.jitter" in text
    assert "notify.failure_threshold" in text
    assert "delete_guard.max_count" in text


def test_float_accepted_for_ratio_and_int_for_jitter(tmp_path: Path) -> None:
    data = _mutated(runtime__jitter=0, rules__0__delete_guard={"max_ratio": 1, "max_count": 5.0})
    cfg = parse_config(data, base_dir=tmp_path)
    assert cfg.runtime.jitter == 0.0
    assert cfg.rules[0].delete_guard.max_count == 5


MORE_BAD_VALUES = [
    ({"runtime__jitter": float("nan")}, "runtime.jitter"),
    ({"runtime__jitter": 1.0}, "runtime.jitter"),
    ({"rules__0__delete_guard": {"max_ratio": float("nan")}}, "delete_guard.max_ratio"),
    ({"rules__0__delete_guard": {"max_ratio": float("inf")}}, "delete_guard.max_ratio"),
    ({"rules__0__delete_guard": {"max_ratio": 1.5}}, "delete_guard.max_ratio"),
    ({"notify__failure_threshold": 0}, "notify.failure_threshold"),
    ({"accounts__family-icloud__max_concurrency": 0}, "accounts.family-icloud.max_concurrency"),
    ({"rules": 5}, "rules: must be an array of tables (use [[rules]])"),
    ({"rules": {"name": "x"}}, "rules: must be an array of tables (use [[rules]])"),
    ({"notify": {"bark_url": 5}}, "notify.bark_url"),
    ({"notify": {"webhook_url": ["x"]}}, "notify.webhook_url"),
]


@pytest.mark.parametrize(("changes", "expected"), MORE_BAD_VALUES)
def test_more_malformed_values(tmp_path: Path, changes: dict[str, object], expected: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_config(_mutated(**changes), base_dir=tmp_path)
    assert expected in "\n".join(info.value.problems)


def test_icloud_server_alias_is_accepted(tmp_path: Path) -> None:
    data = {
        "accounts": {
            "i": {
                "provider": "caldav",
                "server_url": "icloud",
                "username": "u@icloud.com",
                "credential": {"backend": "env", "ref": "PW"},
            }
        }
    }
    assert parse_config(data, base_dir=tmp_path).accounts["i"].server_url == "icloud"
    data["accounts"]["i"]["server_url"] = "icloud.com"
    with pytest.raises(ConfigError, match="server_url"):
        parse_config(data, base_dir=tmp_path)


def test_fields_default(tmp_path: Path) -> None:
    assert parse_config(copy.deepcopy(VALID), tmp_path).rules[0].fields == FieldPolicy()


def test_fields_parsed(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["rules"][0]["fields"] = {"attendees": "none", "conference": "none", "content": "busy"}
    assert parse_config(data, tmp_path).rules[0].fields == FieldPolicy("none", "none", "busy")


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"attendees": "invite"}, 'rules[family].fields.attendees: expected one of "none", "notes"'),
        ({"conference": True}, 'rules[family].fields.conference: expected one of "copy", "none"'),
        ({"content": "hidden"}, 'rules[family].fields.content: expected one of "full", "busy"'),
        ({"colour": "red"}, "rules[family].fields.colour: unknown setting"),
        ("busy", "rules[family].fields: must be a table"),
    ],
)
def test_fields_rejected(tmp_path: Path, fields: object, expected: str) -> None:
    data = copy.deepcopy(VALID)
    data["rules"][0]["fields"] = fields
    with pytest.raises(ConfigError) as info:
        parse_config(data, tmp_path)
    assert expected in info.value.problems


def test_bad_fields_reported_with_other_problems_of_the_rule(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["rules"][0]["fields"] = {"content": "hidden"}
    data["rules"][0]["delete_guard"] = {"max_count": -1}
    with pytest.raises(ConfigError) as info:
        parse_config(data, tmp_path)
    text = "\n".join(info.value.problems)
    assert "rules[family].fields.content" in text
    assert "rules[family].delete_guard.max_count" in text


def test_on_target_edit_default_and_rejects_others(tmp_path: Path) -> None:
    assert parse_config(copy.deepcopy(VALID), tmp_path).rules[0].on_target_edit == "revert"
    data = copy.deepcopy(VALID)
    data["rules"][0]["on_target_edit"] = "keep"
    with pytest.raises(ConfigError) as err:
        parse_config(data, tmp_path)
    assert 'rules[family].on_target_edit: only "revert" is supported' in err.value.problems


def test_bad_on_target_edit_reported_with_other_problems_of_the_rule(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["rules"][0]["on_target_edit"] = "keep"
    data["rules"][0]["fields"] = {"content": "hidden"}
    with pytest.raises(ConfigError) as err:
        parse_config(data, tmp_path)
    text = "\n".join(err.value.problems)
    assert "rules[family].on_target_edit" in text and "rules[family].fields.content" in text


def test_paused_and_retire_default_false(tmp_path: Path) -> None:
    rule = parse_config(copy.deepcopy(VALID), tmp_path).rules[0]
    assert (rule.paused, rule.retire) == (False, False)


def test_paused_and_retire_parsed(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["rules"][0]["paused"] = True
    data["rules"][0]["retire"] = True
    rule = parse_config(data, tmp_path).rules[0]
    assert (rule.paused, rule.retire) == (True, True)


@pytest.mark.parametrize("key", ["paused", "retire"])
@pytest.mark.parametrize("value", ["true", 1, 0])
def test_paused_and_retire_must_be_bool(tmp_path: Path, key: str, value: object) -> None:
    data = copy.deepcopy(VALID)
    data["rules"][0][key] = value
    data["rules"][0]["delete_guard"] = {"max_count": -1}
    with pytest.raises(ConfigError) as info:
        parse_config(data, tmp_path)
    assert f"rules[family].{key}: must be true or false" in info.value.problems
    assert any("rules[family].delete_guard.max_count" in p for p in info.value.problems)


def test_public_url(tmp_path: Path) -> None:
    assert parse_config(copy.deepcopy(VALID), tmp_path).notify.public_url is None
    for url in ("https://calsync.example.com", "http://192.0.2.1:8766/"):
        data = copy.deepcopy(VALID)
        data["notify"]["public_url"] = url
        assert parse_config(data, tmp_path).notify.public_url == url


@pytest.mark.parametrize("url", ["calsync.example.com", "ftp://example.com", 5, ""])
def test_public_url_must_be_http(tmp_path: Path, url: object) -> None:
    data = copy.deepcopy(VALID)
    data["notify"]["public_url"] = url
    with pytest.raises(ConfigError) as info:
        parse_config(data, tmp_path)
    assert info.value.problems == ["notify.public_url: must be an http(s) URL"]


# ----- [web] ----------------------------------------------------------------------


def test_web_defaults() -> None:
    cfg = parse_config(VALID, base_dir=Path("/cfg"))
    assert (cfg.web.host, cfg.web.port, cfg.web.allowed_hosts) == ("127.0.0.1", 8766, ())
    assert cfg.web.enabled is True


def test_web_listen_off() -> None:
    data = {**VALID, "web": {"listen": "off", "allowed_hosts": ["nas.lan"]}}
    cfg = parse_config(data, base_dir=Path("/cfg"))
    assert cfg.web.enabled is False and cfg.web.allowed_hosts == ("nas.lan",)


@pytest.mark.parametrize(
    ("listen", "host", "port"),
    [
        ("0.0.0.0:9000", "0.0.0.0", 9000),
        ("[::]:8766", "::", 8766),
        ("[::1]:1", "::1", 1),
        ("Mac-Mini.local:65535", "mac-mini.local", 65535),
    ],
)
def test_web_listen(listen: str, host: str, port: int) -> None:
    data = {**VALID, "web": {"listen": listen, "allowed_hosts": ["Mac-Mini.local", "nas.lan"]}}
    cfg = parse_config(data, base_dir=Path("/cfg"))
    assert (cfg.web.host, cfg.web.port) == (host, port)
    assert cfg.web.allowed_hosts == ("mac-mini.local", "nas.lan")


@pytest.mark.parametrize(
    ("web", "problem"),
    [
        ({"listen": "127.0.0.1"}, "web.listen: expected HOST:PORT"),
        ({"listen": ":8766"}, "web.listen: expected HOST:PORT"),
        ({"listen": "127.0.0.1:0"}, "web.listen: the port must be 1 to 65535"),
        ({"listen": "127.0.0.1:70000"}, "web.listen: the port must be 1 to 65535"),
        ({"listen": "127.0.0.1:http"}, "web.listen: the port must be 1 to 65535"),
        ({"listen": "::1:8766"}, "web.listen: an IPv6 address goes in brackets"),
        ({"listen": "[::1]8766"}, "web.listen: expected HOST:PORT"),
        ({"listen": "[nas.lan]:8766"}, "web.listen: an IPv6 address goes in brackets"),
        ({"listen": "a b:8766"}, "web.listen: expected HOST:PORT"),
        ({"listen": 8766}, "web.listen: expected HOST:PORT"),
        ({"listen": "OFF"}, "web.listen: expected HOST:PORT"),
        ({"listen": False}, 'web.listen: expected HOST:PORT such as "127.0.0.1:8766", or "off"'),
        ({"allowed_hosts": "nas.lan"}, "web.allowed_hosts: expected a list of hostnames"),
        ({"allowed_hosts": [""]}, "web.allowed_hosts: expected a list of hostnames"),
        ({"allowed_hosts": ["nas.lan:8766"]}, "web.allowed_hosts: expected a list of hostnames"),
        ({"allowed_hosts": [1]}, "web.allowed_hosts: expected a list of hostnames"),
        ({"port": 8766}, "web.port: unknown setting"),
    ],
)
def test_web_problems(web: dict[str, object], problem: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_config({**VALID, "web": web}, base_dir=Path("/cfg"))
    assert any(p.startswith(problem) for p in info.value.problems), info.value.problems


def test_web_must_be_a_table() -> None:
    with pytest.raises(ConfigError) as info:
        parse_config({**VALID, "web": "127.0.0.1:8766"}, base_dir=Path("/cfg"))
    assert "web: must be a table" in info.value.problems
