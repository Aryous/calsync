import io
from pathlib import Path

import pytest

from calsync import __version__
from calsync.cli import EXIT_AUTH, EXIT_CONFIG, EXIT_OK, EXIT_RUNNING, main
from calsync.lock import AlreadyRunning, RunLock

CONFIG = """
[accounts.dav]
provider = "caldav"
server_url = "{url}"
username = "{user}"
credential = {{ backend = "file", ref = "{secret}" }}
"""


def run(argv: list[str], stdin: str = "") -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, out=out, stdin=io.StringIO(stdin))
    return code, out.getvalue()


def test_version() -> None:
    assert run(["--version"]) == (EXIT_OK, f"calsync {__version__}\n")


def test_config_check_ok_and_errors(tmp_path: Path) -> None:
    good = tmp_path / "good.toml"
    good.write_text(CONFIG.format(url="https://dav.example.com/", user="u", secret=tmp_path / "pw"))
    assert run(["--config", str(good), "config", "check"]) == (EXIT_OK, "ok: 1 accounts, 0 rules\n")
    bad = tmp_path / "bad.toml"
    bad.write_text('[accounts.x]\nprovider = "exchange"\n')
    code, text = run(["--config", str(bad), "config", "check"])
    assert code == EXIT_CONFIG and "accounts.x.provider" in text


def test_missing_config_file(tmp_path: Path) -> None:
    code, text = run(["--config", str(tmp_path / "none.toml"), "config", "check"])
    assert code == EXIT_CONFIG and "config file not found" in text


def test_run_rejects_unknown_rule(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url="https://dav.example.com/", user="u", secret=tmp_path / "pw"))
    code, text = run(["--config", str(cfg), "--state-dir", str(tmp_path / "s"), "run", "--rule", "nope"])
    assert code == EXIT_CONFIG and "unknown rule" in text


def test_lock_is_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    with RunLock(path), pytest.raises(AlreadyRunning) as info, RunLock(path):
        pass
    assert info.value.pid is not None
    with RunLock(path):  # released again
        pass


def test_run_reports_lock_busy(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url="https://dav.example.com/", user="u", secret=tmp_path / "pw"))
    state = tmp_path / "s"
    with RunLock(state / "run.lock"):
        code, text = run(["--config", str(cfg), "--state-dir", str(state), "run"])
    assert code == EXIT_RUNNING and "holds the lock" in text


@pytest.mark.e2e
def test_auth_caldav_stores_and_verifies(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    secret = tmp_path / "secrets" / "dav"
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url=radicale_url, user=dav_user, secret=secret))
    code, text = run(["--config", str(cfg), "auth", "dav"], stdin="app-specific-pw\n")
    assert code == EXIT_OK and "credentials stored" in text
    assert secret.read_text() == "app-specific-pw"
    code, text = run(["--config", str(cfg), "calendars", "dav"])
    assert code == EXIT_OK


def test_auth_error_exit_code(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url="https://dav.example.com/", user="u", secret=tmp_path / "missing"))
    code, text = run(["--config", str(cfg), "calendars", "dav"])
    assert code == EXIT_AUTH and "credential file not found" in text
