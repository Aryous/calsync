"""The CLI's write commands and ``calsync.opsclient``: in-process (with the run lock) and
through a daemon's socket."""

import asyncio
import contextlib
import io
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio.to_thread
import pytest

from calsync import api, configfile, opsclient
from calsync.cli import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, EXIT_RUNNING, main
from calsync.configfile import ConfigConflict, Snapshot
from calsync.daemon import DaemonInfo
from calsync.errors import ConfigError
from calsync.lock import RunLock
from calsync.ops import OpResult, OpsUnavailable
from calsync.views import StateReader
from calsync.wiring import socket_path
from tests.harness import NOW, config_for, rule
from tests.test_ops import TEXT, FakeKeyring

NOTIFY = '\n[notify]\nbark_url = "https://bark.example.com/DEVICEKEY"\npublic_url = "https://calsync.example.com/x"\n'


def run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, out=out, stdin=io.StringIO(""))
    return code, out.getvalue()


class Files:
    def __init__(self, root: Path) -> None:
        self.config = root / "config.toml"
        self.config.write_text(TEXT)
        self.config.chmod(0o600)
        self.state = root / "s"

    def cli(self, *argv: str) -> tuple[int, str]:
        return run(["--config", str(self.config), "--state-dir", str(self.state), *argv])

    def history(self) -> list[tuple[str, str, int | None]]:
        with StateReader(self.state / "state.db") as reader:
            return [(c.source, c.summary, c.undoes) for c in reader.config_changes()]


@pytest.fixture
def files(short_dir: Path) -> Files:
    return Files(short_dir)


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    from calsync import credentials

    fake = FakeKeyring()
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)
    return fake


# ----- the write commands, in-process ----------------------------------------------------


def test_pause_and_resume(files: Files) -> None:
    assert files.cli("pause", "family") == (EXIT_OK, 'rule "family" paused\n')
    assert files.cli("pause", "family") == (EXIT_OK, 'rule "family" is already paused\n')
    assert files.cli("resume", "family") == (EXIT_OK, 'rule "family" resumed\n')
    assert files.config.read_text() == TEXT
    assert files.history() == [("cli", 'rule "family" resumed', None), ("cli", 'rule "family" paused', None)]


def test_json_output(files: Files) -> None:
    code, out = files.cli("pause", "family", "--json")
    body = json.loads(out)
    assert code == EXIT_OK
    assert list(body) == ["op", "changed", "summary", "diff", "data", "needs_human", "command", "reason"]
    assert (body["op"], body["changed"], body["needs_human"]) == ("rule.pause", True, False)
    assert "+paused = true" in body["diff"]


def test_dry_run_prints_the_diff_and_writes_nothing(files: Files) -> None:
    code, out = files.cli("rule", "set", "family", "window", "all", "--dry-run")
    assert code == EXIT_OK
    assert out.startswith(
        'dry run, nothing written: rule "family": window set\n--- config.toml\n+++ config.toml\n'
    )
    assert '-past = "7d"\n' in out
    assert files.config.read_text() == TEXT
    assert files.history() == []


