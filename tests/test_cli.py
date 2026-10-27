import io
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

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
    # Nothing cached: the list is fetched first, and that fails at the account's setup.
    assert code == EXIT_AUTH and 'AuthError: account "dav" has no usable credential' in text
    assert "next: calsync auth dav" in text


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
    assert code == EXIT_RUNNING
    assert text == (
        f"another calsync process (pid {os.getpid()}) holds the lock; if it is the daemon, use `calsync trigger`\n"
    )


def test_lock_busy_with_allow_deletes_points_to_approve(tmp_path: Path) -> None:
    """`trigger` cannot let blocked deletes through: the advice is `approve`, through the daemon."""
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url="https://dav.example.com/", user="u", secret=tmp_path / "pw"))
    state = tmp_path / "s"
    with RunLock(state / "run.lock"):
        code, text = run(["--config", str(cfg), "--state-dir", str(state), "run", "--allow-deletes"])
    assert code == EXIT_RUNNING
    assert text == (
        f"another calsync process (pid {os.getpid()}) holds the lock. If it is the daemon, approve the "
        "blocked deletes through it: check `calsync blocked --rule RULE`, then `calsync approve RULE`\n"
    )
    assert "trigger" not in text and "stop" not in text
    from argparse import Namespace

    from calsync.cli import _describe  # pyright: ignore[reportPrivateUsage]

    one = Namespace(command="run", allow_deletes=True, rules=["family"])
    assert _describe(AlreadyRunning(None), one).endswith(
        "check `calsync blocked --rule family`, then `calsync approve family`\n"
    )


def test_lock_message_without_a_pid(tmp_path: Path) -> None:
    from calsync.cli import _describe  # pyright: ignore[reportPrivateUsage]
    from calsync.lock import AlreadyRunning

    assert _describe(AlreadyRunning(None)) == (
        "another calsync process holds the lock; if it is the daemon, use `calsync trigger`\n"
    )


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


def test_print_cycle_shows_out_of_window_only_when_nonzero() -> None:
    from calsync.engine import RuleResult

    text = _printed(_cycle(RuleResult("a", created=1), RuleResult("b", created=1, out_of_window=1)))
    lines = text.splitlines()
    assert lines[0] == "rule=a status=ok created=1 updated=0 deleted=0 failed=0"
    assert lines[1] == "rule=b status=ok created=1 updated=0 deleted=0 failed=0 out_of_window=1"


def test_print_cycle_shows_reverted_only_when_nonzero() -> None:
    from calsync.engine import RuleResult

    text = _printed(_cycle(RuleResult("a", updated=1), RuleResult("b", updated=2, reverted=1)))
    lines = text.splitlines()
    assert lines[0] == "rule=a status=ok created=0 updated=1 deleted=0 failed=0"
    assert lines[1] == "rule=b status=ok created=0 updated=2 deleted=0 failed=0 reverted=1"


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


# ----- exit codes ---------------------------------------------------------------


def _result(name: str, status: str = "ok", kind: str | None = None) -> "RuleResult":
    from calsync.engine import RuleResult

    return RuleResult(name, status=status, error_kind=kind)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("results", "code"),
    [
        ([], EXIT_OK),
        ([("a", "ok", None)], EXIT_OK),
        ([("a", "blocked", "guard")], EXIT_FAILED),
        ([("a", "partial", "other")], EXIT_FAILED),
        ([("a", "failed", "other"), ("b", "failed", "config")], EXIT_CONFIG),
        ([("a", "failed", "config"), ("b", "failed", "auth")], EXIT_AUTH),
        ([("a", "blocked", "guard"), ("b", "partial", "auth"), ("c", "ok", None)], EXIT_AUTH),
    ],
)
def test_run_exit_code_precedence(results: list[tuple[str, str, str | None]], code: int) -> None:
    from calsync.cli import exit_code_for_results

    assert exit_code_for_results([_result(*r) for r in results]) == code


def test_rule_result_fail_classifies_config_errors() -> None:
    from calsync.engine import RuleResult
    from calsync.errors import AuthError, ConfigError, ForbiddenError, TransientError

    kinds = {}
    for exc in (ConfigError(["x"]), AuthError("x"), ForbiddenError("x"), TransientError("x")):
        r = RuleResult("r")
        r.fail(exc)
        kinds[type(exc).__name__] = r.error_kind
    assert kinds == {
        "ConfigError": "config",
        "AuthError": "auth",
        "ForbiddenError": "other",
        "TransientError": "other",
    }


def _run_config(tmp_path: Path, *parts: str) -> list[str]:
    cfg = tmp_path / "c.toml"
    cfg.write_text("\n".join(parts))
    return ["--config", str(cfg), "--state-dir", str(tmp_path / "state")]


def _dav(name: str, url: str, secret: Path) -> str:
    return CONFIG.format(url=url, user="u", secret=secret).replace("[accounts.dav]", f"[accounts.{name}]")


def _rule(name: str, source: str, target: str) -> str:
    return (
        f'[[rules]]\nname = "{name}"\nsource = {{ account = "{source}", calendar = "Family" }}\n'
        f'target = {{ account = "{target}", calendar = "{name}" }}\n'
    )


def _google(client_secret_file: Path) -> str:
    return (
        f'[google]\nclient_secret_file = "{client_secret_file}"\n\n'
        '[accounts.me]\nprovider = "google"\ncredential = { backend = "env", ref = "CALSYNC_TEST_GOOGLE" }\n'
    )


def _runs(tmp_path: Path) -> dict[str, Any]:
    from calsync.store import Store

    with Store(tmp_path / "state" / "state.db") as store:
        return {r.rule: r for r in store.latest_runs()}


