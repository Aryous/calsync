"""Serve the calsync web UI against synthetic fixtures, without the backend.

    uv run python scripts/dev_web.py [--port 8767] [--scenario ok]

- ``/`` serves ``index.html`` (``pair.html`` for the ``unpaired`` scenario).
- ``/static/*`` serves ``src/calsync/web/static``.
- ``/_frame?w=390&h=844&src=/%3Fscenario%3Dok`` wraps a page in a sized iframe (screenshots).
- ``/api/*`` serves ``tests/fixtures/web/<scenario>/<name>.json``, falling back to ``ok/``.

The scenario comes from ``?scenario=`` on any request (``/?scenario=attention`` also sets a
cookie that the page's API calls carry), else the cookie, else ``$SCENARIO``, else ``ok``.
Scenarios with fixtures: ok, attention, never_run, old_schema, readonly. Three more are built
in: ``no_state`` (every API call answers 503), ``offline`` (API connections are dropped) and
``unpaired`` (401, ``/`` is the pair page).

Time markers in fixtures are resolved at serve time, so "the next 24 hours" stays true:
``"@-90s"``, ``"@+35m"``, ``"@-3h"`` become ISO datetimes relative to now; ``"@q+35m"``
anchors on now floored to the quarter hour; ``"@+1D"`` becomes a local date N days ahead.

``POST /api/trigger`` answers 202 (409 in ``readonly``). From then on the fixtures look
synced: pending events are delivered, ``summary.last_sync_at`` moves to the trigger time
and a ``created`` change appears. Loading ``/`` resets this.

Standard library only; the backend is not imported.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "calsync" / "web" / "static"
FIXTURES = ROOT / "tests" / "fixtures" / "web"
BUILTIN = {"no_state", "offline", "unpaired"}
MARKER = re.compile(r"^@(q?)([+-]\d+)([smhD])$")
SCENARIO_RE = re.compile(r"^[a-z_]+$")

# scenario -> trigger time; cleared when the page is reloaded
TRIGGERED: dict[str, datetime] = {}


def scenarios() -> list[str]:
    found = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())
    return [*found, *sorted(BUILTIN)]


def resolve(value: Any, now: datetime) -> Any:
    if isinstance(value, dict):
        return {k: resolve(v, now) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, now) for v in value]
    if isinstance(value, str):
        m = MARKER.match(value)
        if m:
            quarter, amount, unit = m.group(1), int(m.group(2)), m.group(3)
            if unit == "D":
                return (now.date() + timedelta(days=amount)).isoformat()
            base = now.replace(minute=now.minute - now.minute % 15, second=0) if quarter else now
            step = {"s": timedelta(seconds=1), "m": timedelta(minutes=1), "h": timedelta(hours=1)}[unit]
            return (base + amount * step).isoformat(timespec="seconds")
    return value


def load(scenario: str, name: str) -> Any:
    for folder in (scenario, "ok"):
        path = FIXTURES / folder / f"{name}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(name)


def api_payload(scenario: str, name: str, query: dict[str, list[str]]) -> Any:
    now = datetime.now().astimezone().replace(microsecond=0)
    data = resolve(load(scenario, name), now)
    synced_at = TRIGGERED.get(scenario)
    if name == "status" and synced_at:
        data["summary"]["last_sync_at"] = synced_at.isoformat(timespec="seconds")
        data["summary"]["changes_24h"] = (data["summary"]["changes_24h"] or 0) + 1
        if data.get("daemon"):
            data["daemon"]["last_cycle_at"] = data["summary"]["last_sync_at"]
    if name == "upcoming" and synced_at:
        for event in data["events"]:
            if event["delivery"] == "pending":
                event["delivery"] = "delivered"
    if name == "changes":
        rows = data["changes"]
        if synced_at:
            rows.insert(
                0,
                {
                    "id": 999,
                    "run_id": 41 if scenario != "attention" else 81,
                    "rule": "family",
                    "object_id": "0029000a1b2c3d4e5f6a7b8c",
                    "action": "created",
                    "at": synced_at.isoformat(timespec="seconds"),
                    "title": "和朋友吃午饭",
                },
            )
        if "run_id" in query:
            rows = [c for c in rows if str(c["run_id"]) == query["run_id"][0]]
        limit = int(query.get("limit", ["100"])[0])
        data["changes"] = rows[: max(1, min(limit, 1000))]
    if name == "runs":
        limit = int(query.get("limit", ["50"])[0])
        data["runs"] = data["runs"][: max(1, min(limit, 500))]
    return data


class Handler(SimpleHTTPRequestHandler):
    server_version = "calsync-dev"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC), **kwargs)

    # -- helpers ---------------------------------------------------------------------
    def scenario(self) -> tuple[str, bool]:
        query = parse_qs(urlsplit(self.path).query)
        if "scenario" in query and SCENARIO_RE.match(query["scenario"][0]):
            return query["scenario"][0], True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        if "dev_scenario" in cookie and SCENARIO_RE.match(cookie["dev_scenario"].value):
            return cookie["dev_scenario"].value, False
        return os.environ.get("SCENARIO", "ok"), False

    def send_json(self, status: int, body: Any, extra: dict[str, str] | None = None) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def send_html(self, name: str, status: int = 200, cookie: str | None = None) -> None:
        raw = (STATIC / name).read_bytes()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        if cookie:
            self.send_header("Set-Cookie", f"dev_scenario={cookie}; Path=/; SameSite=Lax")
        self.end_headers()
        self.wfile.write(raw)

    def send_frame(self, query: dict[str, list[str]]) -> None:
        # Headless Chrome will not make a window narrower than 500 px; a sized iframe gives the
        # page a true phone-width viewport for screenshots.
        w = int(query.get("w", ["390"])[0])
        h = int(query.get("h", ["844"])[0])
        src = html.escape(query.get("src", ["/"])[0], quote=True)
        page = (
            "<!doctype html><meta charset=utf-8><style>html,body{margin:0;background:#888}"
            f"iframe{{display:block;margin:0 auto;border:0;width:{w}px;height:{h}px}}</style>"
            f'<iframe src="{src}"></iframe>'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def error_json(self, status: int, kind: str, message: str) -> None:
        self.send_json(status, {"error": {"kind": kind, "message": message}})

    # -- routes ----------------------------------------------------------------------
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        scenario, from_query = self.scenario()
        if url.path == "/":
            TRIGGERED.pop(scenario, None)
            page = "pair.html" if scenario == "unpaired" else "index.html"
            self.send_html(page, cookie=scenario if from_query else None)
            return
        if url.path == "/_frame":
            self.send_frame(parse_qs(url.query))
            return
        if url.path.startswith("/static/"):
            self.path = self.path[len("/static") :]
            super().do_GET()
            return
        if url.path.startswith("/api/"):
            self.handle_api(scenario, url.path[len("/api/") :], parse_qs(url.query))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        scenario, _ = self.scenario()
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if url.path == "/pair":
            self.send_html("pair.html", status=403)
            return
        if url.path != "/api/trigger":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if scenario == "unpaired":
            self.error_json(401, "unpaired", "This browser is not paired.")
        elif scenario == "no_state":
            self.error_json(503, "no_state", "No sync has run yet.")
        elif scenario == "readonly":
            self.error_json(409, "read_only", "The daemon runs with --no-sync.")
        else:
            TRIGGERED[scenario] = datetime.now().astimezone().replace(microsecond=0)
            self.send_json(202, {"accepted": True, "rule": None})

    def handle_api(self, scenario: str, name: str, query: dict[str, list[str]]) -> None:
        if scenario == "offline":
            self.close_connection = True  # no response at all: fetch() rejects
            return
        if scenario == "unpaired":
            self.error_json(401, "unpaired", "This browser is not paired.")
            return
        if scenario == "no_state":
            self.error_json(503, "no_state", "No sync has run yet.")
            return
        if name not in {"status", "upcoming", "runs", "changes"}:
            self.error_json(404, "not_found", "Unknown endpoint.")
            return
        try:
            self.send_json(200, api_payload(scenario, name, query))
        except FileNotFoundError:
            self.error_json(404, "not_found", f"No fixture for scenario {scenario!r}.")

    def end_headers(self) -> None:
        if self.path.endswith((".js", ".mjs", ".css")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("DEV_WEB_QUIET"):
            return
        super().log_message(format, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8767")))
    parser.add_argument("--scenario", help="default scenario (overrides $SCENARIO)")
    args = parser.parse_args()
    if args.scenario:
        os.environ["SCENARIO"] = args.scenario
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"calsync dev UI on http://{args.host}:{args.port}/  scenarios: {', '.join(scenarios())}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