def test_every_write_command(files: Files, keychain: FakeKeyring) -> None:
    commands: list[tuple[list[str], str]] = [
        (
            [
                "rule",
                "add",
                "kids",
                "--source",
                "me-google:Kids",
                "--target",
                "family-icloud:Kids",
                "--create-target",
                "--window",
                "30d:365d",
                "--alarms=-10m,0m",
                "--content",
                "busy",
                "--attendees",
                "none",
                "--conference",
                "none",
            ],
            'rule "kids" added: "me-google:Kids" to "family-icloud:Kids"\n',
        ),
        (["rule", "set", "kids", "alarms", "strip"], 'rule "kids": alarms set\n'),
        (["rule", "remove", "kids"], 'rule "kids" removed; its copies stay in "Kids"\n'),
        (
            ["rule", "remove", "work", "--delete-copies"],
            'rule "work" retires: its copies in "Work" are deleted, then the rule is removed\n',
        ),
        (["config", "set", "runtime.interval", "2m"], "runtime.interval set\n"),
        (
            [
                "account",
                "add",
                "kid-dav",
                "--provider",
                "caldav",
                "--server-url",
                "icloud",
                "--username",
                "kid",
            ],
            'account "kid-dav" added\nnext: calsync auth kid-dav  '
            "(a person signs in (Google) or types the app-specific password (iCloud))\n",
        ),
        (["account", "remove", "kid-dav"], 'account "kid-dav" removed; its credential was kept\n'),
        (
            ["account", "remove", "spare-dav", "--delete-credential"],
            'account "spare-dav" removed; its keychain entry was deleted\n',
        ),
        (["config", "undo"], 'undid change 8 (account "spare-dav" removed)\n'),
    ]
    for argv, expected in commands:
        assert files.cli(*argv) == (EXIT_OK, expected), argv
    config = configfile.validate_text(files.config.read_text(), files.config.parent)
    assert [r.name for r in config.rules] == ["family", "work"] and config.rules[1].retire
    assert "spare-dav" in config.accounts and "kid-dav" not in config.accounts
    assert len(files.history()) == len(commands)


def test_a_validation_error_exits_2_with_the_problems(files: Files) -> None:
    assert files.cli("rule", "set", "family", "paused", "yes") == (
        EXIT_CONFIG,
        'configuration error:\n  - rule.set: paused: expected true or false, got "yes"\n',
    )
    assert files.cli("account", "remove", "family-icloud") == (
        EXIT_CONFIG,
        'configuration error:\n  - account "family-icloud" is used by rules "family", "work"; remove them first\n',
    )
    code, out = files.cli(
        "rule", "add", "x", "--source", "ghost:Kids", "--target", "family-icloud:Kids", "--json"
    )
    assert code == EXIT_CONFIG
    assert json.loads(out) == {
        "error": {
            "kind": "config",
            "message": "rules[x].source.account: unknown account 'ghost'",
            "problems": ["rules[x].source.account: unknown account 'ghost'"],
        }
    }
    assert files.config.read_text() == TEXT


def test_a_conflict_exits_2(files: Files, monkeypatch: pytest.MonkeyPatch) -> None:
    real = configfile.write_config

    def edited_meanwhile(snapshot: Snapshot, text: str) -> Snapshot:
        files.config.write_text(files.config.read_text() + "# edited by hand\n")
        return real(snapshot, text)

    monkeypatch.setattr(configfile, "write_config", edited_meanwhile)
    assert files.cli("pause", "family") == (
        EXIT_CONFIG,
        "configuration error:\n  - the file was changed outside calsync; reload and try again\n",
    )
    code, out = files.cli("pause", "family", "--json")
    assert code == EXIT_CONFIG and json.loads(out)["error"]["kind"] == "conflict"


def test_the_in_process_path_exits_4_while_the_lock_is_held(files: Files) -> None:
    import os

    with RunLock(files.state / "run.lock"):
        code, out = files.cli("pause", "family")
    assert code == EXIT_RUNNING
    assert (
        out == f"another calsync process (pid {os.getpid()}) holds the lock; try again once it has finished\n"
    )
    assert files.config.read_text() == TEXT