def test_setup_config_error_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A credential file readable by others is a configuration error, as in `calsync calendars`."""
    from tests.fakes.google import FakeGoogle

    _patch_google(monkeypatch, FakeGoogle())
    loose = secret_file(tmp_path / "loose", "pw")
    loose.chmod(0o644)
    args = _run_config(
        tmp_path, _google(tmp_path / "unused.json"), _dav("dav", CLOSED, loose), _rule("r", "me", "dav")
    )
    code, text = run([*args, "run"])
    assert code == EXIT_CONFIG and "rule=r status=failed" in text and "0600" in text
    assert run(["--config", args[1], "calendars", "dav"])[0] == EXIT_CONFIG  # the same verdict


def test_missing_google_client_secret_file_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALSYNC_TEST_GOOGLE", '{"refresh_token": "rt"}')
    secret = secret_file(tmp_path / "pw", "pw")
    args = _run_config(
        tmp_path, _google(tmp_path / "missing.json"), _dav("dav", CLOSED, secret), _rule("r", "me", "dav")
    )
    code, text = run([*args, "run"])
    assert code == EXIT_CONFIG and "client secret file not found" in text


def test_auth_beats_config_beats_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.fakes.google import FakeGoogle

    no_backoff(monkeypatch)
    fake = FakeGoogle()
    fake.add_calendar("family@x", "Family")
    _patch_google(monkeypatch, fake)
    secret = secret_file(tmp_path / "pw", "pw")
    loose = secret_file(tmp_path / "loose", "pw")
    loose.chmod(0o644)
    accounts = [
        _google(tmp_path / "unused.json"),
        _dav("down", CLOSED, secret),  # unreachable: the rule fails ("other")
        _dav("loose", CLOSED, loose),  # setup ConfigError
        _dav("gone", CLOSED, tmp_path / "missing-secret"),  # setup AuthError
    ]
    failed_only = _run_config(tmp_path, *accounts, _rule("down", "me", "down"))
    assert run([*failed_only, "run"])[0] == EXIT_FAILED
    with_config = _run_config(tmp_path, *accounts, _rule("down", "me", "down"), _rule("cfg", "me", "loose"))
    assert run([*with_config, "run"])[0] == EXIT_CONFIG
    with_auth = _run_config(
        tmp_path,
        *accounts,
        _rule("down", "me", "down"),
        _rule("cfg", "me", "loose"),
        _rule("unauthed", "me", "gone"),
    )
    assert run([*with_auth, "run"])[0] == EXIT_AUTH


def test_skipped_rules_record_the_cycle_request_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fakes.google import FakeGoogle

    no_backoff(monkeypatch)
    fake = FakeGoogle()
    fake.add_calendar("family@x", "Family")
    _patch_google(monkeypatch, fake)
    monkeypatch.setenv("CALSYNC_TEST_GOOGLE", '{"refresh_token": "rt"}')
    secret = secret_file(tmp_path / "pw", "pw")
    args = _run_config(
        tmp_path,
        _google(tmp_path / "unused.json"),
        _dav("dav", CLOSED, secret),
        _dav("gone", CLOSED, tmp_path / "missing-secret"),
        _rule("live", "me", "dav"),
        _rule("skipped", "me", "gone"),
    )
    code, text = run([*args, "run"])
    assert code == EXIT_AUTH and "rule=live status=failed" in text
    runs = _runs(tmp_path)
    assert runs["live"].requests > 0
    assert runs["skipped"].requests == runs["live"].requests


def _patch_google(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    from collections.abc import Mapping

    from calsync import wiring
    from calsync.http import make_client
    from calsync.sources.google import GoogleSource
    from calsync.sources.google_auth import StaticToken
    from tests.fakes.google import BASE_URL

    real = wiring.build_adapters

    async def build(config: Any, names: set[str], *, environ: Mapping[str, str] | None = None) -> Any:
        google = {n for n in names if config.accounts[n].provider == "google"}
        adapters = await real(config, names - google)
        for name in google:
            client = make_client(adapters.log, transport=fake.transport())
            adapters.clients.append(client)
            adapters.sources[name] = GoogleSource(client, StaticToken("t"), base_url=BASE_URL)
        return adapters

    monkeypatch.setattr(wiring, "build_adapters", build)


@pytest.mark.parametrize(
    "exc",
    [
        __import__("sqlite3").OperationalError("disk I/O error"),
        PermissionError(13, "Permission denied"),
        __import__("httpx").DecodingError("bad gzip"),
    ],
    ids=["sqlite", "oserror", "httpx"],
)
def test_escaped_infrastructure_errors_exit_1_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    from calsync import store

    def boom(*_: object, **__: object) -> None:
        raise exc

    monkeypatch.setattr(store, "Store", boom)
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url=CLOSED, user="u", secret=tmp_path / "pw"))
    code, text = run(["--config", str(cfg), "--state-dir", str(tmp_path / "s"), "run"])
    assert code == EXIT_FAILED
    assert text == f"error: {type(exc).__name__}: {exc}\n"


def test_unrelated_exceptions_still_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from calsync import store

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(store, "Store", boom)
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url=CLOSED, user="u", secret=tmp_path / "pw"))
    with pytest.raises(RuntimeError, match="a bug"):
        run(["--config", str(cfg), "--state-dir", str(tmp_path / "s"), "run"])


def test_auth_google_browser_failure_keeps_existing_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from calsync import cli
    from calsync.errors import AuthError

    secret = tmp_path / "google-cred"
    secret.write_text('{"refresh_token": "old"}')
    secret.chmod(0o600)
    client = tmp_path / "client.json"
    client.write_text('{"installed": {"client_id": "id", "client_secret": "sec"}}')
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[google]\nclient_secret_file = "{client}"\n'
        f'[accounts.g]\nprovider = "google"\ncredential = {{ backend = "file", ref = "{secret}" }}\n'
    )

    async def refused(config: object) -> str:
        raise AuthError("Google sign-in was not completed: access_denied")

    monkeypatch.setattr(cli, "_google_browser_sign_in", refused)
    code, text = run(["--config", str(cfg), "auth", "g"])
    assert code == EXIT_AUTH and "sign-in failed" in text and "access_denied" in text
    assert secret.read_text() == '{"refresh_token": "old"}'


# ----- read-only views: status, upcoming, runs, changes ------------------------------

VIEWS_CONFIG = """
[google]
client_secret_file = "unused.json"

