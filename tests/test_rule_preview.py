"""``rule.preview`` (``calsync rule add ... --preview``): what a new rule would do, written nowhere.

The source side comes from the mirror when that calendar is already synced, else from one live
pull with empty state that is thrown away; the target side from the calendar cache."""

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest

from calsync import ops, wiring
from calsync.cli import EXIT_AUTH, EXIT_CONFIG, EXIT_OK, main
from calsync.errors import AuthError, ConfigError
from calsync.store import Store
from calsync.views import StateReader
from tests.factories import google_event
from tests.harness import GCAL, NOW, build_fake, config_for, rule
from tests.test_calendar_cache import Accounts
from tests.test_daemon import World, eventually

pytestmark = pytest.mark.anyio

WORK = "work@group.calendar.google.com"

CONFIG = """\
[google]
client_secret_file = "unused.json"

[accounts.me]
provider = "google"
credential = { backend = "env", ref = "UNUSED" }

[accounts.dav]
provider = "caldav"
server_url = "http://unused/"
username = "u"
credential = { backend = "env", ref = "UNUSED" }

[[rules]]
name = "family"
source = { account = "me", calendar = "Family" }
target = { account = "dav", calendar = "Family", create_if_missing = true }
"""


class Setup:
    """A state directory whose ``family`` rule has synced once (Family is mirrored), a config
    file with that rule, and fake accounts for any adapter the preview builds."""

    def __init__(self, root: Path, accounts: Accounts) -> None:
        self.root = root
        self.accounts = accounts
        g = accounts.google
        g.put_event(
            GCAL,
            google_event(
                "daily",
                "Daily",
                "2026-10-24T10:00:00+08:00",
                "2026-10-24T10:15:00+08:00",
                recurrence=["RRULE:FREQ=DAILY;COUNT=10"],
            ),
        )
        g.put_event(
            GCAL, google_event("lunch", "Lunch", "2026-10-25T12:00:00+08:00", "2026-10-25T13:00:00+08:00")
        )
        g.put_event(
            GCAL, google_event("plan", "Plan", "2026-10-28T10:00:00+08:00", "2026-10-28T11:00:00+08:00")
        )
        g.add_calendar(WORK, "Work")
        g.put_event(WORK, google_event("w1", "W1", "2026-10-26T09:00:00+08:00", "2026-10-26T10:00:00+08:00"))
        g.put_event(WORK, google_event("w2", "W2", "2026-10-27T09:00:00+08:00", "2026-10-27T10:00:00+08:00"))
        self.state = root / "s"
        self.state.mkdir()
        self.config = root / "config.toml"
        self.config.write_text(CONFIG)

    async def sync_family(self) -> None:
        h = build_fake(self.state, self.accounts.google, config_for([rule()]), target=self.accounts.target)
        assert (await h.engine.cycle()).rules["family"].created == 3
        h.store.close()

    def preview(self, **args: Any) -> ops.OpResult:
        with Store(self.state / "state.db") as store:
            ctx = ops.OpContext(
                config_path=self.config, state_dir=self.state, store=store, source="cli", clock=lambda: NOW
            )
            return ops.run_op(ctx, "rule.preview", {"name": "new", **args}, False)

    def mirrored_events(self, calendar: str) -> int:
        with StateReader(self.state / "state.db") as reader:
            return reader.series_count("me", calendar)

    def tokens(self) -> set[str]:
        with StateReader(self.state / "state.db") as reader:
            return set(reader.source_state("me").get("events_tokens", {}))

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = main(["--config", str(self.config), "--state-dir", str(self.state), *argv], out=out)
        return code, out.getvalue()


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
    accounts = Accounts()
    monkeypatch.setattr(wiring, "build_adapters", accounts.build)
    return Setup(tmp_path, accounts)


def unchanged(setup: Setup) -> None:
    assert setup.config.read_text() == CONFIG
    with StateReader(setup.state / "state.db") as reader:
        assert reader.config_changes() == []


def test_preview_from_the_mirror(setup: Setup) -> None:
    asyncio.run(setup.sync_family())
    result = setup.preview(source="me:Family", target="dav:Kids", create_target=True)
    assert result.data == {
        "series": 3,
        "instances_in_window": None,  # the rule syncs everything
        "target_exists": False,
        "target_would_be_created": True,
        "source_mirrored": True,
        "leftover": [],
    }
    assert result.summary == 'would create 3 series in "Kids" (calendar would be created)'
    assert not result.changed
    assert setup.accounts.builds == [{"dav"}]  # the target list, refreshed once; no Google pull
    unchanged(setup)