def test_config_get_masks_urls_and_identities(files: Files) -> None:
    files.config.write_text(TEXT + NOTIFY)
    code, out = files.cli("config", "get")
    body = json.loads(out)
    assert code == EXIT_OK and body["revealed"] is False
    assert body["notify"]["bark_url"] == "https://bark.example.com"
    assert body["notify"]["public_url"] == "https://calsync.example.com"
    family = body["accounts"]["family-icloud"]
    assert family == {
        "provider": "caldav",
        "host": "iCloud",
        "server_url": "icloud",
        "username": "(hidden)",
        "credential": {"backend": "env", "ref": "(hidden)"},
        "max_concurrency": 2,
    }
    assert "DEVICEKEY" not in out and "family@example.com" not in out and "FAMILY_PW" not in out
    assert body["rules"][0] == {
        "name": "family",
        "source": {"account": "me-google", "calendar": "Family"},
        "target": {"account": "family-icloud", "calendar": "Home", "create_if_missing": False},
        "window": {"past": "7d", "future": "180d"},
        "alarms": "copy",
        "fields": {"content": "full", "attendees": "notes", "conference": "copy"},
        "delete_guard": {"max_count": 10, "max_ratio": 0.5},
        "paused": False,
        "retire": False,
    }
    assert body["runtime"] == {"interval": "1m", "jitter": 0.1}
    assert body["web"] == {"listen": "127.0.0.1:8766", "allowed_hosts": []}
    revealed = json.loads(files.cli("config", "get", "--reveal")[1])
    assert revealed["revealed"] is True
    assert revealed["notify"]["bark_url"] == "https://bark.example.com/DEVICEKEY"
    assert revealed["accounts"]["family-icloud"]["username"] == "family@example.com"
    assert revealed["accounts"]["family-icloud"]["credential"]["ref"] == "FAMILY_PW"


def test_config_get_without_a_config(short_dir: Path) -> None:
    code, out = run(["--config", str(short_dir / "none.toml"), "config", "get"])
    assert code == EXIT_CONFIG and "config file not found" in out


def test_config_history(files: Files) -> None:
    assert files.cli("config", "history") == (EXIT_OK, "no config changes recorded\n")
    assert json.loads(files.cli("config", "history", "--json")[1]) == {"changes": []}
    files.cli("pause", "family")
    files.cli("config", "undo")
    code, out = files.cli("config", "history")
    assert code == EXIT_OK
    assert out == (
        f'2 {_at(files, 2)} cli undid change 1 (rule "family" paused) (undoes 1)\n'
        f'1 {_at(files, 1)} cli rule "family" paused\n'
    )
    body = json.loads(files.cli("config", "history", "--json", "--limit", "1")[1])
    [change] = body["changes"]
    assert list(change) == ["id", "at", "source", "summary", "after_fingerprint", "undoes"]  # never `before`
    assert (change["id"], change["undoes"]) == (2, 1)


def _at(files: Files, change_id: int) -> str:
    with StateReader(files.state / "state.db") as reader:
        return next(c.at for c in reader.config_changes() if c.id == change_id).isoformat()


# ----- through a daemon's socket ---------------------------------------------------------


class FakeOpsDaemon:
    """A daemon that answers ops with ``answer`` (or raises it) and records each call."""

    def __init__(self, answer: OpResult | Exception) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any], bool, str]] = []

    def request_cycle(self, rule: str | None = None) -> bool:
        return True

    def snapshot(self) -> DaemonInfo:
        return DaemonInfo(True, True, 4242, NOW, None, None, 0)

    async def run_op(self, name: str, args: dict[str, Any], dry_run: bool, *, source: str) -> OpResult:
        self.calls.append((name, args, dry_run, source))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@contextlib.asynccontextmanager
async def _serve(state_dir: Path, daemon: Any) -> AsyncIterator[None]:
    app = api.create_app(
        config_provider=lambda: config_for([rule()]), state_dir=state_dir, daemon=daemon, clock=lambda: NOW
    )
    stop = asyncio.Event()
    with api.unix_socket(socket_path(state_dir)) as sock:
        task = asyncio.create_task(api.serve(app, sock, stop))
        try:
            yield
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)


def _call(files: Files, name: str, args: dict[str, Any], dry_run: bool = False) -> OpResult:
    return opsclient.call(name, args, dry_run=dry_run, config_path=files.config, state_dir=files.state)


ANSWER = OpResult("rule.pause", True, 'rule "family" paused', diff="d", data={"x": 1})