[accounts.me]
provider = "google"
credential = { backend = "env", ref = "CALSYNC_TEST_UNSET" }

[accounts.dav]
provider = "caldav"
server_url = "https://dav.example.com/"
username = "u"
credential = { backend = "env", ref = "CALSYNC_TEST_UNSET" }

[[rules]]
name = "family"
alarms = ["-10m", "0m"]
source = { account = "me", calendar = "Family" }
target = { account = "dav", calendar = "Family", create_if_missing = true }
"""


@pytest.fixture
def synced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Global CLI args for a config plus a state directory after one real cycle at harness NOW."""
    import anyio

    from calsync.config import load_config
    from tests.factories import google_event
    from tests.fakes.google import FakeGoogle
    from tests.harness import GCAL, NOW, build_fake

    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG)
    state = tmp_path / "state"
    state.mkdir()
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(
        GCAL,
        google_event(
            "standup",
            "Standup",
            "2026-10-23T18:00:00+08:00",
            "2026-10-23T18:15:00+08:00",
            recurrence=["RRULE:FREQ=DAILY;COUNT=10"],
        ),
    )
    fake.put_event(
        GCAL, google_event("lunch", "Lunch\nbreak", "2026-10-25T12:00:00+08:00", "2026-10-25T13:00:00+08:00")
    )
    holiday = google_event("holiday", "Holiday", "", "")
    holiday.update(start={"date": "2026-11-05"}, end={"date": "2026-11-06"})
    fake.put_event(GCAL, holiday)
    h = build_fake(state, fake, load_config(cfg))
    anyio.run(h.engine.cycle)
    h.store.close()
    monkeypatch.setattr("calsync.cli._now", lambda: NOW)
    return ["--config", str(cfg), "--state-dir", str(state)]


def _local_hhmm(iso: str) -> str:
    from datetime import datetime

    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")


def test_status_json_has_the_exact_top_level_keys(synced: list[str]) -> None:
    import json

    code, text = run([*synced, "status", "--json"])
    assert code == EXIT_OK
    view = json.loads(text)
    assert list(view) == ["generated_at", "schema", "summary", "accounts", "rules", "attention", "daemon"]
    assert view["summary"]["health"] == "ok" and view["rules"][0]["copies"] == 3
    assert text == json.dumps(view, ensure_ascii=False, indent=2) + "\n"


def test_status_text(synced: list[str]) -> None:
    code, text = run([*synced, "status"])
    assert code == EXIT_OK
    assert text == "rule=family health=ok last_sync=2026-10-24T08:00:00+00:00 copies=3\n"


