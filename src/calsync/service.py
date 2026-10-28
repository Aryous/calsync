"""``calsync service``: run ``calsync daemon`` at login as a launchd agent (macOS).

The agent's label is ``dev.calsync.daemon`` and its plist lives in ``~/Library/LaunchAgents``;
the daemon's output goes to ``~/Library/Logs/calsync/daemon.log``. ``install`` never touches
another job: it lists launchd jobs that also run calsync (two schedulers against the same
accounts would fight over the run lock or duplicate work) and proceeds past them only with
``--force``. Other jobs are found cheaply and best effort: labels in ``launchctl list`` that
contain "calsync", plus plists in ``~/Library/LaunchAgents`` (not ``Disabled``) whose label or
program does; only those few are ``launchctl print``-ed. Jobs defined elsewhere (another user,
``/Library/LaunchAgents``, ``/Library/LaunchDaemons``) are not seen.

Replacing or removing a loaded agent boots it out and waits until launchd has let it go:
launchd gives the daemon up to ``ExitTimeOut`` (120 s) to finish a sync cycle, so this can take
about two minutes. Nothing is bootstrapped, and no plist removed, while the old job is still
there.

Every ``launchctl`` call goes through a :class:`Launchctl`; the CLI builds the real one
(:func:`system_launchctl`), tests pass a fake. Standard library only. Not an operation: no
daemon, no lock, no state database.
"""

import contextlib
import difflib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

LABEL = "dev.calsync.daemon"
PATH_ENV = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
EXIT_TIMEOUT = 120  # seconds launchd waits after SIGTERM: the daemon finishes its cycle
LAUNCHCTL_TIMEOUT = 10
# How long a bootout may take, the call and then the wait until `print` no longer finds the job.
STOP_TIMEOUT = EXIT_TIMEOUT + 30
STOP_POLL = 0.5

OK, FAILED, REFUSED, CONFLICT = 0, 1, 2, 4  # the CLI's EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_RUNNING

STOPPING_NOTICE = f"stopping {LABEL}: this can take up to about two minutes while a sync cycle finishes"
NO_GUI_HINT = "hint: run this from a logged-in desktop session (not over SSH)"

# `launchctl print` / `bootout` of a job that is not loaded: ESRCH (3) or 113 ("Could not find
# specified service").
NOT_LOADED_CODES = (3, 113)


class LaunchctlError(Exception):
    """``launchctl`` failed, could not be run, or did not answer."""


class LaunchctlTimeout(LaunchctlError):
    """``launchctl`` did not answer in time."""


class Launchctl(Protocol):
    def bootstrap(self, domain: str, plist: Path) -> None: ...

    def bootout(self, domain_label: str) -> None:
        """Unload; a job that is not loaded is fine. May return before the job has gone."""
        ...

    def print(self, domain_label: str) -> str | None:
        """The job's description, ``None`` when it is not loaded."""
        ...

    def list(self) -> str: ...


Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


# The waits of a bootout; tests replace them.
_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep


class SystemLaunchctl:
    """The real ``launchctl``."""

    def __init__(self, run: Runner | None = None) -> None:
        self._runner = run if run is not None else _run

    def _call(self, *args: str, timeout: float = LAUNCHCTL_TIMEOUT) -> "subprocess.CompletedProcess[str]":
        argv = ["launchctl", *args]
        try:
            return self._runner(argv, timeout)
        except FileNotFoundError:
            raise LaunchctlError("launchctl not found (calsync service needs macOS)") from None
        except subprocess.TimeoutExpired:
            raise LaunchctlTimeout(f"launchctl {args[0]} did not answer within {timeout:g} s") from None

    @staticmethod
    def _failed(args: tuple[str, ...], result: "subprocess.CompletedProcess[str]") -> LaunchctlError:
        detail = (result.stderr or result.stdout or "").strip()
        message = f"launchctl {args[0]} failed (exit {result.returncode})"
        return LaunchctlError(f"{message}: {detail}" if detail else message)

    def bootstrap(self, domain: str, plist: Path) -> None:
        args = ("bootstrap", domain, str(plist))
        result = self._call(*args)
        if result.returncode != 0:
            raise self._failed(args, result)

    def bootout(self, domain_label: str) -> None:
        # launchd waits up to ExitTimeOut for the daemon to finish its cycle.
        args = ("bootout", domain_label)
        result = self._call(*args, timeout=STOP_TIMEOUT)
        if result.returncode not in (0, *NOT_LOADED_CODES):
            raise self._failed(args, result)

    def print(self, domain_label: str) -> str | None:
        args = ("print", domain_label)
        result = self._call(*args)
        if result.returncode == 0:
            return result.stdout
        if result.returncode in NOT_LOADED_CODES or "could not find" in (result.stderr or "").lower():
            return None
        raise self._failed(args, result)

    def list(self) -> str:
        args = ("list",)
        result = self._call(*args)
        if result.returncode != 0:
            raise self._failed(args, result)
        return result.stdout