@pytest.mark.anyio
async def test_call_goes_through_the_socket_when_a_daemon_serves_it(files: Files) -> None:
    files.state.mkdir()
    daemon = FakeOpsDaemon(ANSWER)
    async with _serve(files.state, daemon):
        result = await anyio.to_thread.run_sync(lambda: _call(files, "rule.pause", {"name": "family"}, True))
        code, out = await anyio.to_thread.run_sync(lambda: files.cli("pause", "family"))
    assert result == ANSWER
    assert (code, out) == (EXIT_OK, 'rule "family" paused\n')
    assert daemon.calls == [
        ("rule.pause", {"name": "family"}, True, "cli"),
        ("rule.pause", {"name": "family"}, False, "cli"),
    ]
    assert files.config.read_text() == TEXT  # the daemon ran it, not this process
    assert not (files.state / "state.db").exists()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (ConfigError(["one", "two"]), ConfigError(["one", "two"])),
        (ConfigConflict(), ConfigConflict()),
        (OpsUnavailable("The daemon runs with --no-sync: it changes no configuration."), None),
    ],
)
async def test_daemon_errors_come_back_as_errors(files: Files, answer: Exception, expected: Any) -> None:
    files.state.mkdir()
    async with _serve(files.state, FakeOpsDaemon(answer)):
        with pytest.raises(type(answer)) as info:
            await anyio.to_thread.run_sync(lambda: _call(files, "rule.pause", {"name": "family"}))
        code, out = await anyio.to_thread.run_sync(lambda: files.cli("pause", "family"))
    if expected is not None:
        assert info.value.problems == expected.problems  # type: ignore[attr-defined]
        assert code == EXIT_CONFIG
    else:
        assert str(info.value) == "The daemon runs with --no-sync: it changes no configuration."
        assert (code, out) == (
            EXIT_FAILED,
            "error: The daemon runs with --no-sync: it changes no configuration.\n",
        )


@pytest.mark.anyio
async def test_an_unknown_op_over_the_socket(files: Files) -> None:
    files.state.mkdir()
    daemon = FakeOpsDaemon(ANSWER)
    async with _serve(files.state, daemon):
        with pytest.raises(ConfigError) as info:
            await anyio.to_thread.run_sync(lambda: _call(files, "rule.explode", {}))
    assert info.value.problems == ['unknown operation "rule.explode"']
    assert daemon.calls == []


def test_call_runs_in_process_without_a_daemon(files: Files, short_dir: Path) -> None:
    import socket

    result = _call(files, "rule.pause", {"name": "family"})
    assert result.changed and result.summary == 'rule "family" paused'
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # a socket file nobody listens on
    dead.bind(str(socket_path(files.state)))
    dead.close()
    assert _call(files, "rule.resume", {"name": "family"}).summary == 'rule "family" resumed'
    assert files.history() == [("cli", 'rule "family" resumed', None), ("cli", 'rule "family" paused', None)]


def test_a_socket_it_cannot_reach_is_an_error(files: Files) -> None:
    import socket

    files.state.mkdir()
    listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listening.bind(str(socket_path(files.state)))
        listening.listen(1)
        socket_path(files.state).chmod(0)  # connect needs write permission on the socket
        assert files.cli("pause", "family") == (
            EXIT_FAILED,
            "error: cannot connect to the daemon socket (PermissionError)\n",
        )
    finally:
        listening.close()
    assert files.config.read_text() == TEXT


# ----- fix round 1 -----------------------------------------------------------------------


def test_config_get_never_prints_a_url_login(files: Files) -> None:
    files.config.write_text(
        TEXT + '\n[notify]\nwebhook_url = "https://user:tok3n@hooks.example.com:8443/x"\n'
    )
    code, out = files.cli("config", "get")
    assert code == EXIT_OK
    assert json.loads(out)["notify"]["webhook_url"] == "https://hooks.example.com:8443"
    assert "user" not in out.replace("username", "") and "tok3n" not in out


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("https://user:tok3n@hooks.example.com:8443/x", "https://hooks.example.com:8443"),
        ("https://user@[2001:db8::1]:8443/key", "https://[2001:db8::1]:8443"),
        ("http://[2001:db8::1]/key", "http://[2001:db8::1]"),
        ("https://bark.example.com/DEVICEKEY", "https://bark.example.com"),
        ("https://hooks.example.com:notaport/x", "(hidden)"),
        ("not a url", "(hidden)"),
    ],
)
def test_scheme_host(url: str, shown: str) -> None:
    from calsync.views import _scheme_host  # pyright: ignore[reportPrivateUsage]

    assert _scheme_host(url) == shown