def test_status_text_lists_attention_items(synced: list[str], tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from calsync.store import RunRecord, Store

    at = datetime(2026, 10, 24, 8, 1, tzinfo=UTC)
    with Store(tmp_path / "state" / "state.db") as store:
        store.record_runs(
            [RunRecord("family", at, at, "failed", 0, 0, 0, 0, 1, "Google API 401", error_kind="auth")]
        )
    code, text = run([*synced, "status"])
    assert code == EXIT_OK
    assert text == (
        "rule=family health=auth last_sync=2026-10-24T08:00:00+00:00 copies=3\n"
        "  auth: The me account needs to be connected again. Run: calsync auth me\n"
    )


def test_upcoming_json_48_hours(synced: list[str]) -> None:
    import json

    code, text = run([*synced, "upcoming", "--json", "--hours", "48"])
    assert code == EXIT_OK
    view = json.loads(text)
    assert view["hours"] == 48
    assert [(e["title"], e["start"], e["delivery"]) for e in view["events"]] == [
        ("Standup", "2026-10-24T18:00:00+08:00", "delivered"),
        ("Lunch\nbreak", "2026-10-25T12:00:00+08:00", "delivered"),
        ("Standup", "2026-10-25T18:00:00+08:00", "delivered"),
    ]
    assert view["events"][0]["alarms_minutes_before"] == [10, 0]


def test_upcoming_text(synced: list[str]) -> None:
    code, text = run([*synced, "upcoming", "--rule", "family"])
    assert code == EXIT_OK
    assert text == (
        f"{_local_hhmm('2026-10-24T18:00:00+08:00')}  Standup  [Family]  alarms=10,0  delivered\n"
        f"{_local_hhmm('2026-10-25T12:00:00+08:00')}  Lunch break  [Family]  alarms=10,0  delivered\n"
    )


def test_upcoming_text_all_day_and_empty(synced: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    monkeypatch.setattr("calsync.cli._now", lambda: datetime(2026, 11, 4, 12, tzinfo=UTC))
    assert run([*synced, "upcoming"]) == (EXIT_OK, "all day  Holiday  [Family]  alarms=10,0  delivered\n")
    monkeypatch.setattr("calsync.cli._now", lambda: datetime(2030, 1, 1, tzinfo=UTC))
    assert run([*synced, "upcoming"]) == (EXIT_OK, "no events in the next 24 hours\n")


def test_runs_text_and_json(synced: list[str]) -> None:
    import json

    code, text = run([*synced, "runs"])
    assert code == EXIT_OK
    assert text == (
        "1 2026-10-24T08:00:00+00:00 rule=family status=ok created=3 updated=0 deleted=0 failed=0 "
        "duration_ms=0\n"
    )
    code, text = run([*synced, "runs", "--json", "--limit", "1", "--rule", "family"])
    assert code == EXIT_OK and [r["id"] for r in json.loads(text)["runs"]] == [1]


def test_changes_text_and_json(synced: list[str]) -> None:
    import json

    from calsync.naming import object_id
    from tests.harness import GCAL

    code, text = run([*synced, "changes"])
    assert code == EXIT_OK
    lines = text.splitlines()
    assert len(lines) == 3
    assert (
        f"2026-10-24T08:00:00+00:00 rule=family created Lunch break object={object_id('family', GCAL, 'lunch')}"
        in lines
    )
    code, text = run([*synced, "changes", "--json", "--since", "2026-10-24T09:00:00+00:00"])
    assert code == EXIT_OK and json.loads(text) == {"changes": []}
    code, text = run([*synced, "changes", "--json", "--limit", "1"])
    assert len(json.loads(text)["changes"]) == 1


def test_view_commands_without_state_exit_1(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG)
    for command in (["status"], ["upcoming", "--json"], ["runs"], ["changes"]):
        code, text = run(["--config", str(cfg), "--state-dir", str(tmp_path / "none"), *command])
        assert (code, text) == (EXIT_FAILED, "no state yet: run `calsync run` or start the daemon\n")
    assert not (tmp_path / "none").exists()


def test_view_commands_reject_unknown_rules_and_bad_config(synced: list[str], tmp_path: Path) -> None:
    code, text = run([*synced, "upcoming", "--rule", "nope"])
    assert code == EXIT_CONFIG and "unknown rule" in text
    code, text = run(["--config", str(tmp_path / "missing.toml"), "status"])
    assert code == EXIT_CONFIG and "config file not found" in text


def test_history_commands_accept_a_removed_rule(synced: list[str], tmp_path: Path) -> None:
    import json
    from datetime import UTC, datetime

    from calsync.store import ChangeRecord, RunRecord, Store

    at = datetime(2026, 10, 24, 8, 1, tzinfo=UTC)
    with Store(tmp_path / "state" / "state.db") as store:
        store.record_runs(
            [RunRecord("retired", at, at, "ok", 1, 0, 0, 0, 1, None)],
            [ChangeRecord("retired", "o1", "created", at, "Standup")],
        )
    code, text = run([*synced, "runs", "--rule", "retired", "--json"])
    assert code == EXIT_OK and [r["rule"] for r in json.loads(text)["runs"]] == ["retired"]
    code, text = run([*synced, "changes", "--rule", "retired", "--json"])
    assert code == EXIT_OK and [c["object_id"] for c in json.loads(text)["changes"]] == ["o1"]
    code, text = run([*synced, "runs", "--rule", "nope"])
    assert (code, text) == (EXIT_OK, "")


def test_view_commands_report_an_unreadable_database(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG)
    state = tmp_path / "state"
    state.mkdir()
    (state / "state.db").write_bytes(b"not a database, just some text " * 64)
    for command in (["status"], ["upcoming"], ["runs"], ["changes", "--json"]):
        code, text = run(["--config", str(cfg), "--state-dir", str(state), *command])
        assert (code, text) == (EXIT_FAILED, "error: the state database could not be read: DatabaseError\n")


def test_light_commands_import_no_heavy_modules(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG)
    script = (
        "import io, sys\n"
        "from calsync.cli import main\n"
        "assert main(sys.argv[1:], out=io.StringIO()) == 0\n"
        "heavy = sorted(m for m in ('starlette', 'uvicorn', 'httpx', 'icalendar') if m in sys.modules)\n"
        "print(','.join(heavy))\n"
    )
    for argv in (
        ["--version"],
        ["--config", str(cfg), "config", "check"],
        ["--config", str(cfg), "--state-dir", str(tmp_path / "s"), "web", "pair"],
    ):
        done = subprocess.run(
            [sys.executable, "-c", script, *argv], capture_output=True, text=True, check=True
        )
        assert done.stdout == "\n", (argv, done.stdout, done.stderr)


# ----- daemon -------------------------------------------------------------------


def test_daemon_without_rules_exits_2(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(CONFIG.format(url="https://dav.example.com/", user="u", secret=tmp_path / "pw"))
    state = tmp_path / "s"
    code, text = run(["--config", str(cfg), "--state-dir", str(state), "daemon"])
    assert (code, text) == (EXIT_CONFIG, "no rules configured: nothing to sync\n")
    assert not state.exists()


def test_daemon_refuses_a_held_lock(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG)
    state = tmp_path / "s"
    with RunLock(state / "run.lock"):
        code, text = run(["--config", str(cfg), "--state-dir", str(state), "daemon"])
    assert code == EXIT_RUNNING and "holds the lock" in text
    assert "trigger" not in text  # the daemon itself was refused: triggering it means nothing
    assert not (state / "state.db").exists()


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


DAEMON_SCRIPT = "import sys\nfrom calsync.cli import main\nsys.exit(main(sys.argv[1:]))\n"


def _daemon_process(
    tmp_path: Path, state: Path, *extra: str, port: int | None = None
) -> "subprocess.Popen[str]":
    """``calsync daemon`` with a config in ``tmp_path`` and the state (and its socket) in
    ``state``, which must be short: a unix socket path is limited to 104 bytes on macOS."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG)  # env credentials that are never set: no network, no secret read
    # The sealed environment (HOME, CALSYNC_CONFIG / CALSYNC_STATE_DIR, the fail keyring, no
    # proxy) reaches the child too; only the developer's CALSYNC_DEBUG is dropped.
    env = {k: v for k, v in os.environ.items() if k != "CALSYNC_DEBUG"}
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            DAEMON_SCRIPT,
            "--config",
            str(cfg),
            "--state-dir",
            str(state),
            "daemon",
            "--listen",
            f"127.0.0.1:{port or _free_port()}",  # never the default port
            *extra,
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(condition: "Any", proc: "subprocess.Popen[str]") -> None:
    deadline = time.monotonic() + 20
    while not condition():
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            out, err = proc.communicate()
            pytest.fail(f"daemon did not get there: {out}{err}")
        time.sleep(0.05)


def _run_count(db: Path) -> int:
    from calsync.views import StateReader

    if not db.exists():
        return 0
    with StateReader(db) as reader:
        return len(reader.runs())


def _serving(state: Path) -> bool:
    """The daemon's socket accepts connections."""
    import socket

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        try:
            probe.connect(str(state / "calsync.sock"))
        except OSError:
            return False
    return True


def test_daemon_signals_and_trigger(tmp_path: Path, short_dir: Path) -> None:
    state = short_dir / "s"
    db = state / "state.db"
    proc = _daemon_process(tmp_path, state)
    try:
        _wait_for(lambda: _run_count(db) >= 1, proc)  # the first cycle starts at once
        proc.send_signal(signal.SIGUSR1)  # a cycle now, despite the backoff of the failed rule
        _wait_for(lambda: _run_count(db) >= 2, proc)
        _wait_for(lambda: _serving(state), proc)
        assert run(["--state-dir", str(state), "trigger"]) == (EXIT_OK, "ok: cycle requested\n")
        _wait_for(lambda: _run_count(db) >= 3, proc)
        assert run(["--state-dir", str(state), "trigger", "--rule", "family"]) == (
            EXIT_OK,
            "ok: cycle requested\n",
        )
        _wait_for(lambda: _run_count(db) >= 4, proc)
        assert run(["--state-dir", str(state), "trigger", "--rule", "nope"]) == (
            EXIT_CONFIG,
            "unknown rule(s): nope\n",
        )
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == EXIT_OK and out == ""
    assert err.count("INFO calsync.daemon: rule=family status=failed ") == 4
    assert "error_kind=auth" in err and "Traceback" not in err
    logged = [line for line in err.splitlines() if "web UI on http://127.0.0.1:" not in line]
    assert "http" not in "\n".join(logged).replace("httpx", "")  # no request URL in the log
    with RunLock(state / "run.lock"):  # released
        pass
    assert not (state / "calsync.sock").exists()


def test_daemon_no_sync_serves_and_stops_on_sigint(tmp_path: Path, short_dir: Path) -> None:
    state = short_dir / "s"
    import httpx

    port = _free_port()
    proc = _daemon_process(tmp_path, state, "--no-sync", port=port)
    try:
        _wait_for(lambda: _serving(state), proc)
        assert run(["--state-dir", str(state), "trigger"]) == (
            EXIT_FAILED,
            "the daemon runs with --no-sync\n",
        )
        unpaired = httpx.get(f"http://127.0.0.1:{port}/api/status", trust_env=False)  # the web UI, TCP
        assert unpaired.status_code == 401 and unpaired.json()["error"]["kind"] == "unpaired"
        proc.send_signal(signal.SIGINT)  # reaches the daemon's own handler, not uvicorn's
        _, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == EXIT_OK and "Traceback" not in err and "daemon stopped" in err
    assert f"web UI on http://127.0.0.1:{port}/" in err and "/api/status" not in err  # no access log
    assert sorted(p.name for p in state.iterdir()) == []  # no lock, no state database; socket removed


def test_daemon_with_its_port_in_use_exits_2_and_says_why(tmp_path: Path, short_dir: Path) -> None:
    import socket

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        proc = _daemon_process(tmp_path, short_dir / "s", port=port)  # with sync, as under launchd
        out, err = proc.communicate(timeout=20)
    finally:
        blocker.close()
    assert proc.returncode == EXIT_CONFIG
    assert out == (
        "configuration error:\n"
        f'  - cannot listen on 127.0.0.1:{port}: address already in use; set [web] listen to another port or "off"\n'
    )
    assert "Traceback" not in err and "rule=" not in err  # no cycle ran
    with RunLock(short_dir / "s" / "run.lock"):  # released
        pass


class _RecordingDaemon:
    listens: ClassVar[list[tuple[str, int] | None]] = []

    def __init__(self, config: Any, state_dir: Path, *, sync: bool, listen: Any, config_path: Path) -> None:
        self.listens.append(listen)

    def snapshot(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(pid=os.getpid())

    def request_cycle(self, rule: str | None = None) -> bool:
        return False

    async def run(self, stop: Any) -> None:
        return None


@pytest.mark.parametrize(
    ("web", "args", "listen"),
    [
        ("", [], ("127.0.0.1", 8766)),
        ('[web]\nlisten = "off"\n', [], None),
        ("", ["--no-web"], None),
        ('[web]\nlisten = "off"\n', ["--listen", "127.0.0.1:9100"], ("127.0.0.1", 9100)),
    ],
)
def test_daemon_web_listener_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, web: str, args: list[str], listen: Any
) -> None:
    from calsync import cli
    from calsync import daemon as daemon_module

    monkeypatch.setattr(daemon_module, "Daemon", _RecordingDaemon)
    monkeypatch.setattr(cli, "_configure_daemon_logging", lambda: None)  # leave global logging alone
    monkeypatch.setattr(_RecordingDaemon, "listens", [])
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG + web)
    code, _ = run(["--config", str(cfg), "--state-dir", str(tmp_path / "s"), "daemon", "--no-sync", *args])
    assert code == EXIT_OK and _RecordingDaemon.listens == [listen]


class _FailingDaemon(_RecordingDaemon):
    async def run(self, stop: Any) -> None:
        raise RuntimeError("the API server stopped at https://example.com/x")


@pytest.mark.parametrize("debug", [False, True])
def test_daemon_service_failure_is_a_type_only_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debug: bool
) -> None:
    from calsync import cli
    from calsync import daemon as daemon_module

    monkeypatch.setattr(daemon_module, "Daemon", _FailingDaemon)
    monkeypatch.setattr(cli, "_configure_daemon_logging", lambda: None)  # leave global logging alone
    monkeypatch.setattr(_RecordingDaemon, "listens", [])
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG)
    argv = ["--config", str(cfg), "--state-dir", str(tmp_path / "s"), "daemon", "--no-sync"]
    if debug:
        monkeypatch.setenv("CALSYNC_DEBUG", "1")
        with pytest.raises(RuntimeError, match="the API server stopped"):  # the traceback, on request
            run(argv)
    else:
        assert run(argv) == (EXIT_FAILED, "error: RuntimeError\n")


