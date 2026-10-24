"""`calsync run` end to end: real wiring for CalDAV accounts against Radicale,
Google sources served by FakeGoogle.

``calsync.wiring.build_adapters`` is monkeypatched with a wrapper that hands the
CalDAV accounts to the real function (so credential reading, per-account error
collection and the CalDavTarget wiring are exercised) and builds each Google
source on a fake transport with a static token. The CLI imports
``build_adapters`` inside each command, so the patched attribute is what runs.
"""

import io
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from calsync import wiring
from calsync.cli import EXIT_AUTH, EXIT_FAILED, EXIT_OK, main
from calsync.config import Config, CredentialRef
from calsync.credentials import write_secret
from calsync.http import make_client
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import StaticToken
from tests.factories import google_event
from tests.fakes.google import BASE_URL, FakeGoogle

pytestmark = pytest.mark.e2e

GCAL = "family@group.calendar.google.com"
TZ = ZoneInfo("Asia/Shanghai")

Transport = Callable[[], httpx.AsyncBaseTransport]


def run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, out=out, stdin=io.StringIO(""))
    return code, out.getvalue()


def soon(days: int, minutes: int = 0) -> str:
    base = datetime.now(TZ).replace(hour=9, minute=0, second=0, microsecond=0)
    return (base + timedelta(days=days, minutes=minutes)).isoformat()


def seeded(n: int = 3) -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for i in range(n):
        fake.put_event(GCAL, google_event(f"e{i}", f"Event {i}", soon(i + 1), soon(i + 1, 30)))
    return fake


def deny() -> httpx.AsyncBaseTransport:
    return httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "revoked"}))


def patch_google(monkeypatch: pytest.MonkeyPatch, transports: Mapping[str, Transport]) -> None:
    real = wiring.build_adapters

    async def build(
        config: Config, names: set[str], *, environ: Mapping[str, str] = os.environ
    ) -> wiring.Adapters:
        google = {n for n in names if config.accounts[n].provider == "google"}
        adapters = await real(config, names - google, environ=environ)
        for name in sorted(google):
            client = make_client(adapters.log, transport=transports[name]())  # fresh per run
            adapters.clients.append(client)
            adapters.sources[name] = GoogleSource(client, StaticToken("t"), base_url=BASE_URL)
        return adapters

    monkeypatch.setattr(wiring, "build_adapters", build)


def dav_account(name: str, url: str, user: str, secret: Path) -> str:
    return (
        f'[accounts.{name}]\nprovider = "caldav"\nserver_url = "{url}"\nusername = "{user}"\n'
        f'credential = {{ backend = "file", ref = "{secret}" }}\n'
    )


def google_account(name: str) -> str:
    return f'[accounts.{name}]\nprovider = "google"\ncredential = {{ backend = "env", ref = "CALSYNC_UNUSED" }}\n'


def rule(name: str, source: str, target: str, calendar: str = "Family") -> str:
    return (
        f'[[rules]]\nname = "{name}"\nsource = {{ account = "{source}", calendar = "Family" }}\n'
        f'target = {{ account = "{target}", calendar = "{calendar}", create_if_missing = true }}\n'
    )


def write_config(tmp_path: Path, *parts: str) -> list[str]:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[google]\nclient_secret_file = "unused.json"\n\n' + "\n".join(parts))
    return ["--config", str(cfg), "--state-dir", str(tmp_path / "state")]


def password(path: Path) -> Path:
    write_secret(CredentialRef("file", str(path)), "pw")
    return path


@pytest.fixture
def one_rule(tmp_path: Path, radicale_url: str, dav_user: str) -> list[str]:
    return write_config(
        tmp_path,
        google_account("me"),
        dav_account("dav", radicale_url, dav_user, password(tmp_path / "pw")),
        rule("family", "me", "dav"),
    )


def test_first_run_creates_events(monkeypatch: pytest.MonkeyPatch, one_rule: list[str]) -> None:
    patch_google(monkeypatch, {"me": seeded().transport})
    code, text = run([*one_rule, "run"])
    assert code == EXIT_OK
    assert "rule=family status=ok created=3 updated=0 deleted=0 failed=0\n" in text
    code, text = run([*one_rule, "run"])  # idle second cycle
    assert code == EXIT_OK and "rule=family status=ok created=0 updated=0 deleted=0 failed=0\n" in text


def test_dry_run_prints_the_plan_without_writing(
    monkeypatch: pytest.MonkeyPatch, one_rule: list[str]
) -> None:
    fake = seeded()
    patch_google(monkeypatch, {"me": fake.transport})
    assert run([*one_rule, "run"])[0] == EXIT_OK
    fake.put_event(GCAL, google_event("new", "Brand new", soon(5), soon(5, 30)))
    code, text = run([*one_rule, "run", "--dry-run"])
    assert code == EXIT_OK and "  + Brand new @ " in text and "created=0" in text
    code, text = run([*one_rule, "run"])
    assert code == EXIT_OK and "created=1" in text  # the dry run wrote nothing


def test_blocked_mass_delete_exits_1_and_lists_titles(
    monkeypatch: pytest.MonkeyPatch, one_rule: list[str]
) -> None:
    fake = seeded()
    patch_google(monkeypatch, {"me": fake.transport})
    assert run([*one_rule, "run"])[0] == EXIT_OK
    for i in range(3):
        fake.cancel_event(GCAL, f"e{i}")
    code, text = run([*one_rule, "run"])
    assert code == EXIT_FAILED and "status=blocked" in text
    for i in range(3):
        assert f"  - Event {i}\n" in text
    assert "--allow-deletes" in text and "(titles incomplete)" not in text
    code, text = run([*one_rule, "run", "--allow-deletes"])
    assert code == EXIT_OK and "deleted=3" in text


def test_source_auth_failure_exits_3(monkeypatch: pytest.MonkeyPatch, one_rule: list[str]) -> None:
    patch_google(monkeypatch, {"me": deny})
    code, text = run([*one_rule, "run"])
    assert code == EXIT_AUTH and "status=failed" in text and "AuthError" in text


def test_unreadable_secret_fails_only_its_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, radicale_url: str, dav_user: str
) -> None:
    patch_google(monkeypatch, {"me": seeded().transport})
    args = write_config(
        tmp_path,
        google_account("me"),
        dav_account("dav", radicale_url, dav_user, password(tmp_path / "pw")),
        dav_account("dav2", radicale_url, dav_user, tmp_path / "missing"),
        rule("good", "me", "dav", calendar="Good"),
        rule("bad", "me", "dav2", calendar="Bad"),
    )
    code, text = run([*args, "run"])
    assert code == EXIT_AUTH
    assert "rule=good status=ok created=3" in text
    assert "rule=bad status=failed" in text and "credential file not found" in text
    assert str(tmp_path / "missing") in text and "pw\n" not in text
    from calsync.store import Store

    with Store(tmp_path / "state" / "state.db") as store:
        runs = {r.rule: r for r in store.latest_runs()}
    assert runs["good"].status == "ok" and runs["bad"].status == "failed"
    assert runs["bad"].error is not None and "credential file not found" in runs["bad"].error
