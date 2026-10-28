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
from calsync.cli import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, EXIT_RUNNING, main
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
    """Records every call; ``loaded`` maps a ``domain/label`` to its ``print`` text. After a
    ``bootout`` the job stays listed for ``stopping_polls`` more ``print`` calls (``None``: it never
    goes away), as launchd keeps it while the daemon finishes its cycle."""

    def __init__(self, loaded: dict[str, str] | None = None, listed: list[str] | None = None) -> None:
        self.loaded = dict(loaded or {})
        self.listed = list(listed or [])
        self.calls: list[tuple[str, ...]] = []
        self.fail_bootstrap: str | None = None
        self.fail_list = False
        self.stopping_polls: int | None = 0
        self._stopping: dict[str, int | None] = {}

    # `list` comes last: below it, the name means the method, not the type.
    def writes(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[0] in ("bootstrap", "bootout")]

    def bootstrap(self, domain: str, plist: Path) -> None:
        self.calls.append(("bootstrap", domain, str(plist)))
        if self.fail_bootstrap is not None:
            raise LaunchctlError(self.fail_bootstrap)
        self.loaded[f"{domain}/{LABEL}"] = PRINT_RUNNING

    def bootout(self, domain_label: str) -> None:
        self.calls.append(("bootout", domain_label))
        if self.stopping_polls == 0:
            self.loaded.pop(domain_label, None)
        elif domain_label in self.loaded:
            self._stopping[domain_label] = self.stopping_polls

    def print(self, domain_label: str) -> str | None:
        self.calls.append(("print", domain_label))
        if domain_label in self._stopping:
            left = self._stopping[domain_label]
            if left is not None:
                if left == 0:
                    del self._stopping[domain_label]
                    self.loaded.pop(domain_label, None)
                else:
                    self._stopping[domain_label] = left - 1
        return self.loaded.get(domain_label)

    def list(self) -> str:
        self.calls.append(("list",))
        if self.fail_list:
            raise LaunchctlError("launchctl list failed (exit 1): Operation not permitted")
        labels = self.listed + [key.rsplit("/", 1)[1] for key in self.loaded]
        return LIST_HEADER + "".join(f"-\t0\t{label}\n" for label in dict.fromkeys(labels))


