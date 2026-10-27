"""``calsync service``: the launchd plist, install / uninstall / status. ``launchctl`` is always a
fake here (the sealed environment refuses the real one), and every path is under the temporary
``HOME``."""

import io
import json
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from calsync import service
from calsync.cli import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, main
from calsync.service import LABEL, LaunchctlError, Paths

UID = 7331
DOMAIN = f"gui/{UID}"
TARGET = f"{DOMAIN}/{LABEL}"

LIST_HEADER = "PID\tStatus\tLabel\n"

# `launchctl print gui/501/<label>` samples, written after macOS 15's output (abridged).
PRINT_RUNNING = """gui/501/dev.calsync.daemon = {
\tactive count = 1
\tpath = /Users/someone/Library/LaunchAgents/dev.calsync.daemon.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /Users/someone/.local/bin/calsync
\targuments = {
\t\t/Users/someone/.local/bin/calsync
\t\tdaemon
\t}

\tstdout path = /Users/someone/Library/Logs/calsync/daemon.log
\tstderr path = /Users/someone/Library/Logs/calsync/daemon.log
\tinherited environment = {
\t\tSSH_AUTH_SOCK => /private/tmp/com.apple.launchd.x/Listeners
\t}

\tdefault environment = {
\t\tPATH => /usr/bin:/bin:/usr/sbin:/sbin
\t}

\tdomain = gui/501 [100005]
\tminimum runtime = 10
\texit timeout = 120
\truns = 1
\tpid = 4242
\timmediate reason = speculative
\tforks = 0
\texecs = 1
\tinitialized = 1
\ttrampolined = 1
\tstarted suspended = 0
\tproxy started suspended = 0
\tlast exit code = (never exited)

\tspawn type = background (2)
\tjetsam priority = 40
\tproperties = keepalive | runatload | inferred program
}
"""

PRINT_CRASHED = """gui/501/dev.calsync.daemon = {
\tactive count = 0
\tpath = /Users/someone/Library/LaunchAgents/dev.calsync.daemon.plist
\ttype = LaunchAgent
\tstate = not running

\tprogram = /Users/someone/.local/bin/calsync
\targuments = {
\t\t/Users/someone/.local/bin/calsync
\t\tdaemon
\t}

\tdomain = gui/501 [100005]
\tminimum runtime = 10
\texit timeout = 120
\truns = 7
\tlast exit code = 2
\tspawn type = background (2)
\tproperties = keepalive | runatload | inferred program
}
"""

PRINT_OLD_STYLE = """gui/501/dev.calsync.daemon = {
\tstate = waiting
\tlast exit status = 256
}
"""

PRINT_OTHER = """gui/501/com.example.calsync = {
\tactive count = 0
\tpath = /Users/someone/Library/LaunchAgents/com.example.calsync.plist
\tstate = not running

\tprogram = /usr/local/bin/calsync
\targuments = {
\t\t/usr/local/bin/calsync
\t\trun
\t}

\trun interval = 60 seconds
\tlast exit code = 0
}
"""


class FakeLaunchctl:
    """Records every call; ``loaded`` maps a ``domain/label`` to its ``print`` text."""

    def __init__(self, loaded: dict[str, str] | None = None, listed: list[str] | None = None) -> None:
        self.loaded = dict(loaded or {})
        self.listed = list(listed or [])
        self.calls: list[tuple[str, ...]] = []
        self.fail_bootstrap = False

    # `list` comes last: below it, the name means the method, not the type.
    def writes(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[0] in ("bootstrap", "bootout")]

    def bootstrap(self, domain: str, plist: Path) -> None:
        self.calls.append(("bootstrap", domain, str(plist)))
        if self.fail_bootstrap:
            raise LaunchctlError(
                "launchctl bootstrap failed (exit 5): Bootstrap failed: 5: Input/output error"
            )
        self.loaded[f"{domain}/{LABEL}"] = PRINT_RUNNING

    def bootout(self, domain_label: str) -> None:
        self.calls.append(("bootout", domain_label))
        self.loaded.pop(domain_label, None)

    def print(self, domain_label: str) -> str | None:
        self.calls.append(("print", domain_label))
        return self.loaded.get(domain_label)

    def list(self) -> str:
        self.calls.append(("list",))
        labels = self.listed + [key.rsplit("/", 1)[1] for key in self.loaded]
        return LIST_HEADER + "".join(f"-\t0\t{label}\n" for label in dict.fromkeys(labels))