def test_daemon_no_web_and_listen_exclude_each_other(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG)
    with pytest.raises(SystemExit) as info:
        run(["--config", str(cfg), "daemon", "--no-web", "--listen", "127.0.0.1:9100"])
    assert info.value.code == 2


def test_trigger_without_a_daemon_runs_here(short_dir: Path) -> None:
    """No socket, or one nobody listens on: nothing to hand the cycle to, so it runs here (and
    needs the config; ``tests.test_trigger`` runs real cycles)."""
    import socket

    missing = (EXIT_CONFIG, f"configuration error:\n  - config file not found: {short_dir / 'none.toml'}\n")
    argv = ["--config", str(short_dir / "none.toml"), "--state-dir"]
    assert run([*argv, str(short_dir / "none"), "trigger"]) == missing  # no socket
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(short_dir / "calsync.sock"))  # a socket file nobody listens on: refused
    dead.close()
    assert run([*argv, str(short_dir), "trigger"]) == missing
    assert not (short_dir / "none").exists()


def test_trigger_reports_a_socket_it_cannot_reach(short_dir: Path) -> None:
    import socket

    listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listening.bind(str(short_dir / "calsync.sock"))
        listening.listen(1)
        (short_dir / "calsync.sock").chmod(0)  # connect needs write permission on the socket
        for wait in ([], ["--wait"]):
            assert run(["--state-dir", str(short_dir), "trigger", *wait]) == (
                EXIT_FAILED,
                "error: cannot connect to the daemon socket (PermissionError)\n",
            )
    finally:
        listening.close()