class FakeClock:
    """``service._clock`` and ``service._sleep``: sleeping advances the clock, nobody waits."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


class Machine:
    """The temporary home's LaunchAgents and Logs, a fake ``calsync`` executable on PATH (the
    running program is pytest, and no calsync sits next to the running Python), the fake
    launchctl the CLI gets, and a fake clock. ``$CALSYNC_CONFIG`` / ``$CALSYNC_STATE_DIR`` (set by
    the seal) are unset: a test that wants them sets them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        self.home = home
        self.paths = Paths.default(home)
        self.exe = executable(home / "bin" / "calsync")
        self.launchctl = FakeLaunchctl()
        self.time = FakeClock()
        monkeypatch.setattr(service, "system_launchctl", lambda: self.launchctl)
        monkeypatch.setattr(
            service.shutil, "which", lambda name: str(self.exe) if name == "calsync" else None
        )
        monkeypatch.setattr(service.sys, "argv", [str(home / "venv" / "bin" / "pytest")])
        monkeypatch.setattr(service.sys, "executable", str(home / "python" / "bin" / "python3"))
        monkeypatch.setattr(service.os, "getuid", lambda: UID)
        monkeypatch.setattr(service, "_clock", self.time.clock)
        monkeypatch.setattr(service, "_sleep", self.time.sleep)
        monkeypatch.delenv("LANG", raising=False)
        monkeypatch.delenv("CALSYNC_CONFIG", raising=False)
        monkeypatch.delenv("CALSYNC_STATE_DIR", raising=False)

    def run(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = main(list(argv), out=out, stdin=io.StringIO(""))
        return code, out.getvalue()

    def other_plist(self, label: str, arguments: list[str], **extra: object) -> Path:
        self.paths.agents_dir.mkdir(parents=True, exist_ok=True)
        path = self.paths.agents_dir / f"{label}.plist"
        body = {"Label": label, "ProgramArguments": arguments, "StartInterval": 60, **extra}
        path.write_bytes(plistlib.dumps(body))
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


NO_PYTHON = "/nowhere/bin/python3"


def test_the_executable_is_the_running_calsync_first(tmp_path: Path) -> None:
    running = executable(tmp_path / "tools" / "calsync")
    beside = executable(tmp_path / "py" / "bin" / "calsync")
    on_path = executable(tmp_path / "path" / "calsync")
    found = service.find_executable(
        which=lambda _: str(on_path), argv0=str(running), python=str(tmp_path / "py" / "bin" / "python3")
    )
    assert found == running.resolve()
    assert beside.exists()


def test_the_running_calsync_is_resolved(tmp_path: Path) -> None:
    real = executable(tmp_path / "tools" / "calsync")
    link = tmp_path / "bin" / "calsync"
    link.parent.mkdir()
    link.symlink_to(real)
    assert service.find_executable(which=lambda _: None, argv0=str(link), python=NO_PYTHON) == real.resolve()


def test_python_m_calsync_finds_the_calsync_next_to_its_python(tmp_path: Path) -> None:
    # `python -m calsync`: argv[0] is .../calsync/__main__.py. The venv's python is a symlink
    # to the base interpreter: the calsync beside the symlink counts, not beside its target.
    base = executable(tmp_path / "base" / "bin" / "python3")
    venv_bin = tmp_path / "env" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").symlink_to(base)
    beside = executable(venv_bin / "calsync")
    on_path = executable(tmp_path / "path" / "calsync")
    found = service.find_executable(
        which=lambda _: str(on_path),
        argv0=str(tmp_path / "src" / "calsync" / "__main__.py"),
        python=str(venv_bin / "python3"),
    )
    assert found == beside.resolve()


def test_the_executable_falls_back_to_path_resolved(tmp_path: Path) -> None:
    real = executable(tmp_path / "tools" / "calsync")
    link = tmp_path / "calsync"
    link.symlink_to(real)
    other = executable(tmp_path / "pytest")
    found = service.find_executable(which=lambda _: str(link), argv0=str(other), python=NO_PYTHON)
    assert found == real.resolve()


def test_a_missing_running_calsync_falls_through(tmp_path: Path) -> None:
    on_path = executable(tmp_path / "path" / "calsync")
    found = service.find_executable(
        which=lambda _: str(on_path), argv0=str(tmp_path / "gone" / "calsync"), python=NO_PYTHON
    )
    assert found == on_path.resolve()


def test_no_executable_when_none_is_an_existing_file(tmp_path: Path) -> None:
    missing = tmp_path / "calsync"
    assert service.find_executable(which=lambda _: str(missing), argv0=str(missing), python=NO_PYTHON) is None
    directory = tmp_path / "d" / "calsync"
    directory.mkdir(parents=True)
    assert service.find_executable(which=lambda _: None, argv0=str(directory), python=NO_PYTHON) is None
    assert service.find_executable(which=lambda _: None, argv0="pytest", python=NO_PYTHON) is None


def test_a_checkout_virtualenv_is_warned_about(tmp_path: Path) -> None:
    project = tmp_path / "work" / "calsync"
    (project / ".git").mkdir(parents=True)
    exe = project / ".venv" / "bin" / "calsync"
    warning = service.checkout_warning(exe)
    assert warning is not None
    assert str(project) in warning
    assert "stop working if this checkout is removed" in warning
    assert "uv tool install" in warning
    # a git worktree: .git is a file
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere\n")
    assert service.checkout_warning(worktree / ".venv" / "bin" / "calsync") is not None


def test_a_tool_or_plain_virtualenv_is_not_warned_about(tmp_path: Path) -> None:
    assert service.checkout_warning(tmp_path / "uv" / "tools" / "calsync" / "bin" / "calsync") is None
    (tmp_path / "proj").mkdir()
    assert service.checkout_warning(tmp_path / "proj" / ".venv" / "bin" / "calsync") is None


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
    timeouts: list[float] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        timeouts.append(timeout)
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
    # bootout waits for the daemon to finish its cycle (ExitTimeOut) and then some
    assert timeouts == [10, service.EXIT_TIMEOUT + 30, 10, 10, 10]


def test_the_real_launchctl_raises_on_other_failures() -> None:
    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 5, "", "Bootstrap failed: 5: Input/output error\n")

    real = service.SystemLaunchctl(run=runner)
    with pytest.raises(LaunchctlError, match="bootstrap failed"):
        real.bootstrap(DOMAIN, Path("/x.plist"))
    with pytest.raises(LaunchctlError):
        real.bootout(TARGET)
    with pytest.raises(LaunchctlError):
        real.list()


