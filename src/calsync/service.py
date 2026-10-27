"""``calsync service``: run ``calsync daemon`` at login as a launchd agent (macOS).

The agent's label is ``dev.calsync.daemon`` and its plist lives in ``~/Library/LaunchAgents``;
the daemon's output goes to ``~/Library/Logs/calsync/daemon.log``. ``install`` never touches
another job: it lists launchd jobs that also run calsync (two schedulers against the same
accounts would fight over the run lock or duplicate work) and proceeds past them only with
``--force``. Other jobs are found cheaply and best effort: labels in ``launchctl list`` that
contain "calsync", plus plists in ``~/Library/LaunchAgents`` whose label or program does; only
those few are ``launchctl print``-ed.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

LABEL = "dev.calsync.daemon"
PATH_ENV = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
EXIT_TIMEOUT = 120  # seconds launchd waits after SIGTERM: the daemon finishes its cycle
LAUNCHCTL_TIMEOUT = 10

OK, FAILED, REFUSED = 0, 1, 2  # the CLI's EXIT_OK, EXIT_FAILED, EXIT_CONFIG

# `launchctl print` / `bootout` of a job that is not loaded: ESRCH (3) or 113 ("Could not find
# specified service").
NOT_LOADED_CODES = (3, 113)


class LaunchctlError(Exception):
    """``launchctl`` failed, could not be run, or did not answer."""


class Launchctl(Protocol):
    def bootstrap(self, domain: str, plist: Path) -> None: ...

    def bootout(self, domain_label: str) -> None:
        """Unload; a job that is not loaded is fine."""
        ...

    def print(self, domain_label: str) -> str | None:
        """The job's description, ``None`` when it is not loaded."""
        ...

    def list(self) -> str: ...


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=LAUNCHCTL_TIMEOUT, check=False)


class SystemLaunchctl:
    """The real ``launchctl``."""

    def __init__(self, run: Runner | None = None) -> None:
        self._runner = run if run is not None else _run

    def _call(self, *args: str) -> "subprocess.CompletedProcess[str]":
        argv = ["launchctl", *args]
        try:
            return self._runner(argv)
        except FileNotFoundError:
            raise LaunchctlError("launchctl not found (calsync service needs macOS)") from None
        except subprocess.TimeoutExpired:
            raise LaunchctlError(f"launchctl {args[0]} did not answer within {LAUNCHCTL_TIMEOUT} s") from None

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
        args = ("bootout", domain_label)
        result = self._call(*args)
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
    which: Callable[[str], str | None] | None = None, argv0: str | None = None
) -> Path | None:
    """The absolute path of the ``calsync`` executable: ``calsync`` on ``PATH``, else the running
    program if it is a ``calsync`` entry point; resolved. ``None`` if neither is an existing
    executable file."""
    which = shutil.which if which is None else which
    argv0 = sys.argv[0] if argv0 is None else argv0
    candidates: list[Path] = []
    if found := which("calsync"):
        candidates.append(Path(found))
    if argv0 and Path(argv0).name == "calsync":
        candidates.append(Path(argv0))
    for candidate in candidates:
        try:
            path = candidate.resolve()
        except OSError:
            continue
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return path
    return None


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
        if not isinstance(label, str) or label == LABEL:
            continue
        raw = job.get("ProgramArguments")
        argv = [str(a) for a in cast(list[Any], raw)] if isinstance(raw, list) else []
        program = job.get("Program")
        if not argv and isinstance(program, str):
            argv = [program]
        command = " ".join(argv) or None
        if _mentions_calsync(label) or (command is not None and _mentions_calsync(command)):
            found[label] = (path, command)
    return found


def find_others(launchctl: Launchctl | None, paths: Paths, domain: str) -> list[OtherJob]:
    """Other launchd jobs that run calsync, best effort. ``launchctl`` ``None``: from the
    LaunchAgents plists only, loaded unknown."""
    on_disk = _agent_plists(paths)
    labels = dict.fromkeys(on_disk)
    if launchctl is not None:
        with contextlib.suppress(LaunchctlError):
            labels.update(dict.fromkeys(x for x in parse_list(launchctl.list()) if _mentions_calsync(x)))
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
    return others


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
    force: bool = False,
    dry_run: bool = False,
) -> Outcome:
    domain = f"gui/{uid}"
    target = f"{domain}/{LABEL}"
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
    others = find_others(None if dry_run else launchctl, paths, domain)
    lines = _warn_others(others, domain)
    base: dict[str, Any] = {
        "label": LABEL,
        "plist": str(paths.plist),
        "log": str(paths.log),
        "program": program_arguments(executable, config=config, state_dir=state_dir, listen=listen),
        "others": [job.to_json() for job in others],
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
            others=base["others"],
        )
    bootout = f"launchctl bootout {target}"
    bootstrap = f"launchctl bootstrap {domain} {paths.plist}"
    if dry_run:
        commands = ([bootout] if old is not None and changed else []) + [bootstrap]
        xml = new.decode()
        lines += [
            xml.rstrip("\n"),
            "would run (bootout only if loaded; bootstrap skipped if unchanged and loaded):",
        ]
        lines += [f"  {command}" for command in commands]
        return Outcome(OK, {**base, "action": "would_install", "commands": commands, "plist_xml": xml}, lines)

    loaded = launchctl.print(target) is not None
    commands: list[str] = []
    try:
        if loaded and changed:
            launchctl.bootout(target)
            commands.append(bootout)
        if changed:
            paths.agents_dir.mkdir(parents=True, exist_ok=True)
            paths.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            paths.log_dir.chmod(0o700)
            _write_atomic(paths.plist, new)
        if changed or not loaded:
            launchctl.bootstrap(domain, paths.plist)
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
    lines += [summary, f"log: {paths.log}"]
    return Outcome(OK, {**base, "action": action, "commands": commands}, lines)


def uninstall(launchctl: Launchctl, paths: Paths, uid: int, *, dry_run: bool = False) -> Outcome:
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
        lines = ["would run (only if loaded):", f"  {bootout}"]
        if exists:
            lines.append(f"would remove {paths.plist}")
        return Outcome(OK, {**base, "action": "would_uninstall", "commands": [bootout]}, [*lines, kept])
    commands: list[str] = []
    try:
        if launchctl.print(target) is not None:
            launchctl.bootout(target)
            commands.append(bootout)
    except LaunchctlError as exc:
        return _refused("launchctl", f"error: {exc}", [], code=FAILED, commands=commands)
    if exists:
        paths.plist.unlink(missing_ok=True)
    if exists or commands:
        action, summary = "removed", f"uninstalled {LABEL}"
    else:
        action, summary = "not_installed", f"{LABEL} is not installed"
    lines = [f"ran: {command}" for command in commands]
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
    others = find_others(launchctl, paths, domain)
    data: dict[str, Any] = {
        "installed": paths.plist.exists(),
        "loaded": text is not None,
        "state": job.state,
        "pid": job.pid,
        "last_exit": job.last_exit,
        "plist": str(paths.plist),
        "log": str(paths.log),
        "others": [o.to_json() for o in others],
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
    ]
    return Outcome(OK, data, lines)