def test_a_busy_socket_has_its_own_message_and_exit_4(short_dir: Path) -> None:
    from calsync.cli import _describe, exit_code_for  # pyright: ignore[reportPrivateUsage]
    from calsync.lock import DaemonServing

    exc = DaemonServing(short_dir / "calsync.sock")
    assert isinstance(exc, AlreadyRunning) and exc.pid is None
    assert exit_code_for(exc) == EXIT_RUNNING
    text = _describe(exc)
    assert text.startswith(f"another calsync daemon is serving {short_dir / 'calsync.sock'}")
    assert "lock" not in text and "pid" not in text


@pytest.mark.parametrize("debug", [False, True])
def test_daemon_logging_keeps_request_urls_out(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, debug: bool
) -> None:
    import asyncio
    import logging

    import httpx

    from calsync import cli
    from calsync.http import RequestLog, make_client

    if debug:  # CALSYNC_DEBUG opens calsync's own loggers, never httpx's or httpcore's
        monkeypatch.setenv("CALSYNC_DEBUG", "1")
    else:
        monkeypatch.delenv("CALSYNC_DEBUG", raising=False)
    for name in ("httpx", "httpcore"):  # restored at teardown, whatever the daemon setup does
        caplog.set_level(logging.DEBUG, logger=name)
    caplog.set_level(logging.INFO, logger="calsync")
    caplog.set_level(logging.DEBUG)
    cli._configure_daemon_logging()  # pyright: ignore[reportPrivateUsage]
    assert logging.getLogger("calsync").level == (logging.DEBUG if debug else logging.INFO)
    assert logging.getLogger("httpx").level == logging.getLogger("httpcore").level == logging.WARNING

    async def fetch() -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"items": []}))
        async with make_client(RequestLog(), transport=transport) as client:
            response = await client.get(
                "https://www.googleapis.example.com/calendar/v3/calendars/someone%40example.com/events",
                params={"syncToken": "tok123"},
            )
            assert response.status_code == 200

    asyncio.run(fetch())
    text = caplog.text + "".join(r.getMessage() for r in caplog.records)
    for fragment in ("http", "googleapis", "example.com", "/calendar/", "syncToken", "tok123"):
        assert fragment not in text, (fragment, text)


# ----- web -------------------------------------------------------------------------

WARNING = "Anyone with this link can see your calendar sync status. Reset it with `calsync web reset-key`.\n"


@pytest.mark.parametrize(
    ("web", "hosts"),
    [
        ("", ["localhost:8766", "127.0.0.1:8766"]),
        (
            '[web]\nlisten = "0.0.0.0:9000"\nallowed_hosts = ["mac-mini.local"]\n',
            ["localhost:9000", "mac-mini.local:9000"],
        ),
        ('[web]\nlisten = "192.168.1.5:8766"\n', ["localhost:8766", "192.168.1.5:8766"]),
        ('[web]\nlisten = "[fd00::5]:8766"\n', ["localhost:8766", "[fd00::5]:8766"]),
        ('[web]\nlisten = "[::]:8766"\nallowed_hosts = ["localhost"]\n', ["localhost:8766"]),
    ],
)
def test_web_pair_prints_one_link_per_address(tmp_path: Path, web: str, hosts: list[str]) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG + web)
    state = tmp_path / "s"
    code, text = run(["--config", str(cfg), "--state-dir", str(state), "web", "pair"])
    key = (state / "web.key").read_text().strip()
    assert code == EXIT_OK
    assert text == "".join(f"http://{h}/?k={key}\n" for h in hosts) + WARNING
    assert stat.S_IMODE((state / "web.key").stat().st_mode) == 0o600
    assert run(["--config", str(cfg), "--state-dir", str(state), "web", "pair"]) == (code, text)  # same key