def system_launchctl() -> Launchctl:
    return SystemLaunchctl()


@dataclass(frozen=True)
class Paths:
    agents_dir: Path
    log_dir: Path

    @classmethod
    def default(cls, home: Path | None = None) -> "Paths":
        home = home if home is not None else Path.home()
        return cls(home / "Library" / "LaunchAgents", home / "Library" / "Logs" / "calsync")

    @property
    def plist(self) -> Path:
        return self.agents_dir / f"{LABEL}.plist"

    @property
    def log(self) -> Path:
        return self.log_dir / "daemon.log"


def find_executable(
    which: Callable[[str], str | None] | None = None,
    argv0: str | None = None,
    python: str | None = None,
) -> Path | None:
    """The absolute path of the ``calsync`` the service should run, resolved; the first existing
    executable file of: the running program if it is named ``calsync``; ``calsync`` next to the
    running Python (``python -m calsync`` in a venv or tool environment); ``calsync`` on
    ``PATH``. ``None`` if there is none."""
    which = shutil.which if which is None else which
    argv0 = sys.argv[0] if argv0 is None else argv0
    python = sys.executable if python is None else python

    def usable(candidate: Path) -> Path | None:
        try:
            path = candidate.resolve()
        except OSError:
            return None
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return path
        return None

    if argv0 and Path(argv0).name == "calsync" and (found := usable(Path(argv0))):
        return found
    # Not resolved first: a venv's bin/python is a symlink to the base interpreter.
    if python and (found := usable(Path(python).parent / "calsync")):
        return found
    if (on_path := which("calsync")) and (found := usable(Path(on_path))):
        return found
    return None


CHECKOUT_WARNING = (
    "the daemon will stop working if this checkout is removed; install calsync as a tool "
    "(uv tool install) for a long-lived service"
)


def checkout_warning(executable: Path) -> str | None:
    """A warning when ``executable`` lives in a project's ``.venv`` next to a ``.git`` (a repository
    checkout or a git worktree): such an environment goes away with the checkout."""
    parts = executable.parts
    for index, part in enumerate(parts[:-1]):
        if part != ".venv" or index == 0:
            continue
        root = Path(*parts[:index])
        if (root / ".git").exists():
            return f"warning: {executable} is in the virtualenv of the checkout {root}: {CHECKOUT_WARNING}"
    return None


ENV_FLAGS = (("CALSYNC_CONFIG", "--config"), ("CALSYNC_STATE_DIR", "--state-dir"))


def locations(
    config: Path | None, state_dir: Path | None, environ: Mapping[str, str]
) -> tuple[Path | None, Path | None, dict[str, str]]:
    """The ``--config`` and ``--state-dir`` for the agent, absolute (launchd starts the daemon in
    ``/``, with none of the shell's environment): a flag given wins; else ``$CALSYNC_CONFIG`` /
    ``$CALSYNC_STATE_DIR`` when set, so the service uses what the shell uses. The third value
    maps each variable taken that way to the path embedded."""
    from_env: dict[str, str] = {}

    def pick(flag: Path | None, variable: str) -> Path | None:
        if flag is not None:
            return flag.expanduser().resolve()
        if value := environ.get(variable):
            path = Path(value).expanduser().resolve()
            from_env[variable] = str(path)
            return path
        return None

    return pick(config, "CALSYNC_CONFIG"), pick(state_dir, "CALSYNC_STATE_DIR"), from_env


