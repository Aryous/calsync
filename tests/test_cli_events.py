"""`calsync event new|edit|delete|apply` and `calsync writes`, in-process and over the socket."""

import json
from pathlib import Path
from typing import Any

import anyio.to_thread
import pytest

from calsync import wiring
from calsync.cli import EXIT_CONFIG, EXIT_FAILED, EXIT_OK
from tests.harness import GCAL
from tests.test_cli import run
from tests.test_daemon import World, eventually
from tests.test_daemon_reload import TEXT
from tests.test_trigger import Running


class Here:
    """A config file and a state directory with no daemon; the accounts are ``World``'s fakes."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.world = World()
        monkeypatch.setattr(wiring, "build_adapters", self.world.factory)
        self.path = root / "config.toml"
        self.path.write_text(TEXT)
        self.state = root / "s"
        assert self.cli("trigger")[0] == EXIT_OK  # the first sync: the mirror and the copies

    def cli(self, *argv: str, stdin: str = "") -> tuple[int, str]:
        return run(["--config", str(self.path), "--state-dir", str(self.state), *argv], stdin)

    def json(self, *argv: str, stdin: str = "") -> tuple[int, Any]:
        code, out = self.cli(*argv, "--json", stdin=stdin)
        return code, json.loads(out)

    def event(self, title: str, day: str = "2026-10-25") -> dict[str, Any]:
        _, body = self.json("events", "--from", day)
        return next(e for e in body["events"] if e["title"] == title)


def test_event_edit_and_its_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = Here(tmp_path, monkeypatch)
    lunch = here.event("Lunch")
    assert lunch["editable"]["ok"] is True and lunch["source_id"] == "lunch"
    code, dry = here.json("event", "edit", lunch["id"], "--title", "Brunch", "--dry-run")
    assert code == EXIT_OK and dry["data"]["would_change"] is True and here.world.fake.writes == []
    code, out = here.cli(
        "event", "edit", lunch["id"], "--title", "Brunch", "--expect-updated", lunch["updated"]
    )
    assert code == EXIT_OK, out
    assert (
        out.splitlines()[0]
        == 'the event in a calendar of account "me" was changed in Google; the copies were synced here'
    )
    assert here.world.fake.event(GCAL, "lunch")["summary"] == "Brunch"
    assert here.event("Brunch")["deliveries"][0]["state"] == "delivered"


def test_a_stale_edit_exits_1_with_its_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = Here(tmp_path, monkeypatch)
    lunch = here.event("Lunch")
    here.world.fake.touch(GCAL, "lunch", location="Elsewhere")
    code, body = here.json("event", "edit", lunch["id"], "--title", "x", "--expect-updated", lunch["updated"])
    assert code == EXIT_FAILED and body["error"]["kind"] == "event_conflict"


def test_event_new_and_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = Here(tmp_path, monkeypatch)
    code, made = here.json(
        "event", "new", "--account", "me", "--calendar", "Family", "--title", "Dentist",
        "--start", "2026-10-27T10:00", "--end", "2026-10-27T10:30", "--alarms", "30",
    )  # fmt: skip
    assert code == EXIT_OK, made
    assert made["changed"] and made["data"]["event"]["alarms"] == [30]
    assert here.event("Dentist", "2026-10-27")["id"] == made["data"]["event_id"]
    code, gone = here.json("event", "delete", made["data"]["event_id"])
    assert code == EXIT_OK and gone["data"]["scope"] == "single"
    with pytest.raises(SystemExit):  # argparse: --end is required
        here.cli(
            "event", "new", "--account", "me", "--calendar", "Family", "--title", "x", "--start", "2026-10-27"
        )


def test_event_apply_from_a_file_and_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = Here(tmp_path, monkeypatch)
    events = [
        {
            "key": f"k{i}",
            "title": f"Reminder {i}",
            "start": f"2026-11-0{i + 1}T08:00",
            "end": f"2026-11-0{i + 1}T08:05",
        }
        for i in range(3)
    ]
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"account": "me", "calendar": "Family", "events": events}))
    code, body = here.json("event", "apply", str(plan))
    assert code == EXIT_OK and body["data"]["counts"]["create"] == 3
    code, out = here.cli(
        "event", "apply", "-", "--account", "me", "--calendar", "Family", stdin=json.dumps(events)
    )
    assert code == EXIT_OK and out.splitlines()[0] == '3 unchanged in a calendar of account "me"'
    code, out = here.cli("event", "apply", str(tmp_path / "missing.json"))
    assert code == EXIT_CONFIG and "cannot read it" in out


def test_writes_lists_the_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = Here(tmp_path, monkeypatch)
    assert here.cli("writes") == (EXIT_OK, "no writes recorded\n")
    here.cli("event", "edit", here.event("Lunch")["id"], "--title", "Brunch")
    code, body = here.json("writes")
    assert code == EXIT_OK
    [row] = body["writes"]
    assert (row["op"], row["origin"], row["title"], row["calendar_name"]) == ("edit", "cli", "Brunch", None)
    assert row["changes"] == [{"field": "title", "before": "Lunch", "after": "Brunch"}]
    code, out = here.cli("writes")
    assert code == EXIT_OK and "  cli  edit  me  Brunch  ok" in out


@pytest.mark.anyio
async def test_over_the_socket_a_conflict_keeps_its_kind(short_dir: Path) -> None:
    d = Running(short_dir, TEXT, serve=True)
    await d.start()
    await eventually(lambda: (short_dir / "calsync.sock").is_socket())
    base = ["--config", str(d.path), "--state-dir", str(short_dir)]
    code, out = await anyio.to_thread.run_sync(
        lambda: run([*base, "events", "--from", "2026-10-25", "--json"])
    )
    lunch = next(e for e in json.loads(out)["events"] if e["title"] == "Lunch")
    d.world.fake.touch(GCAL, "lunch", location="Elsewhere")
    argv = [
        *base,
        "event",
        "edit",
        lunch["id"],
        "--title",
        "x",
        "--expect-updated",
        lunch["updated"],
        "--json",
    ]
    code, out = await anyio.to_thread.run_sync(lambda: run(argv))
    assert code == EXIT_FAILED and json.loads(out)["error"]["kind"] == "event_conflict"
    code, out = await anyio.to_thread.run_sync(
        lambda: run([*base, "event", "edit", lunch["id"], "--title", "Brunch", "--json"])
    )
    assert code == EXIT_OK and json.loads(out)["data"]["cycle"] == {"requested": ["family"]}
    await d.stop()