def test_preview_names_copies_a_removed_line_left_in_the_target(setup: Setup) -> None:
    """``family`` synced 3 copies into dav:Family, then was removed with its copies kept: a new
    line into that calendar would write its own beside them."""
    asyncio.run(setup.sync_family())
    setup.config.write_text(CONFIG.split("[[rules]]", 1)[0])
    result = setup.preview(source="me:Family", target="dav:Family")
    assert result.data["leftover"] == [{"name": "family", "copies": 3}]


def test_preview_does_not_count_the_copies_of_live_lines(setup: Setup) -> None:
    asyncio.run(setup.sync_family())
    result = setup.preview(source="me:Work", target="dav:Family")
    assert result.data["leftover"] == []


def test_preview_counts_the_window(setup: Setup) -> None:
    """Window 1d:3d around NOW (2026-10-24 08:00 UTC): daily on 24-27 (4) and lunch on the 25th."""
    asyncio.run(setup.sync_family())
    result = setup.preview(source="me:Family", target="dav:Family2", window="1d:3d")
    assert (result.data["series"], result.data["instances_in_window"]) == (2, 5)
    assert (result.data["target_exists"], result.data["target_would_be_created"]) == (False, False)
    assert "not found" in result.summary and "--create-target" in result.summary


def test_preview_of_an_existing_target_reads_the_cache(setup: Setup) -> None:
    asyncio.run(setup.sync_family())
    setup.preview(source="me:Work", target="dav:Family2")  # fills the cache
    builds = len(setup.accounts.builds)
    result = setup.preview(source="me:Family", target="dav:Home", name="again")
    assert (result.data["target_exists"], result.data["target_would_be_created"]) == (True, False)
    assert result.summary == 'would create 3 series in "Home" (calendar exists)'
    assert setup.accounts.builds[builds:] == []  # the source mirrored, the target cached


def test_preview_from_a_live_pull_stores_nothing(setup: Setup) -> None:
    asyncio.run(setup.sync_family())
    before = setup.tokens()
    result = setup.preview(source="me:Work", target="dav:Work", create_target=True)
    assert (result.data["series"], result.data["source_mirrored"]) == (2, False)
    assert {"me"} in setup.accounts.builds
    assert setup.mirrored_events(WORK) == 0 and setup.tokens() == before  # pulled, then thrown away
    unchanged(setup)


def test_preview_with_no_state_yet(setup: Setup) -> None:
    result = setup.preview(source="me:Family", target="dav:New", name="first", create_target=True)
    assert (result.data["series"], result.data["source_mirrored"]) == (3, False)
    assert (result.data["target_exists"], result.data["target_would_be_created"]) == (False, True)


def test_preview_refuses_what_rule_add_refuses(setup: Setup) -> None:
    with pytest.raises(ConfigError, match="already syncs"):
        setup.preview(source="me:Family", target="dav:Family")
    with pytest.raises(ConfigError):
        setup.preview(source="nobody:Family", target="dav:Family")
    with pytest.raises(ConfigError, match="not found"):
        setup.preview(source="me:Nonexistent", target="dav:X")


def test_preview_auth_failure_is_reported_not_raised(setup: Setup) -> None:
    setup.accounts.setup_errors["me"] = AuthError("credential file not found: /secret/place")
    result = setup.preview(source="me:Work", target="dav:Work")
    assert result.data["error"] == 'AuthError: account "me" has no usable credential'
    assert (result.data["error_kind"], result.data["error_account"]) == ("auth", "me")
    assert result.data["series"] is None
    assert result.needs_human == {"command": "calsync auth me", "reason": ops.AUTH_REASON}


# ----- the CLI -----------------------------------------------------------------------------


def test_rule_add_preview_prints_and_writes_nothing(setup: Setup) -> None:
    asyncio.run(setup.sync_family())
    code, text = setup.cli(
        "rule", "add", "kids", "--source", "me:Family", "--target", "dav:Kids", "--preview"
    )
    assert code == EXIT_OK
    assert text == 'would create 3 series in "Kids" (calendar not found; add --create-target to create it)\n'
    code, text = setup.cli(
        "rule",
        "add",
        "kids",
        "--source",
        "me:Family",
        "--target",
        "dav:Kids",
        "--create-target",
        "--preview",
        "--json",
    )
    body = json.loads(text)
    assert code == EXIT_OK and body["op"] == "rule.preview" and body["changed"] is False
    assert body["data"]["target_would_be_created"] is True
    unchanged(setup)


def test_rule_add_preview_exit_codes(setup: Setup) -> None:
    code, _ = setup.cli("rule", "add", "kids", "--source", "me:Nonexistent", "--target", "dav:K", "--preview")
    assert code == EXIT_CONFIG
    setup.accounts.setup_errors["me"] = AuthError("x")
    code, text = setup.cli("rule", "add", "kids", "--source", "me:Work", "--target", "dav:K", "--preview")
    assert code == EXIT_AUTH and "calsync auth me" in text


# ----- through the daemon ------------------------------------------------------------------


