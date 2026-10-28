"""The harness itself: the live daemon serves the seeded state, and a page pairs with it."""

import pytest
from playwright.sync_api import Page

from tests.browser.live import PENDING_TITLE, LiveDaemon

pytestmark = pytest.mark.browser


def test_the_live_daemon_serves_the_seeded_state(live: LiveDaemon) -> None:
    status = live.get("/api/status")
    assert [r["name"] for r in status["rules"]] == ["family-home", "school-home", "family-mine", "work-mine"]
    assert {a["name"]: a["host"] for a in status["accounts"]} == {
        "personal": "Google Calendar",
        "work": "Google Calendar",
        "family-icloud": "iCloud",
        "my-icloud": "iCloud",
    }
    personal = next(a for a in status["accounts"] if a["name"] == "personal")
    assert sorted(c["name"] for c in personal["calendars"]) == sorted(["家庭日程", "孩子学校", "中国节假日"])
    assert status["daemon"]["running"] is True and status["daemon"]["cycles"] >= 2
    events = live.get("/api/events", **{"from": "2026-10-26", "to": "2026-11-02"})["events"]
    lunch = next(e for e in events if e["title"] == PENDING_TITLE)
    states = {d["rule"]: d["state"] for d in lunch["deliveries"]}
    assert states == {"family-home": "delivered", "family-mine": "pending"}
    titles = {e["title"] for e in events}
    assert {"晨跑", "交物业费", "钢琴课", "周会", "秋游"} <= titles


def test_a_page_pairs_through_the_key_link(paired_page: Page, live: LiveDaemon) -> None:
    assert paired_page.url == f"{live.url}/"
    cookies = paired_page.context.cookies()
    assert any(c.get("name") == "calsync_session" for c in cookies)


def test_a_fresh_daemon_runs_in_shanghai_without_a_page(fresh_live: LiveDaemon) -> None:
    """``fresh_live`` brings the session's environment itself (local zone included): local days
    are Asia/Shanghai's whether or not a test opens a page."""
    import os
    import time

    assert os.environ["TZ"] == "Asia/Shanghai" and time.strftime("%z") == "+0800"
    events = fresh_live.get("/api/events", **{"from": "2026-10-29", "to": "2026-10-30"})["events"]
    fees = next(e for e in events if e["title"] == "交物业费")
    assert fees["all_day"] is True and fees["start"].startswith("2026-10-29")