def test_web_pair_takes_listen(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG)
    state = tmp_path / "s"
    code, text = run(
        ["--config", str(cfg), "--state-dir", str(state), "web", "pair", "--listen", "0.0.0.0:9001"]
    )
    key = (state / "web.key").read_text().strip()
    assert (code, text) == (EXIT_OK, f"http://localhost:9001/?k={key}\n" + WARNING)
    with pytest.raises(SystemExit) as info:  # argparse: a usage error, exit 2
        run(["--config", str(cfg), "web", "pair", "--listen", "nope"])
    assert info.value.code == EXIT_CONFIG


def test_web_reset_key(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG)
    state = tmp_path / "s"
    run(["--config", str(cfg), "--state-dir", str(state), "web", "pair"])
    old = (state / "web.key").read_text().strip()
    code, text = run(["--config", str(cfg), "--state-dir", str(state), "web", "reset-key"])
    new = (state / "web.key").read_text().strip()
    assert code == EXIT_OK and new != old
    assert text == (
        "ok: the key was replaced; every paired browser has to pair again\n"
        f"http://localhost:8766/?k={new}\nhttp://127.0.0.1:8766/?k={new}\n" + WARNING
    )
    assert stat.S_IMODE((state / "web.key").stat().st_mode) == 0o600


def test_web_pair_refuses_a_key_file_others_can_read(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG)
    state = tmp_path / "s"
    run(["--config", str(cfg), "--state-dir", str(state), "web", "pair"])
    key_file = state / "web.key"
    key_file.chmod(0o644)
    code, text = run(["--config", str(cfg), "--state-dir", str(state), "web", "pair"])
    assert code == EXIT_FAILED
    assert text == (
        f"error: other users can access {key_file} (mode 0644); run `chmod 600 {key_file}`, "
        "or replace the key with `calsync web reset-key`\n"
    )
    code, text = run(["--config", str(cfg), "--state-dir", str(state), "web", "reset-key"])
    assert code == EXIT_OK and stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_daemon_listen_is_validated(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG)
    with pytest.raises(SystemExit) as info:
        run(["--config", str(cfg), "daemon", "--listen", "127.0.0.1:0"])
    assert info.value.code == 2  # argparse: a usage error


WEB_OFF = 'the web UI is off ([web] listen = "off"); set [web] listen to HOST:PORT to use it\n'


def test_web_pair_when_the_web_ui_is_off(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(VIEWS_CONFIG + '[web]\nlisten = "off"\n')
    state = tmp_path / "s"
    assert run(["--config", str(cfg), "--state-dir", str(state), "web", "pair"]) == (EXIT_CONFIG, WEB_OFF)
    assert not (state / "web.key").exists()  # nothing to pair with: no key made
    code, text = run(
        ["--config", str(cfg), "--state-dir", str(state), "web", "pair", "--listen", "127.0.0.1:9100"]
    )
    key = (state / "web.key").read_text().strip()
    assert (code, text) == (
        EXIT_OK,
        f"http://localhost:9100/?k={key}\nhttp://127.0.0.1:9100/?k={key}\n" + WARNING,
    )
    code, text = run(["--config", str(cfg), "--state-dir", str(state), "web", "reset-key"])  # still resets
    assert (code, text) == (
        EXIT_OK,
        "ok: the key was replaced; every paired browser has to pair again\n" + WEB_OFF,
    )
    assert (state / "web.key").read_text().strip() != key


# ----- events and event show ------------------------------------------------------------------

RANGE = ["--from", "2026-10-24T00:00+08:00", "--to", "2026-10-26T00:00+08:00"]


def _local_day(iso: str) -> str:
    from datetime import datetime

    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d")


def test_events_text(synced: list[str]) -> None:
    code, text = run([*synced, "events", *RANGE])
    assert code == EXIT_OK

    def line(start: str, end: str, title: str) -> str:
        return f"{_local_day(start)} {_local_hhmm(start)}–{_local_hhmm(end)}  {title}  [Family → Family: delivered]"

    assert text.splitlines() == [
        line("2026-10-24T18:00:00+08:00", "2026-10-24T18:15:00+08:00", "Standup"),
        line("2026-10-25T12:00:00+08:00", "2026-10-25T13:00:00+08:00", "Lunch break"),
        line("2026-10-25T18:00:00+08:00", "2026-10-25T18:15:00+08:00", "Standup"),
        "(use --json for event ids)",
    ]


def test_events_text_all_day_and_empty(synced: list[str]) -> None:
    code, text = run(
        [*synced, "events", "--from", "2026-11-05T00:00+00:00", "--to", "2026-11-06T00:00+00:00"]
    )
    assert (code, text) == (
        EXIT_OK,
        "2026-11-05 all day  Holiday  [Family → Family: delivered]\n(use --json for event ids)\n",
    )
    code, text = run([*synced, "events", "--from", "2030-01-01", "--to", "2030-01-02"])
    assert (code, text) == (EXIT_OK, "no events\n")


def test_events_json_and_filters(synced: list[str]) -> None:
    import json

    code, text = run([*synced, "events", *RANGE, "--json"])
    assert code == EXIT_OK
    body = json.loads(text)
    assert list(body) == ["from", "to", "schema", "truncated", "events"]
    assert [e["title"] for e in body["events"]] == ["Standup", "Lunch\nbreak", "Standup"]
    code, text = run([*synced, "events", *RANGE, "--json", "--query", "LUNCH", "--rule", "family"])
    assert [e["title"] for e in json.loads(text)["events"]] == ["Lunch\nbreak"]
    code, text = run([*synced, "events", *RANGE, "--json", "--target", "dav:Family"])
    assert len(json.loads(text)["events"]) == 3


def test_events_defaults_to_a_week_from_today(synced: list[str]) -> None:
    import json
    from datetime import datetime, timedelta

    from tests.harness import NOW

    body = json.loads(run([*synced, "events", "--json"])[1])
    start, end = datetime.fromisoformat(body["from"]), datetime.fromisoformat(body["to"])
    assert start == NOW.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    assert end - start == timedelta(days=7)
    body = json.loads(run([*synced, "events", "--json", "--from", "2026-10-30"])[1])
    assert datetime.fromisoformat(body["to"]) - datetime.fromisoformat(body["from"]) == timedelta(days=7)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--from", "2026-10-01", "--to", "2026-12-03"], "to: at most 62 days after from"),
        (["--from", "2026-10-24", "--to", "2026-10-23"], "to: must not be before from"),
        (["--rule", "nope"], "unknown rule(s): nope"),
        (["--target", "dav:Nowhere"], "no rule syncs into dav:Nowhere"),
        (["--target", "nocolon"], "target: expected ACCOUNT:CALENDAR, got 'nocolon'"),
        (["--from", "2040-01-01"], "from: must be within 10 years of today"),
        (["--from", "9999-12-30"], "from: must be within 10 years of today"),
        (["--from", "2026-10-24", "--to", "9999-12-31T23:59+00:00"], "to: must be within 10 years of today"),
    ],
)
def test_events_refuses_bad_arguments(synced: list[str], argv: list[str], message: str) -> None:
    import json

    assert run([*synced, "events", *argv]) == (EXIT_CONFIG, message + "\n")
    code, text = run([*synced, "events", *argv, "--json"])
    assert code == EXIT_CONFIG and json.loads(text)["error"]["message"] == message