def test_the_real_launchctl_reports_a_missing_or_hanging_launchctl() -> None:
    def missing(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    def hangs(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, timeout)

    with pytest.raises(LaunchctlError, match="not found"):
        service.SystemLaunchctl(run=missing).list()
    with pytest.raises(service.LaunchctlTimeout, match="did not answer within 10 s"):
        service.SystemLaunchctl(run=hangs).list()
    with pytest.raises(service.LaunchctlTimeout, match="did not answer within 150 s"):
        service.SystemLaunchctl(run=hangs).bootout(TARGET)


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
    assert code == EXIT_RUNNING
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
    assert code == EXIT_RUNNING
    error = json.loads(out)["error"]
    assert error["kind"] == "other_jobs"
    assert error["others"] == [
        {"label": "com.example.sync", "program": "/usr/local/bin/calsync run", "loaded": True}
    ]


def test_an_unloaded_agent_plist_still_counts_it_loads_at_login(machine: Machine) -> None:
    path = machine.other_plist("com.example.sync", ["/usr/local/bin/calsync", "run"])
    code, out = machine.run("service", "install")
    assert code == EXIT_RUNNING
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
    machine.launchctl.fail_bootstrap = (
        "launchctl bootstrap failed (exit 5): Bootstrap failed: 5: Input/output error"
    )
    code, out = machine.run("service", "install")
    assert code == EXIT_FAILED
    assert "bootstrap failed" in out
    assert "desktop session" not in out
    code, out = machine.run("service", "install", "--json")
    assert code == EXIT_FAILED
    error = json.loads(out)["error"]
    assert error["kind"] == "launchctl"
    assert "hint" not in error


@pytest.mark.parametrize(
    "stderr",
    [
        "Bootstrap failed: 125: Domain does not support specified action",
        "Domain does not support specified action",
    ],
)
def test_a_bootstrap_without_a_gui_session_says_where_to_run_it(machine: Machine, stderr: str) -> None:
    machine.launchctl.fail_bootstrap = f"launchctl bootstrap failed (exit 125): {stderr}"
    code, out = machine.run("service", "install")
    assert code == EXIT_FAILED
    assert "run this from a logged-in desktop session (not over SSH)" in out
    code, out = machine.run("service", "install", "--json")
    assert code == EXIT_FAILED
    assert "desktop session" in json.loads(out)["error"]["hint"]


def test_install_prints_the_program_it_pins(machine: Machine) -> None:
    code, out = machine.run("service", "install")
    assert code == EXIT_OK, out
    assert f"program: {machine.exe.resolve()}\n" in out
    body = json.loads(machine.run("service", "install", "--json")[1])
    assert body["program"] == [str(machine.exe.resolve()), "daemon"]
    assert body["warnings"] == []


def test_install_pins_the_running_calsync_over_the_one_on_path(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = executable(machine.home / "tools" / "calsync")
    monkeypatch.setattr(service.sys, "argv", [str(running)])
    code, out = machine.run("service", "install")
    assert code == EXIT_OK, out
    assert load(machine.paths.plist)["ProgramArguments"] == [str(running.resolve()), "daemon"]
    assert f"program: {running.resolve()}" in out


def test_install_from_a_checkout_virtualenv_warns(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    project = machine.home / "code" / "calsync"
    (project / ".git").mkdir(parents=True)
    exe = executable(project / ".venv" / "bin" / "calsync")
    monkeypatch.setattr(service.sys, "argv", [str(exe)])
    code, out = machine.run("service", "install")
    assert code == EXIT_OK, out
    assert "warning:" in out
    assert "the daemon will stop working if this checkout is removed" in out
    assert "uv tool install" in out
    body = json.loads(machine.run("service", "install", "--json")[1])
    assert len(body["warnings"]) == 1
    assert "stop working if this checkout is removed" in body["warnings"][0]
    dry = json.loads(machine.run("service", "install", "--dry-run", "--json")[1])
    assert len(dry["warnings"]) == 1


def test_install_embeds_calsync_config_and_state_dir_from_the_environment(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(machine.home)
    monkeypatch.setenv("CALSYNC_CONFIG", "conf/c.toml")
    monkeypatch.setenv("CALSYNC_STATE_DIR", "~/st")
    code, out = machine.run("service", "install")
    assert code == EXIT_OK, out
    config = str((machine.home / "conf" / "c.toml").resolve())
    state = str((machine.home / "st").resolve())
    assert load(machine.paths.plist)["ProgramArguments"] == [
        str(machine.exe.resolve()),
        "--config",
        config,
        "--state-dir",
        state,
        "daemon",
    ]
    assert f"--config {config}: from $CALSYNC_CONFIG" in out
    assert f"--state-dir {state}: from $CALSYNC_STATE_DIR" in out
    body = json.loads(machine.run("service", "install", "--json")[1])
    assert body["from_env"] == {"CALSYNC_CONFIG": config, "CALSYNC_STATE_DIR": state}


def test_a_flag_wins_over_the_environment(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALSYNC_CONFIG", str(machine.home / "env.toml"))
    monkeypatch.setenv("CALSYNC_STATE_DIR", "")
    code, out = machine.run("--config", str(machine.home / "flag.toml"), "service", "install", "--json")
    assert code == EXIT_OK, out
    body = json.loads(out)
    assert body["from_env"] == {}
    assert load(machine.paths.plist)["ProgramArguments"] == [
        str(machine.exe.resolve()),
        "--config",
        str((machine.home / "flag.toml").resolve()),
        "daemon",
    ]


def test_a_failed_launchctl_list_is_a_warning_not_silence(machine: Machine) -> None:
    machine.launchctl.fail_list = True
    code, out = machine.run("service", "install")
    assert code == EXIT_OK, out
    assert "warning: launchctl list failed" in out
    assert f"only covered {machine.paths.agents_dir}" in out
    body = json.loads(machine.run("service", "status", "--json")[1])
    assert len(body["warnings"]) == 1
    assert "only covered" in body["warnings"][0]
    assert "only covered" in machine.run("service", "status")[1]


def test_a_disabled_agent_plist_is_not_another_job(machine: Machine) -> None:
    machine.other_plist("com.example.sync", ["/usr/local/bin/calsync", "run"], Disabled=True)
    code, out = machine.run("service", "install", "--json")
    assert code == EXIT_OK, out
    assert json.loads(out)["others"] == []


def test_install_dry_run_marks_the_bootout_as_conditional(machine: Machine) -> None:
    # Without launchctl a dry run cannot know: the bootout is listed whenever the plist changes.
    code, out = machine.run("service", "install", "--dry-run")
    assert code == EXIT_OK, out
    assert f"  launchctl bootout {TARGET}  (if loaded)\n" in out
    assert f"  launchctl bootstrap {DOMAIN} {machine.paths.plist}\n" in out
    body = json.loads(machine.run("service", "install", "--dry-run", "--json")[1])
    assert body["steps"] == [
        {"command": f"launchctl bootout {TARGET}", "when": "if loaded"},
        {"command": f"launchctl bootstrap {DOMAIN} {machine.paths.plist}", "when": "always"},
    ]
    # unchanged: no bootout; the bootstrap only if not loaded
    assert machine.run("service", "install")[0] == EXIT_OK
    machine.launchctl.calls.clear()
    code, out = machine.run("service", "install", "--dry-run", "--json")
    assert json.loads(out)["steps"] == [
        {"command": f"launchctl bootstrap {DOMAIN} {machine.paths.plist}", "when": "if not loaded"}
    ]
    assert machine.launchctl.calls == []


def replaceable(machine: Machine) -> None:
    """An installed, loaded agent whose plist differs from the one install would write now."""
    machine.paths.agents_dir.mkdir(parents=True)
    machine.paths.plist.write_bytes(plistlib.dumps({"Label": LABEL, "ProgramArguments": ["/old/calsync"]}))
    machine.launchctl.loaded[TARGET] = PRINT_RUNNING


def test_a_replace_waits_until_launchd_has_stopped_the_old_job(machine: Machine) -> None:
    replaceable(machine)
    machine.launchctl.stopping_polls = 3
    code, out = machine.run("service", "install", "--force")
    assert code == EXIT_OK, out
    assert "can take up to about two minutes while a sync cycle finishes" in out
    calls = machine.launchctl.calls
    bootout = calls.index(("bootout", TARGET))
    bootstrap = calls.index(("bootstrap", DOMAIN, str(machine.paths.plist)))
    # three polls still find the job, the fourth does not; only then the bootstrap
    assert calls[bootout + 1 : bootstrap] == [("print", TARGET)] * 4
    assert machine.time.sleeps == [0.5, 0.5, 0.5]
    assert load(machine.paths.plist)["ProgramArguments"] == [str(machine.exe.resolve()), "daemon"]


def test_a_replace_gives_up_when_the_old_job_does_not_go(machine: Machine) -> None:
    replaceable(machine)
    old = machine.paths.plist.read_bytes()
    machine.launchctl.stopping_polls = None
    code, out = machine.run("service", "install", "--force")
    assert code == EXIT_FAILED
    assert "launchd may still be stopping" in out
    assert "calsync service status" in out
    assert ("bootstrap", DOMAIN, str(machine.paths.plist)) not in machine.launchctl.calls
    assert machine.paths.plist.read_bytes() == old  # nothing written either
    assert sum(machine.time.sleeps) == pytest.approx(service.EXIT_TIMEOUT + 30)

    machine.time.sleeps.clear()
    code, out = machine.run("service", "install", "--force", "--json")
    assert code == EXIT_FAILED
    error = json.loads(out)["error"]
    assert error["kind"] == "still_stopping"
    assert error["commands"] == [f"launchctl bootout {TARGET}"]


def test_a_bootout_that_does_not_answer_is_still_stopping(machine: Machine) -> None:
    replaceable(machine)

    def hangs(target: str) -> None:
        raise service.LaunchctlTimeout("launchctl bootout did not answer within 150 s")

    machine.launchctl.bootout = hangs  # type: ignore[method-assign]
    code, out = machine.run("service", "install", "--force")
    assert code == EXIT_FAILED
    assert "launchd may still be stopping" in out
    assert ("bootstrap", DOMAIN, str(machine.paths.plist)) not in machine.launchctl.calls


def test_stop_uses_the_injected_clock_and_sleep() -> None:
    fake = FakeLaunchctl({TARGET: PRINT_RUNNING})
    fake.stopping_polls = 2
    clock = FakeClock()
    assert service._stop(fake, TARGET, clock=clock.clock, sleep=clock.sleep)  # pyright: ignore[reportPrivateUsage]
    assert clock.sleeps == [0.5, 0.5]
    fake = FakeLaunchctl({TARGET: PRINT_RUNNING})
    fake.stopping_polls = None
    clock = FakeClock()
    assert not service._stop(fake, TARGET, clock=clock.clock, sleep=clock.sleep)  # pyright: ignore[reportPrivateUsage]
    assert clock.now - 1000.0 == pytest.approx(150)


def test_the_install_help_tells_the_exit_codes_and_the_limits(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        machine.run("service", "install", "--help")
    text = " ".join(capsys.readouterr().out.split())
    assert "exit 4: another calsync job is set up" in text
    assert "Exit 2: the plist differs" in text
    assert "best effort" in text and "jobs defined elsewhere are not seen" in text
    assert "$CALSYNC_CONFIG and $CALSYNC_STATE_DIR, when set, do" in text


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


def test_uninstall_waits_for_the_job_before_removing_the_plist(machine: Machine) -> None:
    assert machine.run("service", "install")[0] == EXIT_OK
    machine.launchctl.calls.clear()
    machine.launchctl.stopping_polls = 2
    code, out = machine.run("service", "uninstall")
    assert code == EXIT_OK, out
    assert "can take up to about two minutes" in out
    assert machine.launchctl.calls == [("print", TARGET), ("bootout", TARGET)] + [("print", TARGET)] * 3
    assert machine.time.sleeps == [0.5, 0.5]
    assert not machine.paths.plist.exists()


def test_uninstall_keeps_the_plist_while_the_job_is_still_stopping(machine: Machine) -> None:
    assert machine.run("service", "install")[0] == EXIT_OK
    machine.launchctl.stopping_polls = None
    code, out = machine.run("service", "uninstall")
    assert code == EXIT_FAILED
    assert "launchd may still be stopping" in out
    assert "calsync service status" in out
    assert machine.paths.plist.exists()
    code, out = machine.run("service", "uninstall", "--json")
    assert code == EXIT_FAILED
    assert json.loads(out)["error"]["kind"] == "still_stopping"
    assert machine.paths.plist.exists()


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
    assert f"  launchctl bootout {TARGET}  (if loaded)\n" in out
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
        "warnings": [],
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
