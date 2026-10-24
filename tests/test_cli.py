import io
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from calsync import __version__
from calsync.cli import EXIT_AUTH, EXIT_CONFIG, EXIT_FAILED, EXIT_OK, EXIT_RUNNING, main
from calsync.lock import AlreadyRunning, RunLock

if TYPE_CHECKING:
    from calsync.engine import CycleResult, RuleResult
    from calsync.planner import Actual

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


# ----- review fixes ---------------------------------------------------------

CLOSED = "http://127.0.0.1:9/"  # discard port: nothing listens, connections are refused


def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make retry backoff instant: wiring looks CalDavTarget up at call time."""
    import functools

    from calsync import wiring
    from calsync.targets.caldav import CalDavTarget

    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr(wiring, "CalDavTarget", functools.partial(CalDavTarget, sleep=instant))


def secret_file(path: Path, value: str) -> Path:
    from calsync.config import CredentialRef
    from calsync.credentials import write_secret

    write_secret(CredentialRef("file", str(path)), value)
    return path


def test_other_calsync_error_exits_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    no_backoff(monkeypatch)
    secret = secret_file(tmp_path / "pw", "pw")
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url=CLOSED, user="u", secret=secret))
    code, text = run(["--config", str(cfg), "calendars", "dav"])
    assert code == EXIT_FAILED and text.startswith("error: ")


def test_auth_rejects_empty_input_and_keeps_the_credential(tmp_path: Path) -> None:
    secret = secret_file(tmp_path / "pw", "old-secret")
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url="https://dav.example.com/", user="u", secret=secret))
    code, text = run(["--config", str(cfg), "auth", "dav"], stdin="  \n")
    assert code == EXIT_CONFIG and "empty" in text
    assert secret.read_text() == "old-secret" and stat.S_IMODE(secret.stat().st_mode) == 0o600


def test_auth_keeps_the_credential_when_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_backoff(monkeypatch)
    secret = secret_file(tmp_path / "pw", "old-secret")
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url=CLOSED, user="u", secret=secret))
    code, text = run(["--config", str(cfg), "auth", "dav"], stdin="new-secret\n")
    assert code == EXIT_FAILED and "not stored" in text and "new-secret" not in text
    assert secret.read_text() == "old-secret" and stat.S_IMODE(secret.stat().st_mode) == 0o600


def test_auth_creates_no_credential_when_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_backoff(monkeypatch)
    secret = tmp_path / "secrets" / "pw"
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url=CLOSED, user="u", secret=secret))
    code, _ = run(["--config", str(cfg), "auth", "dav"], stdin="new-secret\n")
    assert code == EXIT_FAILED and not secret.exists()


def test_refresh_token_flag_only_for_google(tmp_path: Path) -> None:
    secret = secret_file(tmp_path / "pw", "old-secret")
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url="https://dav.example.com/", user="u", secret=secret))
    code, text = run(["--config", str(cfg), "auth", "dav", "--refresh-token-stdin"], stdin="tok\n")
    assert code == EXIT_CONFIG and "--refresh-token-stdin" in text
    assert secret.read_text() == "old-secret"


def test_lock_busy_mentions_trigger(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url="https://dav.example.com/", user="u", secret=tmp_path / "pw"))
    state = tmp_path / "s"
    with RunLock(state / "run.lock"):
        code, text = run(["--config", str(cfg), "--state-dir", str(state), "run"])
    assert code == EXIT_RUNNING and "calsync trigger" in text


# ----- _print_cycle -----------------------------------------------------------


def _cycle(*results: "RuleResult") -> "CycleResult":
    from datetime import UTC, datetime

    from calsync.engine import CycleResult

    now = datetime(2026, 10, 24, tzinfo=UTC)
    return CycleResult(now, now, {r.rule: r for r in results}, requests=3)


def _printed(result: "CycleResult", *, verbose: bool = False) -> str:
    from calsync.cli import _print_cycle  # pyright: ignore[reportPrivateUsage]

    out = io.StringIO()
    _print_cycle(result, out, verbose=verbose)
    return out.getvalue()


def _actual(href: str) -> "Actual":
    from calsync.planner import Actual

    return Actual("oid-" + href, href, None, None, None)


def test_print_cycle_shows_protected_and_degraded_only_when_nonzero() -> None:
    from calsync.engine import RuleResult

    text = _printed(_cycle(RuleResult("a", created=1), RuleResult("b", protected=2, degraded=3)))
    lines = text.splitlines()
    assert lines[0] == "rule=a status=ok created=1 updated=0 deleted=0 failed=0"
    assert lines[1] == "rule=b status=ok created=0 updated=0 deleted=0 failed=0 protected=2 degraded=3"
    assert lines[2] == "requests=3"


def test_print_cycle_blocked_lists_titles_and_hint() -> None:
    from calsync.engine import RuleResult

    blocked = RuleResult(
        "family",
        status="blocked",
        error="would delete 4 of 4 events",
        error_kind="guard",
        blocked_titles=["Dentist", "Swim\nclass"],
        blocked_titles_complete=False,
    )
    text = _printed(_cycle(blocked))
    assert "status=blocked" in text and "would delete 4 of 4 events" in text
    assert "  - Dentist\n" in text and "  - Swim class\n" in text  # control characters flattened
    assert "  (titles incomplete)\n" in text
    assert "  re-run with --allow-deletes if this is intended\n" in text


def test_print_cycle_complete_titles_have_no_incomplete_marker() -> None:
    from calsync.engine import RuleResult

    blocked = RuleResult("f", status="blocked", error="too many", error_kind="guard", blocked_titles=["X"])
    assert "(titles incomplete)" not in _printed(_cycle(blocked))


def test_dry_run_deletes_show_titles_when_known_else_hrefs() -> None:
    from calsync.engine import RuleResult
    from calsync.planner import Plan

    named = RuleResult(
        "named",
        status="blocked",
        error="too many",
        error_kind="guard",
        plan=Plan(delete=[_actual("/c/1.ics"), _actual("/c/2.ics")]),
        blocked_titles=["One", "Two"],
    )
    bare = RuleResult("bare", plan=Plan(delete=[_actual("/c/3.ics")]))
    text = _printed(_cycle(named, bare), verbose=True)
    assert "/c/1.ics" not in text and "/c/2.ics" not in text
    assert text.count("  - One\n") == 1 and text.count("  - Two\n") == 1  # not listed twice
    assert "  - /c/3.ics\n" in text


def test_print_cycle_failed_rule_shows_its_error() -> None:
    from calsync.engine import RuleResult

    failed = RuleResult("f", status="failed", error="AuthError: rejected", error_kind="auth")
    assert "error='AuthError: rejected'" in _printed(_cycle(failed))


# ----- wiring ---------------------------------------------------------------


def test_adapters_aclose_closes_every_client_even_if_one_fails() -> None:
    import asyncio

    import httpx

    from calsync.http import RequestLog
    from calsync.wiring import Adapters

    class Broken(httpx.AsyncClient):
        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    good = httpx.AsyncClient()
    adapters = Adapters(RequestLog(), clients=[Broken(), good])
    with pytest.raises(RuntimeError, match="close failed"):
        asyncio.run(adapters.aclose())
    assert good.is_closed


def test_build_adapters_closes_clients_when_building_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import httpx

    from calsync import wiring
    from calsync.config import load_config

    made: list[httpx.AsyncClient] = []
    real_make = wiring.make_client

    def recording(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client = real_make(*args, **kwargs)
        made.append(client)
        return client

    calls = 0

    def target(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("unexpected")
        return object()

    monkeypatch.setattr(wiring, "make_client", recording)
    monkeypatch.setattr(wiring, "CalDavTarget", target)
    monkeypatch.setenv("CALSYNC_TEST_PW", "pw")
    cfg = tmp_path / "c.toml"
    two = "\n".join(
        f'[accounts.{n}]\nprovider = "caldav"\nserver_url = "https://dav.example.com/"\nusername = "u"\n'
        'credential = { backend = "env", ref = "CALSYNC_TEST_PW" }\n'
        for n in ("a", "b")
    )
    cfg.write_text(two)
    config = load_config(cfg)
    with pytest.raises(RuntimeError, match="unexpected"):
        asyncio.run(wiring.build_adapters(config, {"a", "b"}))
    assert len(made) == 2 and all(c.is_closed for c in made)