def test_events_refuses_an_unrepresentable_local_time(
    synced: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as info:  # argparse: a usage error, exit 2 (not a traceback)
        run([*synced, "events", "--from", "9999-12-31T23:00"])
    assert info.value.code == EXIT_CONFIG and "expected an ISO 8601 time" in capsys.readouterr().err


def test_upcoming_refuses_hours_beyond_the_calendar(synced: list[str]) -> None:
    assert run([*synced, "upcoming", "--hours", "999999999999"]) == (EXIT_CONFIG, "hours: too far ahead\n")


def test_upcoming_refuses_hours_whose_local_end_is_beyond_the_calendar(
    synced: list[str], local_zone: Any
) -> None:
    from datetime import UTC, datetime

    from tests.harness import NOW

    local_zone("Asia/Shanghai")  # +08:00: the UTC end is representable, the local one is not
    hours = int((datetime(9999, 12, 31, 20, tzinfo=UTC) - NOW).total_seconds() // 3600)
    assert run([*synced, "upcoming", "--hours", str(hours)]) == (EXIT_CONFIG, "hours: too far ahead\n")


def test_span_text_shows_the_end_date_of_a_multi_day_event(local_zone: Any) -> None:
    from datetime import date, datetime, timedelta, timezone

    from calsync.cli import _span_text  # pyright: ignore[reportPrivateUsage]
    from calsync.model import When

    local_zone("Asia/Shanghai")
    plus8 = timezone(timedelta(hours=8))

    def at(d: int, h: int) -> When:
        return When(datetime(2026, 10, d, h, tzinfo=plus8), "Asia/Shanghai")

    assert _span_text(at(28, 18), at(28, 19)) == "2026-10-28 18:00–19:00"
    assert _span_text(at(28, 22), at(29, 1)) == "2026-10-28 22:00 – 2026-10-29 01:00"
    assert _span_text(When(date(2026, 11, 5)), When(date(2026, 11, 6))) == "2026-11-05 all day"
    assert _span_text(When(date(2026, 11, 5)), When(date(2026, 11, 8))) == "2026-11-05 – 2026-11-07 all day"


def test_event_show(synced: list[str]) -> None:
    import json

    events = json.loads(run([*synced, "events", *RANGE, "--json"])[1])["events"]
    lunch = events[1]["id"]
    code, text = run([*synced, "event", "show", lunch, "--json"])
    assert code == EXIT_OK
    body = json.loads(text)
    assert body["id"] == lunch and body["original"]["title"] == "Lunch\nbreak"
    assert body["deliveries"][0]["copy"]["title"] == "Lunch\nbreak"
    code, text = run([*synced, "event", "show", lunch])
    assert code == EXIT_OK
    start, end = "2026-10-25T12:00:00+08:00", "2026-10-25T13:00:00+08:00"
    assert text == (
        "Lunch break\n"
        f"  when: {_local_day(start)} {_local_hhmm(start)}–{_local_hhmm(end)}\n"
        "  source: me / Family\n"
        "  family → dav / Family: delivered at 2026-10-24T08:00:00+00:00\n"
    )


def test_event_show_of_an_unknown_event(synced: list[str]) -> None:
    import json

    assert run([*synced, "event", "show", "bm9wZQ"]) == (EXIT_FAILED, "no such event\n")
    code, text = run([*synced, "event", "show", "bm9wZQ", "--json"])
    assert code == EXIT_FAILED and json.loads(text)["error"]["kind"] == "not_found"


def test_status_json_daemon_is_null_without_a_daemon(synced: list[str]) -> None:
    import json

    assert json.loads(run([*synced, "status", "--json"])[1])["daemon"] is None