def test_a_fresh_install_can_set_the_google_client_secret_file(short_dir: Path) -> None:
    config, state = short_dir / "new" / "config.toml", short_dir / "s"

    def cli(*argv: str) -> tuple[int, str]:
        return run(["--config", str(config), "--state-dir", str(state), *argv])

    code, out = cli("account", "add", "me-google", "--provider", "google")
    assert code == EXIT_CONFIG and "google.client_secret_file: required" in out
    assert cli("config", "set", "google.client_secret_file", "~/Downloads/client.json") == (
        EXIT_OK,
        "google.client_secret_file set\n",
    )
    assert f'client_secret_file = "{Path.home()}/Downloads/client.json"' in config.read_text()
    code, out = cli("account", "add", "me-google", "--provider", "google")
    assert code == EXIT_OK and out.startswith('account "me-google" added\n'), out


def test_an_empty_client_secret_file_removes_the_key(short_dir: Path) -> None:
    config, state = short_dir / "c" / "config.toml", short_dir / "s"

    def cli(*argv: str) -> tuple[int, str]:
        return run(["--config", str(config), "--state-dir", str(state), *argv])

    assert cli("config", "set", "google.client_secret_file", "/x/client.json")[0] == EXIT_OK
    assert cli("config", "set", "google.client_secret_file", "")[0] == EXIT_OK
    assert "client_secret_file" not in config.read_text()


class SilentDaemon:
    """A socket that takes connections and answers none: ``close`` hangs up on each one,
    otherwise each is held open (the daemon is stuck)."""

    def __init__(self, path: Path, *, close: bool) -> None:
        import socket
        import threading

        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(4)
        self.server.settimeout(0.05)
        self.close = close
        self.held: list[Any] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except TimeoutError:
                continue
            if self.close:
                conn.recv(65536)  # the request (the probe sends nothing and closes)
                conn.close()
            else:
                self.held.append(conn)

    def __enter__(self) -> "SilentDaemon":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        self.thread.join(5)
        for conn in self.held:
            conn.close()
        self.server.close()


LOST = (
    "lost contact with the daemon; the operation may still be applied — "
    "check `calsync config history` before retrying"
)


@pytest.mark.parametrize("close", [True, False], ids=["hung-up", "timed-out"])
def test_losing_the_daemon_mid_op_is_a_clear_error(
    files: Files, monkeypatch: pytest.MonkeyPatch, close: bool
) -> None:
    files.state.mkdir()
    monkeypatch.setattr(opsclient, "OP_TIMEOUT", 0.3)
    with SilentDaemon(socket_path(files.state), close=close):
        with pytest.raises(opsclient.DaemonLost) as info:
            _call(files, "rule.pause", {"name": "family"})
        assert str(info.value) == LOST
        assert files.cli("pause", "family") == (EXIT_FAILED, f"error: {LOST}\n")
        code, out = files.cli("pause", "family", "--json")
    assert code == EXIT_FAILED
    assert json.loads(out) == {"error": {"kind": "daemon_lost", "message": LOST}}
    assert files.config.read_text() == TEXT  # not run here as well


class PausedDaemon(FakeOpsDaemon):
    def request_cycle(self, rule: str | None = None) -> bool:
        from calsync.errors import RulePaused

        if rule == "family":
            raise RulePaused("family")
        return True


@pytest.mark.anyio
async def test_trigger_for_a_paused_rule_says_so_and_exits_1(files: Files) -> None:
    files.state.mkdir()
    async with _serve(files.state, PausedDaemon(ANSWER)):
        one = await anyio.to_thread.run_sync(lambda: files.cli("trigger", "--rule", "family"))
        every = await anyio.to_thread.run_sync(lambda: files.cli("trigger"))
    assert one == (EXIT_FAILED, 'rule "family" is paused\n')
    assert every == (EXIT_OK, "ok: cycle requested\n")