def program_arguments(
    executable: Path, *, config: Path | None = None, state_dir: Path | None = None, listen: str | None = None
) -> list[str]:
    """``--config`` and ``--state-dir`` are global options: they go before ``daemon``."""
    argv = [str(executable)]
    if config is not None:
        argv += ["--config", str(config)]
    if state_dir is not None:
        argv += ["--state-dir", str(state_dir)]
    argv.append("daemon")
    if listen is not None:
        argv += ["--listen", listen]
    return argv


def render_plist(
    executable: Path,
    paths: Paths,
    *,
    config: Path | None = None,
    state_dir: Path | None = None,
    listen: str | None = None,
    lang: str | None = None,
) -> bytes:
    environment = {"PATH": PATH_ENV}
    if lang:
        environment["LANG"] = lang
    body: dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": program_arguments(executable, config=config, state_dir=state_dir, listen=listen),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ExitTimeOut": EXIT_TIMEOUT,
        "ProcessType": "Background",
        "StandardOutPath": str(paths.log),
        "StandardErrorPath": str(paths.log),
        "EnvironmentVariables": environment,
    }
    return plistlib.dumps(body, fmt=plistlib.FMT_XML, sort_keys=True)


# --- launchctl output -------------------------------------------------------------------


@dataclass
class JobState:
    state: str | None = None
    pid: int | None = None
    last_exit: int | None = None
    program: str | None = None
    arguments: list[str] = field(default_factory=lambda: [])


_ENTRY = re.compile(r"^\s*(?P<key>[^=]+?)\s*=\s*(?P<value>.*?)\s*$")


def _int(text: str) -> int | None:
    return int(text) if re.fullmatch(r"-?\d+", text) else None


def parse_print(text: str) -> JobState:
    """The few fields we need from ``launchctl print``, read tolerantly: top-level ``state``,
    ``pid``, ``last exit code`` (or the older ``last exit status``; ``(never exited)`` is
    ``None``), ``program`` and the ``arguments`` block. The format is not an interface: anything
    unrecognised is skipped."""
    job = JobState()
    depth = 0
    block: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "}":
            depth = max(depth - 1, 0)
            if depth <= 1:
                block = None
            continue
        if stripped.endswith("{"):
            depth += 1
            match = _ENTRY.match(stripped[:-1])
            block = match.group("key") if match and depth == 2 else None
            continue
        if depth == 2 and block == "arguments":
            job.arguments.append(stripped)
            continue
        if depth != 1 or not (match := _ENTRY.match(stripped)):
            continue
        key, value = match.group("key"), match.group("value")
        if key == "state":
            job.state = value
        elif key == "pid":
            job.pid = _int(value)
        elif key in ("last exit code", "last exit status"):
            job.last_exit = _int(value)
        elif key == "program":
            job.program = value
    return job


def parse_list(text: str) -> list[str]:
    """The labels in ``launchctl list`` (``PID<tab>Status<tab>Label`` lines)."""
    labels: list[str] = []
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or parts[0] == "PID":
            continue
        labels.append(parts[2].strip())
    return labels


# --- other calsync jobs -----------------------------------------------------------------


@dataclass(frozen=True)
class OtherJob:
    label: str
    program: str | None
    loaded: bool | None  # None: not checked (a dry run calls no launchctl)
    plist: Path | None = None

    def to_json(self) -> dict[str, Any]:
        return {"label": self.label, "program": self.program, "loaded": self.loaded}


def _mentions_calsync(text: str) -> bool:
    return "calsync" in text.lower()


def _agent_plists(paths: Paths) -> dict[str, tuple[Path, str | None]]:
    """Plists in LaunchAgents (other than ours) whose label or program mentions calsync."""
    found: dict[str, tuple[Path, str | None]] = {}
    if not paths.agents_dir.is_dir():
        return found
    for path in sorted(paths.agents_dir.glob("*.plist")):
        if path.name == paths.plist.name:
            continue
        try:
            data = plistlib.loads(path.read_bytes())
        except Exception:  # unreadable or not a plist: not ours to judge
            continue
        if not isinstance(data, dict):
            continue
        job = cast(dict[str, Any], data)
        label = job.get("Label")
        if not isinstance(label, str) or label == LABEL or job.get("Disabled") is True:
            continue  # a disabled job does not load
        raw = job.get("ProgramArguments")
        argv = [str(a) for a in cast(list[Any], raw)] if isinstance(raw, list) else []
        program = job.get("Program")
        if not argv and isinstance(program, str):
            argv = [program]
        command = " ".join(argv) or None
        if _mentions_calsync(label) or (command is not None and _mentions_calsync(command)):
            found[label] = (path, command)
    return found