class Machine:
    """The temporary home's LaunchAgents and Logs, a fake ``calsync`` executable on PATH, and the
    fake launchctl the CLI gets."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        self.home = home
        self.paths = Paths.default(home)
        bin_dir = home / "bin"
        bin_dir.mkdir()
        self.exe = bin_dir / "calsync"
        self.exe.write_text("#!/bin/sh\n")
        self.exe.chmod(0o755)
        self.launchctl = FakeLaunchctl()
        monkeypatch.setattr(service, "system_launchctl", lambda: self.launchctl)
        monkeypatch.setattr(
            service.shutil, "which", lambda name: str(self.exe) if name == "calsync" else None
        )
        monkeypatch.setattr(service.os, "getuid", lambda: UID)
        monkeypatch.delenv("LANG", raising=False)

    def run(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = main(list(argv), out=out, stdin=io.StringIO(""))
        return code, out.getvalue()

    def other_plist(self, label: str, arguments: list[str]) -> Path:
        self.paths.agents_dir.mkdir(parents=True, exist_ok=True)
        path = self.paths.agents_dir / f"{label}.plist"
        path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": arguments, "StartInterval": 60}))
        return path


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> Machine:
    return Machine(monkeypatch, Path.home())


def load(path: Path) -> dict[str, object]:
    return plistlib.loads(path.read_bytes())


# --- the plist --------------------------------------------------------------------------


def test_the_plist_round_trips_with_every_key(tmp_path: Path) -> None:
    paths = Paths.default(tmp_path)
    exe = tmp_path / "bin" / "calsync"
    body = service.render_plist(exe, paths)
    assert plistlib.loads(body) == {
        "Label": "dev.calsync.daemon",
        "ProgramArguments": [str(exe), "daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ExitTimeOut": 120,
        "ProcessType": "Background",
        "StandardOutPath": str(tmp_path / "Library" / "Logs" / "calsync" / "daemon.log"),
        "StandardErrorPath": str(tmp_path / "Library" / "Logs" / "calsync" / "daemon.log"),
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"},
    }
    assert paths.plist == tmp_path / "Library" / "LaunchAgents" / "dev.calsync.daemon.plist"
    assert body.startswith(b"<?xml")


def test_the_plist_puts_global_options_before_the_command_and_listen_after(tmp_path: Path) -> None:
    exe = tmp_path / "calsync"
    body = service.render_plist(
        exe,
        Paths.default(tmp_path),
        config=tmp_path / "c.toml",
        state_dir=tmp_path / "state",
        listen="127.0.0.1:8766",
        lang="en_US.UTF-8",
    )
    data = plistlib.loads(body)
    # --config and --state-dir belong to the top-level parser: `calsync daemon --config x` fails.
    assert data["ProgramArguments"] == [
        str(exe),
        "--config",
        str(tmp_path / "c.toml"),
        "--state-dir",
        str(tmp_path / "state"),
        "daemon",
        "--listen",
        "127.0.0.1:8766",
    ]
    assert data["EnvironmentVariables"] == {
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
    }


def test_the_program_arguments_parse_as_a_daemon_command(tmp_path: Path) -> None:
    from calsync.cli import _parser  # pyright: ignore[reportPrivateUsage]

    body = service.render_plist(
        tmp_path / "calsync",
        Paths.default(tmp_path),
        config=tmp_path / "c.toml",
        state_dir=tmp_path / "s",
        listen="[::1]:8766",
    )
    args = _parser().parse_args(plistlib.loads(body)["ProgramArguments"][1:])
    assert args.command == "daemon"
    assert args.config == tmp_path / "c.toml"
    assert args.state_dir == tmp_path / "s"
    assert args.listen == ("::1", 8766)


# --- the executable ---------------------------------------------------------------------


def test_the_executable_is_found_on_path_resolved(tmp_path: Path) -> None:
    real = tmp_path / "tools" / "calsync"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    link = tmp_path / "calsync"
    link.symlink_to(real)
    assert service.find_executable(which=lambda _: str(link), argv0="python") == real.resolve()


def test_the_executable_falls_back_to_argv0_when_it_is_calsync(tmp_path: Path) -> None:
    exe = tmp_path / "calsync"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert service.find_executable(which=lambda _: None, argv0=str(exe)) == exe.resolve()
    other = tmp_path / "pytest"
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    assert service.find_executable(which=lambda _: None, argv0=str(other)) is None


def test_no_executable_when_neither_is_an_existing_file(tmp_path: Path) -> None:
    missing = tmp_path / "calsync"
    assert service.find_executable(which=lambda _: str(missing), argv0=str(missing)) is None
    directory = tmp_path / "d" / "calsync"
    directory.mkdir(parents=True)
    assert service.find_executable(which=lambda _: None, argv0=str(directory)) is None


# --- launchctl output -------------------------------------------------------------------


def test_print_of_a_running_job() -> None:
    state = service.parse_print(PRINT_RUNNING)
    assert (state.state, state.pid, state.last_exit) == ("running", 4242, None)
    assert state.program == "/Users/someone/.local/bin/calsync"
    assert state.arguments == ["/Users/someone/.local/bin/calsync", "daemon"]


def test_print_of_a_crashed_job() -> None:
    state = service.parse_print(PRINT_CRASHED)
    assert (state.state, state.pid, state.last_exit) == ("not running", None, 2)


def test_print_tolerates_last_exit_status_and_junk() -> None:
    state = service.parse_print(PRINT_OLD_STYLE)
    assert (state.state, state.pid, state.last_exit) == ("waiting", None, 256)
    junk = service.parse_print("nonsense\n\tpid = many\n}\n}\n")
    assert (junk.state, junk.pid, junk.last_exit, junk.arguments) == (None, None, None, [])


def test_list_labels() -> None:
    text = LIST_HEADER + "-\t0\tcom.apple.a\n123\t-9\tcom.example.calsync\n\n  bad line\n"
    assert service.parse_list(text) == ["com.apple.a", "com.example.calsync"]


def test_the_real_launchctl_builds_these_commands() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        if argv[1] == "print" and argv[2].endswith("/missing"):
            return subprocess.CompletedProcess(argv, 113, "", 'Could not find service "missing" in domain\n')
        if argv[1] == "bootout":
            return subprocess.CompletedProcess(argv, 3, "", "Boot-out failed: 3: No such process\n")
        return subprocess.CompletedProcess(argv, 0, "out\n", "")

    real = service.SystemLaunchctl(run=runner)
    real.bootstrap(DOMAIN, Path("/x/dev.calsync.daemon.plist"))
    real.bootout(TARGET)  # not loaded: fine
    assert real.print(TARGET) == "out\n"
    assert real.print(f"{DOMAIN}/missing") is None
    assert real.list() == "out\n"
    assert seen == [
        ["launchctl", "bootstrap", DOMAIN, "/x/dev.calsync.daemon.plist"],
        ["launchctl", "bootout", TARGET],
        ["launchctl", "print", TARGET],
        ["launchctl", "print", f"{DOMAIN}/missing"],
        ["launchctl", "list"],
    ]


def test_the_real_launchctl_raises_on_other_failures() -> None:
    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 5, "", "Bootstrap failed: 5: Input/output error\n")

    real = service.SystemLaunchctl(run=runner)
    with pytest.raises(LaunchctlError, match="bootstrap failed"):
        real.bootstrap(DOMAIN, Path("/x.plist"))
    with pytest.raises(LaunchctlError):
        real.bootout(TARGET)
    with pytest.raises(LaunchctlError):
        real.list()


def test_the_real_launchctl_reports_a_missing_or_hanging_launchctl() -> None:
    def missing(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    def hangs(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 10)

    with pytest.raises(LaunchctlError, match="not found"):
        service.SystemLaunchctl(run=missing).list()
    with pytest.raises(LaunchctlError, match="did not answer"):
        service.SystemLaunchctl(run=hangs).list()


# --- install ----------------------------------------------------------------------------


def test_install_writes_the_plist_and_bootstraps_it(machine: Machine) -> None:
    code, out = machine.run("service", "install")
    assert code == EXIT_OK, out
    plist = machine.paths.plist
    assert load(plist)["ProgramArguments"] == [str(machine.exe.resolve()), "daemon"]
    assert plist.stat().st_mode & 0o777 == 0o644
    assert machine.paths.log_dir.is_dir()
    assert machine.paths.log_dir.stat().st_mode & 0o777 == 0o700
    assert machine.launchctl.writes() == [("bootstrap", DOMAIN, str(plist))]
    assert f"launchctl bootstrap {DOMAIN} {plist}" in out
    assert list(machine.paths.agents_dir.iterdir()) == [plist]  # no temp file left behind


def test_install_embeds_absolute_config_state_and_listen(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(machine.home)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    code, out = machine.run(
        "--config", "conf/c.toml", "--state-dir", "~/st", "service", "install", "--listen", "127.0.0.1:9000"
    )
    assert code == EXIT_OK, out
    data = load(machine.paths.plist)
    assert data["ProgramArguments"] == [
        str(machine.exe.resolve()),
        "--config",
        str((machine.home / "conf" / "c.toml").resolve()),
        "--state-dir",
        str((machine.home / "st").resolve()),
        "daemon",
        "--listen",
        "127.0.0.1:9000",
    ]
    assert data["EnvironmentVariables"] == {
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
    }


def test_install_rejects_a_bad_listen(machine: Machine) -> None:
    with pytest.raises(SystemExit) as exc:
        machine.run("service", "install", "--listen", "nonsense")
    assert exc.value.code == 2
    assert machine.launchctl.calls == []


def test_install_again_identical_and_loaded_does_nothing(machine: Machine) -> None:
    assert machine.run("service", "install")[0] == EXIT_OK
    before = machine.paths.plist.stat().st_mtime_ns
    machine.launchctl.calls.clear()
    code, out = machine.run("service", "install", "--json")
    assert code == EXIT_OK
    body = json.loads(out)
    assert body["action"] == "already_installed"
    assert body["commands"] == []
    assert machine.launchctl.writes() == []
    assert machine.paths.plist.stat().st_mtime_ns == before


def test_install_again_identical_but_not_loaded_bootstraps(machine: Machine) -> None:
    assert machine.run("service", "install")[0] == EXIT_OK
    machine.launchctl.loaded.clear()
    machine.launchctl.calls.clear()
    code, out = machine.run("service", "install")
    assert code == EXIT_OK, out
    assert "already installed" in out
    assert machine.launchctl.writes() == [("bootstrap", DOMAIN, str(machine.paths.plist))]


def test_install_refuses_a_differing_plist_without_force(machine: Machine) -> None:
    machine.paths.agents_dir.mkdir(parents=True)
    old = plistlib.dumps({"Label": LABEL, "ProgramArguments": ["/old/calsync", "daemon"]})
    machine.paths.plist.write_bytes(old)
    machine.launchctl.loaded[TARGET] = PRINT_RUNNING
    code, out = machine.run("service", "install")
    assert code == EXIT_CONFIG
    assert "--force" in out
    assert "-" in out and "/old/calsync" in out  # the diff
    assert "+" in out and str(machine.exe.resolve()) in out
    assert machine.paths.plist.read_bytes() == old
    assert machine.launchctl.writes() == []

    code, out = machine.run("service", "install", "--json")
    assert code == EXIT_CONFIG
    error = json.loads(out)["error"]
    assert error["kind"] == "plist_differs"
    assert "/old/calsync" in error["diff"]


def test_install_force_replaces_a_loaded_job_bootout_first(machine: Machine) -> None:
    machine.paths.agents_dir.mkdir(parents=True)
    machine.paths.plist.write_bytes(plistlib.dumps({"Label": LABEL, "ProgramArguments": ["/old/calsync"]}))
    machine.launchctl.loaded[TARGET] = PRINT_RUNNING
    code, out = machine.run("service", "install", "--force", "--json")
    assert code == EXIT_OK, out
    assert json.loads(out)["action"] == "replaced"
    assert machine.launchctl.writes() == [
        ("bootout", TARGET),
        ("bootstrap", DOMAIN, str(machine.paths.plist)),
    ]
    assert load(machine.paths.plist)["ProgramArguments"] == [str(machine.exe.resolve()), "daemon"]


def test_install_warns_about_another_loaded_calsync_job_and_refuses_without_force(machine: Machine) -> None:
    machine.launchctl.loaded[f"{DOMAIN}/com.example.calsync"] = PRINT_OTHER
    machine.launchctl.listed = ["com.apple.unrelated"]
    code, out = machine.run("service", "install")
    assert code == EXIT_CONFIG
    assert "com.example.calsync" in out
    assert "/usr/local/bin/calsync run" in out
    assert "compete" in out
    assert f"launchctl bootout {DOMAIN}/com.example.calsync" in out
    assert "--force" in out
    assert not machine.paths.plist.exists()
    assert machine.launchctl.writes() == []
    # only the calsync label was printed, not every job
    assert ("print", f"{DOMAIN}/com.apple.unrelated") not in machine.launchctl.calls


def test_install_finds_a_job_by_its_program_in_launch_agents(machine: Machine) -> None:
    machine.other_plist("com.example.sync", ["/usr/local/bin/calsync", "run"])
    machine.other_plist("com.example.unrelated", ["/usr/bin/true"])
    machine.launchctl.loaded[f"{DOMAIN}/com.example.sync"] = PRINT_OTHER
    code, out = machine.run("service", "install", "--json")
    assert code == EXIT_CONFIG
    error = json.loads(out)["error"]
    assert error["kind"] == "other_jobs"
    assert error["others"] == [
        {"label": "com.example.sync", "program": "/usr/local/bin/calsync run", "loaded": True}
    ]


def test_an_unloaded_agent_plist_still_counts_it_loads_at_login(machine: Machine) -> None:
    path = machine.other_plist("com.example.sync", ["/usr/local/bin/calsync", "run"])
    code, out = machine.run("service", "install")
    assert code == EXIT_CONFIG
    assert "next login" in out
    assert str(path) in out


def test_install_force_proceeds_with_another_job_and_leaves_it_alone(machine: Machine) -> None:
    machine.launchctl.loaded[f"{DOMAIN}/com.example.calsync"] = PRINT_OTHER
    code, out = machine.run("service", "install", "--force")
    assert code == EXIT_OK, out
    assert "compete" in out
    assert machine.launchctl.writes() == [("bootstrap", DOMAIN, str(machine.paths.plist))]
    assert f"{DOMAIN}/com.example.calsync" in machine.launchctl.loaded


def test_install_refuses_without_an_executable(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    monkeypatch.setattr(service.sys, "argv", ["/nowhere/pytest"])
    code, out = machine.run("service", "install")
    assert code == EXIT_CONFIG
    assert "calsync executable" in out
    assert not machine.paths.plist.exists()
    assert machine.launchctl.calls == []
    code, out = machine.run("service", "install", "--json")
    assert json.loads(out)["error"]["kind"] == "no_executable"


def test_install_dry_run_writes_nothing_and_calls_no_launchctl(machine: Machine) -> None:
    code, out = machine.run("service", "install", "--dry-run")
    assert code == EXIT_OK, out
    assert "<key>Label</key>" in out
    assert f"launchctl bootstrap {DOMAIN} {machine.paths.plist}" in out
    assert machine.launchctl.calls == []
    assert not machine.paths.agents_dir.exists()
    assert not machine.paths.log_dir.exists()

    code, out = machine.run("service", "install", "--dry-run", "--json")
    body = json.loads(out)
    assert body["action"] == "would_install"
    assert body["dry_run"] is True
    assert plistlib.loads(body["plist_xml"].encode())["Label"] == LABEL
    assert machine.launchctl.calls == []


def test_install_dry_run_still_refuses_a_differing_plist(machine: Machine) -> None:
    machine.paths.agents_dir.mkdir(parents=True)
    machine.paths.plist.write_bytes(plistlib.dumps({"Label": LABEL}))
    code, out = machine.run("service", "install", "--dry-run")
    assert code == EXIT_CONFIG
    assert "--force" in out
    assert machine.launchctl.calls == []
    code, out = machine.run("service", "install", "--dry-run", "--force")
    assert code == EXIT_OK
    assert f"launchctl bootout {TARGET}" in out
    assert machine.launchctl.calls == []


def test_install_reports_a_failed_bootstrap(machine: Machine) -> None:
    machine.launchctl.fail_bootstrap = True
    code, out = machine.run("service", "install")
    assert code == EXIT_FAILED
    assert "bootstrap failed" in out
    code, out = machine.run("service", "install", "--json")
    assert code == EXIT_FAILED
    assert json.loads(out)["error"]["kind"] == "launchctl"


# --- uninstall --------------------------------------------------------------------------


def test_uninstall_boots_out_and_removes_only_the_plist(machine: Machine) -> None:
    assert machine.run("service", "install")[0] == EXIT_OK
    machine.paths.log.write_text("a log line\n")
    machine.launchctl.calls.clear()
    code, out = machine.run("service", "uninstall")
    assert code == EXIT_OK, out
    assert machine.launchctl.writes() == [("bootout", TARGET)]
    assert not machine.paths.plist.exists()
    assert machine.paths.log.exists()
    assert "stay" in out


def test_uninstall_when_not_loaded_still_removes_the_plist(machine: Machine) -> None:
    assert machine.run("service", "install")[0] == EXIT_OK
    machine.launchctl.loaded.clear()
    machine.launchctl.calls.clear()
    code, out = machine.run("service", "uninstall", "--json")
    assert code == EXIT_OK, out
    assert json.loads(out)["action"] == "removed"
    assert machine.launchctl.writes() == []
    assert not machine.paths.plist.exists()


def test_uninstall_when_nothing_is_installed(machine: Machine) -> None:
    code, out = machine.run("service", "uninstall", "--json")
    assert code == EXIT_OK
    assert json.loads(out)["action"] == "not_installed"
    assert machine.launchctl.writes() == []


def test_uninstall_dry_run_touches_nothing(machine: Machine) -> None:
    assert machine.run("service", "install")[0] == EXIT_OK
    machine.launchctl.calls.clear()
    code, out = machine.run("service", "uninstall", "--dry-run")
    assert code == EXIT_OK
    assert f"launchctl bootout {TARGET}" in out
    assert machine.paths.plist.exists()
    assert machine.launchctl.calls == []


def test_uninstall_leaves_other_jobs_alone(machine: Machine) -> None:
    path = machine.other_plist("com.example.calsync", ["/usr/local/bin/calsync", "run"])
    machine.launchctl.loaded[f"{DOMAIN}/com.example.calsync"] = PRINT_OTHER
    assert machine.run("service", "install", "--force")[0] == EXIT_OK
    assert machine.run("service", "uninstall")[0] == EXIT_OK
    assert path.exists()
    assert f"{DOMAIN}/com.example.calsync" in machine.launchctl.loaded
    assert ("bootout", f"{DOMAIN}/com.example.calsync") not in machine.launchctl.calls


# --- status -----------------------------------------------------------------------------


def test_status_of_a_running_daemon(machine: Machine) -> None:
    assert machine.run("service", "install")[0] == EXIT_OK
    code, out = machine.run("service", "status", "--json")
    assert code == EXIT_OK
    assert json.loads(out) == {
        "installed": True,
        "loaded": True,
        "state": "running",
        "pid": 4242,
        "last_exit": None,
        "plist": str(machine.paths.plist),
        "log": str(machine.paths.log),
        "others": [],
    }
    code, out = machine.run("service", "status")
    assert "loaded: yes" in out and "4242" in out


def test_status_of_a_crashed_daemon_and_another_job(machine: Machine) -> None:
    assert machine.run("service", "install")[0] == EXIT_OK
    machine.launchctl.loaded[TARGET] = PRINT_CRASHED
    machine.launchctl.loaded[f"{DOMAIN}/com.example.calsync"] = PRINT_OTHER
    code, out = machine.run("service", "status", "--json")
    assert code == EXIT_OK
    body = json.loads(out)
    assert (body["loaded"], body["pid"], body["last_exit"], body["state"]) == (True, None, 2, "not running")
    assert body["others"] == [
        {"label": "com.example.calsync", "program": "/usr/local/bin/calsync run", "loaded": True}
    ]
    code, out = machine.run("service", "status")
    assert "last exit: 2" in out
    assert "com.example.calsync" in out


def test_status_when_not_installed(machine: Machine) -> None:
    code, out = machine.run("service", "status", "--json")
    assert code == EXIT_OK
    body = json.loads(out)
    assert (body["installed"], body["loaded"], body["pid"], body["last_exit"]) == (False, False, None, None)
    code, out = machine.run("service", "status")
    assert "installed: no" in out
    assert machine.launchctl.writes() == []


def test_status_is_read_only(machine: Machine) -> None:
    machine.run("service", "status")
    assert not machine.paths.agents_dir.exists()
    assert not machine.paths.log_dir.exists()
    assert {call[0] for call in machine.launchctl.calls} <= {"print", "list"}


def test_paths_follow_home() -> None:
    paths = Paths.default()
    assert paths.agents_dir == Path(os.environ["HOME"]) / "Library" / "LaunchAgents"
    assert paths.log == Path(os.environ["HOME"]) / "Library" / "Logs" / "calsync" / "daemon.log"