async def test_preview_through_the_daemon_uses_its_adapters(tmp_path: Path) -> None:
    world = World(block=True)
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG)
    daemon = world.daemon(config_for([rule()]), tmp_path, config_path=config_path)
    task = asyncio.create_task(daemon.run(world.stop))
    try:
        await eventually(lambda: daemon.snapshot().cycles >= 1)
        builds = len(world.builds)
        result = await daemon.run_op(
            "rule.preview", {"name": "w", "source": "me:Work", "target": "dav:Work"}, False, source="ui"
        )
        assert (result.data["series"], result.data["source_mirrored"]) == (1, False)
        mirrored = await daemon.run_op(
            "rule.preview", {"name": "f", "source": "me:Family", "target": "dav:Other"}, False, source="ui"
        )
        assert (mirrored.data["series"], mirrored.data["source_mirrored"]) == (1, True)
        assert world.builds[builds:] == []  # the daemon's own adapters
        with StateReader(tmp_path / "state.db") as reader:
            assert reader.series_count("me", WORK) == 0 and reader.config_changes() == []
        assert config_path.read_text() == CONFIG
    finally:
        world.stop.set()
        await task


# ----- fix round 1 -------------------------------------------------------------------------


class Hanging:
    """A source that never answers (a server that accepted the connection and went quiet)."""

    async def pull(self, refs: Any, state: Any) -> Any:
        await asyncio.Event().wait()

    async def list_calendars(self) -> Any:
        await asyncio.Event().wait()


def test_preview_of_a_source_that_never_answers_times_out(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live pull is bounded (``PREVIEW_TIMEOUT``): under the daemon's lock a hung server would
    otherwise hold every cycle. Reported like any unreachable account, by type only."""
    monkeypatch.setattr(ops, "PREVIEW_TIMEOUT", 0.05)
    real = setup.accounts.build

    async def build(config: Any, names: set[str], **kw: Any) -> wiring.Adapters:
        adapters = await real(config, names, **kw)
        if "me" in adapters.sources:
            adapters.sources["me"] = Hanging()  # type: ignore[assignment]
        return adapters

    monkeypatch.setattr(wiring, "build_adapters", build)
    result = setup.preview(source="me:Work", target="dav:Work")
    assert result.summary == 'could not preview: TimeoutError: account "me" did not answer in time'
    assert (result.data["error_kind"], result.data["error_account"]) == ("other", "me")
    assert result.data["series"] is None and result.needs_human is None
    unchanged(setup)


def account_calendar_rows(setup: Setup) -> int:
    with StateReader(setup.state / "state.db") as reader:
        return len(reader.account_calendars("dav"))


def test_rule_add_preview_dry_run_writes_nothing_not_even_the_calendar_cache(setup: Setup) -> None:
    code, text = setup.cli(
        "rule", "add", "kids", "--source", "me:Family", "--target", "dav:Home", "--preview", "--dry-run"
    )
    assert code == EXIT_OK
    assert text == 'would create 3 series in "Home" (calendar exists)\n'  # the listing was read
    assert account_calendar_rows(setup) == 0
    unchanged(setup)
    code, _ = setup.cli("rule", "add", "kids", "--source", "me:Family", "--target", "dav:Home", "--preview")
    assert code == EXIT_OK and account_calendar_rows(setup) == 1  # without --dry-run: cached


@pytest.mark.parametrize(("op", "expected"), [("rule.add", EXIT_OK), ("rule.preview", EXIT_AUTH)])
def test_only_named_operations_exit_by_their_error_kind(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, op: str, expected: int
) -> None:
    """An ``error_kind`` in another operation's data (a key it may carry for its own reasons) does
    not change the exit code."""
    from calsync import opsclient

    def call(name: str, args: Any, **_: Any) -> ops.OpResult:
        return ops.OpResult(op, False, "s", data={"error_kind": "auth"})

    monkeypatch.setattr(opsclient, "call", call)
    preview = ["--preview"] if op == "rule.preview" else []
    code, _ = setup.cli("rule", "add", "kids", "--source", "me:Family", "--target", "dav:Home", *preview)
    assert code == expected


def test_an_unreachable_account_is_logged_by_type(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    monkeypatch.setattr(ops, "PREVIEW_TIMEOUT", 0.05)
    real = setup.accounts.build

    async def build(config: Any, names: set[str], **kw: Any) -> wiring.Adapters:
        adapters = await real(config, names, **kw)
        if "me" in adapters.sources:
            adapters.sources["me"] = Hanging()  # type: ignore[assignment]
        return adapters

    monkeypatch.setattr(wiring, "build_adapters", build)
    with caplog.at_level(logging.WARNING):
        setup.preview(source="me:Work", target="dav:Work")
    messages = [r.getMessage() for r in caplog.records]
    assert "rule preview: account me unreachable (TimeoutError)" in messages