def find_others(launchctl: Launchctl | None, paths: Paths, domain: str) -> tuple[list[OtherJob], list[str]]:
    """Other launchd jobs that run calsync, best effort, and warnings about the search itself.
    ``launchctl`` ``None``: from the LaunchAgents plists only, loaded unknown."""
    on_disk = _agent_plists(paths)
    labels = dict.fromkeys(on_disk)
    warnings: list[str] = []
    if launchctl is not None:
        try:
            listed = parse_list(launchctl.list())
        except LaunchctlError as exc:
            warnings.append(
                f"warning: {exc}; the check for other calsync jobs only covered {paths.agents_dir}"
            )
        else:
            labels.update(dict.fromkeys(x for x in listed if _mentions_calsync(x)))
    labels.pop(LABEL, None)
    others: list[OtherJob] = []
    for label in sorted(labels):
        plist, command = on_disk.get(label, (None, None))
        loaded: bool | None = None
        if launchctl is not None:
            try:
                text = launchctl.print(f"{domain}/{label}")
            except LaunchctlError:
                text = None
            loaded = text is not None
            if text is not None and command is None:
                job = parse_print(text)
                command = " ".join(job.arguments) or job.program
        others.append(OtherJob(label, command, loaded, plist))
    return others, warnings


def _warn_others(others: list[OtherJob], domain: str) -> list[str]:
    lines: list[str] = []
    for job in others:
        lines.append(
            f"warning: another launchd job runs calsync: {job.label} ({job.program or 'program unknown'})"
        )
        if job.loaded is False and job.plist is not None:
            lines.append(
                f"  it is not loaded now but loads at the next login: move {job.plist} out of the way"
            )
        else:
            lines.append(f"  unload it first: launchctl bootout {domain}/{job.label}")
    if others:
        lines.append(
            "  two schedulers would compete: two calsync instances against the same accounts "
            "fight over the run lock or duplicate work"
        )
    return lines


# --- the commands -----------------------------------------------------------------------


@dataclass
class Outcome:
    """What a command did: its exit code, its JSON body and its text lines."""

    code: int
    data: dict[str, Any]
    lines: list[str]


def _refused(kind: str, message: str, lines: list[str], code: int = REFUSED, **extra: Any) -> Outcome:
    return Outcome(code, {"error": {"kind": kind, "message": message, **extra}}, [*lines, message])


def plist_diff(old: bytes, new: bytes, path: Path) -> str:
    return "".join(
        difflib.unified_diff(
            old.decode("utf-8", "replace").splitlines(keepends=True),
            new.decode("utf-8", "replace").splitlines(keepends=True),
            fromfile=f"{path} (installed)",
            tofile=f"{path} (new)",
        )
    )


def _write_atomic(path: Path, body: bytes) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def _stop(
    launchctl: Launchctl,
    target: str,
    *,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> bool:
    """Boot ``target`` out and wait until ``print`` no longer finds it, within ``STOP_TIMEOUT``.
    ``False``: launchd still had the job at the deadline (it may still be stopping it)."""
    clock = _clock if clock is None else clock
    sleep = _sleep if sleep is None else sleep
    deadline = clock() + STOP_TIMEOUT
    try:
        launchctl.bootout(target)
    except LaunchctlTimeout:
        return False
    while launchctl.print(target) is not None:
        if clock() >= deadline:
            return False
        sleep(STOP_POLL)
    return True


def _still_stopping(lines: list[str], commands: list[str], what: str) -> Outcome:
    return _refused(
        "still_stopping",
        f"error: launchd may still be stopping {LABEL} (still loaded after {STOP_TIMEOUT} s); "
        f"{what}; check `calsync service status` and run this again",
        lines,
        code=FAILED,
        commands=commands,
    )


def _no_gui_session(message: str) -> bool:
    """``bootstrap`` into ``gui/<uid>`` without a logged-in desktop session (e.g. over SSH)."""
    text = message.lower()
    return "failed: 125" in text or "domain does not support specified action" in text


def install(
    launchctl: Launchctl,
    paths: Paths,
    uid: int,
    *,
    executable: Path | None,
    config: Path | None = None,
    state_dir: Path | None = None,
    listen: str | None = None,
    lang: str | None = None,
    from_env: dict[str, str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    notice: Callable[[str], None] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Outcome:
    """``from_env``: the variables ``config`` / ``state_dir`` were taken from (see
    :func:`locations`). ``notice`` gets a line before a wait (the CLI prints it at once);
    without one it goes into the outcome's lines."""
    domain = f"gui/{uid}"
    target = f"{domain}/{LABEL}"
    from_env = from_env or {}
    if executable is None:
        return _refused(
            "no_executable",
            "error: cannot find the calsync executable (an absolute path to an existing file): "
            "install calsync (uv tool install ...) or run it by its full path",
            [],
        )
    new = render_plist(executable, paths, config=config, state_dir=state_dir, listen=listen, lang=lang)
    old = paths.plist.read_bytes() if paths.plist.exists() else None
    changed = old != new
    others, warnings = find_others(None if dry_run else launchctl, paths, domain)
    if warning := checkout_warning(executable):
        warnings.append(warning)
    lines = [*_warn_others(others, domain), *warnings]
    flags = dict(ENV_FLAGS)
    lines += [
        f"{flags[variable]} {path}: from ${variable} (launchd does not see your shell's environment)"
        for variable, path in from_env.items()
    ]
    base: dict[str, Any] = {
        "label": LABEL,
        "plist": str(paths.plist),
        "log": str(paths.log),
        "program": program_arguments(executable, config=config, state_dir=state_dir, listen=listen),
        "from_env": dict(from_env),
        "others": [job.to_json() for job in others],
        "warnings": [warning.removeprefix("warning: ") for warning in warnings],
        "dry_run": dry_run,
    }
    if old is not None and changed and not force:
        diff = plist_diff(old, new, paths.plist)
        return _refused(
            "plist_differs",
            f"error: {paths.plist} exists and differs; use --force to replace it",
            [*lines, diff.rstrip("\n")],
            diff=diff,
        )
    if others and not force:
        return _refused(
            "other_jobs",
            "error: another calsync job is set up in launchd; unload it first, or use --force to install anyway",
            lines,
            code=CONFLICT,
            others=base["others"],
        )
    bootout = f"launchctl bootout {target}"
    bootstrap = f"launchctl bootstrap {domain} {paths.plist}"
    program = f"program: {executable}"
    if dry_run:
        # Without launchctl a dry run cannot know whether the job is loaded: say when each runs.
        steps = [(bootout, "if loaded"), (bootstrap, "always")] if changed else [(bootstrap, "if not loaded")]
        xml = new.decode()
        lines += [xml.rstrip("\n"), program, "would run:"]
        lines += [f"  {command}" + ("" if when == "always" else f"  ({when})") for command, when in steps]
        return Outcome(
            OK,
            {
                **base,
                "action": "would_install",
                "commands": [command for command, _ in steps],
                "steps": [{"command": command, "when": when} for command, when in steps],
                "plist_xml": xml,
            },
            lines,
        )

    commands: list[str] = []
    try:
        loaded = launchctl.print(target) is not None
        if loaded and changed:
            if notice is None:
                lines.append(STOPPING_NOTICE)
            else:
                notice(STOPPING_NOTICE)
            commands.append(bootout)
            if not _stop(launchctl, target, clock=clock, sleep=sleep):
                return _still_stopping(lines, commands, "nothing was installed")
        if changed:
            paths.agents_dir.mkdir(parents=True, exist_ok=True)
            paths.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            paths.log_dir.chmod(0o700)
            _write_atomic(paths.plist, new)
        if changed or not loaded:
            try:
                launchctl.bootstrap(domain, paths.plist)
            except LaunchctlError as exc:
                message = f"error: {exc}"
                extra: dict[str, Any] = {"commands": commands}
                if _no_gui_session(str(exc)):
                    extra["hint"] = NO_GUI_HINT.removeprefix("hint: ")
                    return Outcome(
                        FAILED,
                        {"error": {"kind": "launchctl", "message": message, **extra}},
                        [*lines, message, NO_GUI_HINT],
                    )
                return _refused("launchctl", message, lines, code=FAILED, **extra)
            commands.append(bootstrap)
    except LaunchctlError as exc:
        return _refused("launchctl", f"error: {exc}", lines, code=FAILED, commands=commands)
    if old is None:
        action, summary = "installed", f"installed {LABEL}: {paths.plist}"
    elif changed:
        action, summary = "replaced", f"replaced {LABEL}: {paths.plist}"
    else:
        action = "already_installed"
        summary = f"already installed: {paths.plist}" + ("" if loaded else " (loaded it now)")
    lines += [f"ran: {command}" for command in commands]
    lines += [summary, program, f"log: {paths.log}"]
    return Outcome(OK, {**base, "action": action, "commands": commands}, lines)


def uninstall(
    launchctl: Launchctl,
    paths: Paths,
    uid: int,
    *,
    dry_run: bool = False,
    notice: Callable[[str], None] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Outcome:
    """The plist is removed only once the job is gone (or was not loaded)."""
    target = f"gui/{uid}/{LABEL}"
    bootout = f"launchctl bootout {target}"
    exists = paths.plist.exists()
    kept = f"kept: the logs ({paths.log_dir}) and the state directory stay"
    base: dict[str, Any] = {
        "label": LABEL,
        "plist": str(paths.plist),
        "log": str(paths.log),
        "kept": [str(paths.log_dir)],
        "dry_run": dry_run,
    }
    if dry_run:
        lines = ["would run:", f"  {bootout}  (if loaded)"]
        if exists:
            lines.append(f"would remove {paths.plist}")
        return Outcome(OK, {**base, "action": "would_uninstall", "commands": [bootout]}, [*lines, kept])
    commands: list[str] = []
    lines: list[str] = []
    try:
        if launchctl.print(target) is not None:
            if notice is None:
                lines.append(STOPPING_NOTICE)
            else:
                notice(STOPPING_NOTICE)
            commands.append(bootout)
            if not _stop(launchctl, target, clock=clock, sleep=sleep):
                return _still_stopping(lines, commands, f"{paths.plist} was kept")
    except LaunchctlError as exc:
        return _refused("launchctl", f"error: {exc}", lines, code=FAILED, commands=commands)
    if exists:
        paths.plist.unlink(missing_ok=True)
    if exists or commands:
        action, summary = "removed", f"uninstalled {LABEL}"
    else:
        action, summary = "not_installed", f"{LABEL} is not installed"
    lines += [f"ran: {command}" for command in commands]
    if exists:
        lines.append(f"removed {paths.plist}")
    return Outcome(OK, {**base, "action": action, "commands": commands}, [*lines, summary, kept])


def status(launchctl: Launchctl, paths: Paths, uid: int) -> Outcome:
    domain = f"gui/{uid}"
    try:
        text = launchctl.print(f"{domain}/{LABEL}")
    except LaunchctlError as exc:
        return _refused("launchctl", f"error: {exc}", [], code=FAILED)
    job = parse_print(text) if text is not None else JobState()
    others, warnings = find_others(launchctl, paths, domain)
    data: dict[str, Any] = {
        "installed": paths.plist.exists(),
        "loaded": text is not None,
        "state": job.state,
        "pid": job.pid,
        "last_exit": job.last_exit,
        "plist": str(paths.plist),
        "log": str(paths.log),
        "others": [o.to_json() for o in others],
        "warnings": [warning.removeprefix("warning: ") for warning in warnings],
    }

    def show(value: object) -> str:
        return "-" if value is None else str(value)

    lines = [
        f"installed: {'yes' if data['installed'] else 'no'} ({paths.plist})",
        f"loaded: {'yes' if data['loaded'] else 'no'}" + (f" ({job.state})" if job.state else ""),
        f"pid: {show(job.pid)}",
        f"last exit: {show(job.last_exit)}",
        f"log: {paths.log}",
        *_warn_others(others, domain),
        *warnings,
    ]
    return Outcome(OK, data, lines)
