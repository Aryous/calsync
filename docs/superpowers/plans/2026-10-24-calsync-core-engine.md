# calsync 核心引擎实现计划（计划 1 / 3）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从空仓库做出可安装的 `calsync`：Google 日历 → CalDAV（iCloud 等）的单向增量同步引擎，`calsync run` 能完整执行一个同步周期，空闲周期的请求数由测试锁定。

**Architecture:** 源适配器（Google REST，syncToken 增量）把变化写进本地镜像；目标适配器（自写的最小 CalDAV 协议层）用 sync-collection → getctag → 全量列表的降级阶梯检测变化；纯函数规划器在内存中比对"应有集合"和"现有集合"，引擎按账号限流推送。所有本地状态都是可重建的缓存（SQLite），托管关系由目标端资源名 `calsync-<规则>-<ID>.ics` 体现。

**Tech Stack:** Python 3.12+、uv、httpx（HTTP/2）、icalendar 7、python-dateutil、keyring、SQLite；测试用 pytest + anyio、本地 Radicale CalDAV 服务器、httpx.MockTransport 实现的 Google 假服务；ruff、pyright。

## Global Constraints

- 设计规格：`docs/superpowers/specs/2026-10-23-calsync-design.md`。本计划与规格冲突时以规格为准，并报告冲突。
- Python `>=3.12`；运行时依赖只有 `httpx[http2]>=0.28,<1`、`icalendar>=7.3,<8`、`python-dateutil>=2.9`、`keyring>=25`。不得引入 `google-api-python-client`、`caldav` 等库。
- 同步方向只有单向：源 → 目标。只修改、删除资源名匹配 `calsync-<8位hex>-<24位hex>.ics` 的资源。
- 写入目标的事件不得带 `ATTENDEE` / `ORGANIZER`。
- 本地状态全部是缓存：删除状态目录后，下一轮结果必须与删除前一致（不重复、不误删）。
- 空闲周期请求预算（规格 §8.1）：每个 Google 账号 1 次 `calendarList` + 每个源日历 1 次 `events.list` + 每个目标日历 1 次变化检测，不读取任何事件正文。
- Google `events.list` 的固定参数：`singleEvents=false`、`showDeleted=true`、`maxResults=2500`，`fields` 必须包含 `nextPageToken,nextSyncToken`；不使用 `timeMin` / `timeMax`。
- 测试数据只用合成内容和 `example.com` 邮箱，不得出现真实日历内容或凭证。仓库计划开源。
- 代码风格：ruff（`line-length = 110`，规则集见 `pyproject.toml`）、ruff format、pyright `standard` 模式，全部 0 问题。
- 每个任务结束都提交一次；提交信息末尾加一行 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。

## 本计划在三份计划中的位置

1. **计划 1（本文件）：核心引擎**。全部在本地测试：Google 用内存假服务，CalDAV 用本地 Radicale。完成后 `calsync run` 可用。
2. **计划 2：真实环境验证**。规格 §13 的实测项（共享日历提醒、自定义属性、资源名、sync-collection、MKCALENDAR、ETag、HTTP/2、跨主机发现、中国区），需要用户提供 iCloud 测试账号。
3. **计划 3：运行时与运维**。常驻进程、`trigger`、告警（Bark / webhook）、`status`、Google 浏览器授权、launchd 与 Docker。

## 本计划如何验证过

计划中的每一行代码都来自一个已经跑通的原型，并由脚本逐字嵌入，没有手抄。生成前用脚本在空目录里按任务顺序回放了一遍：每个任务先只放测试文件，确认测试失败；再放实现文件，确认此前所有测试通过。每个任务的"预期输出"就是那次回放的真实结果。

执行时**逐字使用计划里的代码**。如果某一步结果和预期不符，先停下来报告，不要为了让测试通过去修改测试。

## 文件结构

| 文件 | 职责 | 任务 |
| --- | --- | --- |
| `pyproject.toml`、`.python-version`、`.github/workflows/ci.yml` | 包定义、工具配置、CI | 1 |
| `src/calsync/errors.py` | 统一错误类型 | 1 |
| `src/calsync/cli.py` | 命令行入口 | 1（最小版）、14（完整版） |
| `src/calsync/model.py` | 统一事件模型、哈希、跨度、窗口、提醒策略 | 2 |
| `src/calsync/durations.py`、`src/calsync/config.py` | 时长解析、配置加载与校验 | 3 |
| `src/calsync/credentials.py` | 凭证后端 | 4 |
| `src/calsync/icalutil.py`、`src/calsync/sources/base.py`、`src/calsync/sources/google_map.py` | iCalendar 工具、源接口、Google 事件映射 | 5 |
| `src/calsync/naming.py`、`src/calsync/render.py` | 确定性命名、渲染与读回 | 6 |
| `src/calsync/planner.py` | 规划与防误删 | 7 |
| `src/calsync/http.py` | HTTP 客户端、请求日志、重试 | 8 |
| `src/calsync/sources/google_auth.py`、`src/calsync/sources/google.py` | Google 令牌与增量客户端 | 9 |
| `src/calsync/targets/dav.py` | WebDAV/CalDAV 协议层 | 10 |
| `src/calsync/targets/base.py`、`src/calsync/targets/caldav.py` | 目标接口、CalDAV 适配器 | 11 |
| `src/calsync/mirror.py`、`src/calsync/store.py` | 源镜像、SQLite 状态 | 12 |
| `src/calsync/engine.py` | 同步周期编排 | 13 |
| `src/calsync/lock.py`、`src/calsync/wiring.py` | 单实例锁、按配置组装 | 14 |
| `tests/conftest.py` | anyio 与本地 Radicale fixture | 1 |
| `tests/factories.py` | 合成测试数据 | 2 |
| `tests/fakes/google.py` | Google Calendar API 假服务 | 9 |
| `tests/harness.py` | 引擎测试组装（假 Google + Radicale） | 13 |
| `tests/e2e/test_request_budget.py` | 请求预算回归测试 | 15 |

## 通用命令

- 单个任务的测试：`uv run pytest <测试文件> -v`
- 全部测试：`uv run pytest`（包含连接本地 Radicale 的 `e2e` 测试，需要 `uv sync` 装好开发依赖）
- 质量检查：`uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

---

### Task 1: 项目骨架、错误类型、最小 CLI、CI

建立可安装的 Python 包、开发工具链和 CI，`calsync --version` 可运行。

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `README.md`
- Create: `.github/workflows/ci.yml`
- Create: `tests/__init__.py`
- Create: `tests/fakes/__init__.py`
- Create: `tests/e2e/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_cli.py`
- Create: `src/calsync/__init__.py`
- Create: `src/calsync/errors.py`
- Create: `src/calsync/cli.py`

**Interfaces:**
- Consumes:
  - 无（第一个任务）。
- Produces:
  - `calsync.__version__: str = "0.1.0"`
  - `calsync.errors`：`CalsyncError`（基类）、`ConfigError(problems: list[str])`（`.problems`）、`AuthError`、`TransientError`、`ConflictError`、`NotFoundError`、`CursorExpired`
  - `calsync.cli.main(argv: Sequence[str] | None = None, *, out: TextIO = sys.stdout) -> int`（Task 14 会整体替换为完整版）
  - `tests/conftest.py` 提供 fixture：`anyio_backend`（固定 asyncio）、`radicale_url`（session 级本地 Radicale，免认证）、`dav_user`（每个测试一个新用户名）

**注意：**
- 先运行 `uv sync` 生成 `.venv` 和 `uv.lock`；`uv.lock` 要提交，CI 使用 `uv sync --locked`。
- `tests/conftest.py` 里的 Radicale fixture 现在还用不到，Task 11 开始使用。它会启动 `python -m radicale` 子进程，监听随机端口，测试结束后关闭。

- [ ] **Step 1: 写入项目配置文件**

`pyproject.toml`：

````toml
[project]
name = "calsync"
version = "0.1.0"
description = "One-way calendar sync engine: Google Calendar to CalDAV (iCloud and more)"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "httpx[http2]>=0.28,<1",
    "icalendar>=7.3,<8",
    "python-dateutil>=2.9",
    "keyring>=25",
]

[project.scripts]
calsync = "calsync.cli:main"

[dependency-groups]
dev = [
    "pytest>=9",
    "anyio>=4.6",
    "radicale>=3.8",
    "ruff>=0.8",
    "pyright>=1.1.390",
]

[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/calsync"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"
markers = ["e2e: end-to-end tests against a local Radicale server"]

[tool.ruff]
line-length = 110
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "SIM", "RUF", "ASYNC"]
ignore = ["E501", "RUF001", "RUF002", "RUF003"]

[tool.pyright]
include = ["src"]
pythonVersion = "3.12"
typeCheckingMode = "standard"
venvPath = "."
venv = ".venv"
````

`.python-version`：

````text
3.12
````

`README.md`：

````markdown
# calsync

One-way calendar sync engine: Google Calendar to CalDAV (iCloud and more).
````

`.github/workflows/ci.yml`：

````yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --locked
      - run: uv run ruff check src tests
      - run: uv run ruff format --check src tests
      - run: uv run pyright
      - run: uv run pytest
````

- [ ] **Step 2: 安装依赖**

Run: `uv sync`

Expected: 创建 `.venv/` 和 `uv.lock`，无报错。

- [ ] **Step 3: 写失败的测试**

`tests/__init__.py`：空文件（`touch tests/__init__.py`）。

`tests/fakes/__init__.py`：空文件（`touch tests/fakes/__init__.py`）。

`tests/e2e/__init__.py`：空文件（`touch tests/e2e/__init__.py`）。

`tests/conftest.py`：

````python
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def radicale_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A throwaway Radicale CalDAV server (auth disabled: any username works)."""
    pytest.importorskip("radicale")
    port = _free_port()
    storage: Path = tmp_path_factory.mktemp("radicale")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "radicale",
            "--storage-filesystem-folder",
            str(storage),
            "--auth-type",
            "none",
            "--server-hosts",
            f"127.0.0.1:{port}",
            "--logging-level",
            "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + 15
    while True:
        try:
            httpx.get(url, timeout=0.5)
            break
        except httpx.TransportError:
            if proc.poll() is not None or time.monotonic() > deadline:
                stderr = proc.stderr.read().decode() if proc.stderr else ""
                proc.kill()
                pytest.fail(f"radicale did not start: {stderr}")
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def dav_user() -> str:
    """A fresh username per test, so every test gets an empty calendar home."""
    return f"u{uuid.uuid4().hex[:10]}"
````

`tests/test_cli.py`：

````python
import io

from calsync import __version__
from calsync.cli import main


def test_version() -> None:
    out = io.StringIO()
    assert main(["--version"], out=out) == 0
    assert out.getvalue() == f"calsync {__version__}\n"


def test_no_command_prints_help() -> None:
    out = io.StringIO()
    assert main([], out=out) == 2
    assert "usage: calsync" in out.getvalue()
````

- [ ] **Step 4: 运行测试，确认失败**

Run: `uv run pytest tests/test_cli.py -v`

Expected: FAIL，收集阶段报错 `ModuleNotFoundError: No module named 'calsync'`

- [ ] **Step 5: 写实现**

`src/calsync/__init__.py`：

````python
"""calsync: one-way calendar sync engine."""

__version__ = "0.1.0"
````

`src/calsync/errors.py`：

````python
"""Error types shared by every module.

Adapters translate service-specific failures into these so the engine can
decide what to retry, what to alert on, and what to surface to the user.
"""


class CalsyncError(Exception):
    """Base class for all calsync errors."""


class ConfigError(CalsyncError):
    """The configuration is invalid. Carries every problem found, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class AuthError(CalsyncError):
    """Credentials are missing, revoked or rejected. Never retried automatically."""


class TransientError(CalsyncError):
    """Network failure, throttling or a 5xx response that survived retries."""


class ConflictError(CalsyncError):
    """A conditional write failed (HTTP 412): the resource changed or already exists."""


class NotFoundError(CalsyncError):
    """The requested calendar or resource does not exist."""


class CursorExpired(CalsyncError):
    """An incremental sync cursor is no longer valid; a full resync is required."""
````

`src/calsync/cli.py`：

````python
"""Command-line entry point. Grows into the full CLI in Task 14."""

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from calsync import __version__


def main(argv: Sequence[str] | None = None, *, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="calsync", description="One-way calendar sync.")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args(argv)
    if args.version:
        out.write(f"calsync {__version__}\n")
        return 0
    parser.print_help(out)
    return 2
````

- [ ] **Step 6: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `2 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 7: 提交**

```bash
git add pyproject.toml .python-version README.md .github/workflows/ci.yml tests/__init__.py tests/fakes/__init__.py tests/e2e/__init__.py tests/conftest.py tests/test_cli.py src/calsync/__init__.py src/calsync/errors.py src/calsync/cli.py uv.lock
git commit -m "chore: project scaffold, error types, minimal CLI and CI" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: 统一事件模型

实现与具体日历服务无关的事件模型：When / Alarm / Occurrence / Series / Window，以及规范化、内容哈希、跨度计算、提醒策略。

**Files:**
- Create: `tests/factories.py`
- Create: `tests/test_model.py`
- Create: `src/calsync/model.py`

**Interfaces:**
- Consumes:
  - 无。
- Produces:
  - `When(value: datetime | date, tzid: str | None = None)`：datetime 必须带时区且必须给 `tzid`；全天 date 不能带 `tzid`。属性 `all_day`，方法 `utc() -> datetime`（全天按 00:00 UTC）
  - `Alarm(offset: timedelta)`，负数表示提前
  - `Occurrence(original_start, start, end, title, description, location, alarms)`：字段完整，不继承主事件
  - `Series(source_id, title, description, location, start, end, recurrence=(), exdates=(), overrides=(), alarms=(), transparent=False)`
  - `Window(start, end)`，`Window.around(now, past, future)`，`window.intersects(span) -> bool`
  - `AlarmPolicy = Literal["copy", "strip"] | tuple[timedelta, ...]`
  - `normalize_series(s) -> Series`、`series_hash(s) -> str`（32 位十六进制，含 `RENDER_VERSION`）
  - `apply_alarm_policy(s, policy) -> Series`
  - `compute_span(start, end, recurrence=(), extra=()) -> tuple[datetime, datetime]`、`series_span(s)`
  - 常量 `RENDER_VERSION = 1`、`MAX_TIME`
  - 测试工具 `tests/factories.py`：`at(y, mo, d, h=0, mi=0, tz=TZ) -> When`、`day(y, mo, d) -> When`、`series(...) -> Series`、`google_event(id, summary, start, end, *, tz, recurrence, reminders, **extra) -> dict`、常量 `TZ = "Asia/Shanghai"`、`ZONE`

**注意：**
- 没有 COUNT/UNTIL 的 RRULE、或无法解析的 RRULE，跨度结束记为 `MAX_TIME`，宁可把系列当作"仍在进行"，也不误判为"已结束"。
- 哈希先规范化再计算：去首尾空白、提醒去重排序、EXDATE 和单次改动按时间排序。

- [ ] **Step 1: 写失败的测试**

`tests/factories.py`：

````python
"""Builders for synthetic test data. Never put real personal data in tests."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from calsync.model import Alarm, Occurrence, Series, When

TZ = "Asia/Shanghai"
ZONE = ZoneInfo(TZ)


def at(y: int, mo: int, d: int, h: int = 0, mi: int = 0, tz: str = TZ) -> When:
    return When(datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)), tz)


def day(y: int, mo: int, d: int) -> When:
    return When(date(y, mo, d))


def series(
    source_id: str = "evt1",
    title: str = "Standup",
    start: When | None = None,
    minutes: int = 5,
    recurrence: tuple[str, ...] = (),
    exdates: tuple[When, ...] = (),
    overrides: tuple[Occurrence, ...] = (),
    alarms: tuple[Alarm, ...] = (Alarm(timedelta(0)),),
) -> Series:
    start = start or at(2026, 10, 23, 9, 30)
    end = When(start.value + timedelta(minutes=minutes), start.tzid) if not start.all_day else start
    return Series(
        source_id=source_id,
        title=title,
        description="",
        location="",
        start=start,
        end=end,
        recurrence=recurrence,
        exdates=exdates,
        overrides=overrides,
        alarms=alarms,
    )


def google_event(
    event_id: str,
    summary: str,
    start: str,
    end: str,
    *,
    tz: str = TZ,
    recurrence: list[str] | None = None,
    reminders: dict | None = None,
    **extra: object,
) -> dict:
    event: dict = {
        "id": event_id,
        "status": "confirmed",
        "summary": summary,
        "start": {"dateTime": start, "timeZone": tz},
        "end": {"dateTime": end, "timeZone": tz},
        "reminders": reminders or {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]},
    }
    if recurrence:
        event["recurrence"] = recurrence
    event.update(extra)
    return event
````

`tests/test_model.py`：

````python
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from calsync import model
from calsync.model import (
    MAX_TIME,
    Alarm,
    Occurrence,
    When,
    Window,
    apply_alarm_policy,
    compute_span,
    series_hash,
    series_span,
)
from tests.factories import ZONE, at, day, series


def test_when_validation() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        When(datetime(2026, 1, 1), "UTC")
    with pytest.raises(ValueError, match="needs a tzid"):
        When(datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="all-day"):
        When(date(2026, 1, 1), "UTC")
    assert day(2026, 1, 2).all_day
    assert day(2026, 1, 2).utc() == datetime(2026, 1, 2, tzinfo=UTC)
    assert at(2026, 1, 2, 8).utc() == datetime(2026, 1, 2, 0, tzinfo=UTC)


def test_span_single_event() -> None:
    s = series(start=at(2026, 10, 23, 9, 30))
    assert series_span(s) == (
        datetime(2026, 10, 23, 1, 30, tzinfo=UTC),
        datetime(2026, 10, 23, 1, 35, tzinfo=UTC),
    )


def test_span_count_rrule() -> None:
    s = series(recurrence=("RRULE:FREQ=DAILY;COUNT=21",))
    lo, hi = series_span(s)
    assert lo == datetime(2026, 10, 23, 1, 30, tzinfo=UTC)
    assert hi == datetime(2026, 11, 12, 1, 35, tzinfo=UTC)


def test_span_until_rrule() -> None:
    s = series(recurrence=("RRULE:FREQ=DAILY;UNTIL=20261105T235959Z",))
    assert series_span(s)[1] == datetime(2026, 11, 5, 1, 35, tzinfo=UTC)


def test_span_unbounded_and_invalid_rrule() -> None:
    assert series_span(series(recurrence=("RRULE:FREQ=WEEKLY",)))[1] == MAX_TIME
    assert series_span(series(recurrence=("RRULE:FREQ=BOGUS;COUNT=2",)))[1] == MAX_TIME


def test_span_all_day_rrule() -> None:
    s = series(start=day(2026, 10, 23), recurrence=("RRULE:FREQ=DAILY;COUNT=3",))
    s = replace(s, end=day(2026, 10, 24))
    assert series_span(s) == (datetime(2026, 10, 23, tzinfo=UTC), datetime(2026, 10, 26, tzinfo=UTC))


def test_span_extended_by_moved_override() -> None:
    moved = Occurrence(
        original_start=at(2026, 10, 24, 9, 30),
        start=at(2026, 12, 1, 9, 30),
        end=at(2026, 12, 1, 9, 35),
        title="moved",
        description="",
        location="",
        alarms=(),
    )
    s = series(recurrence=("RRULE:FREQ=DAILY;COUNT=2",), overrides=(moved,))
    assert series_span(s)[1] == datetime(2026, 12, 1, 1, 35, tzinfo=UTC)


def test_compute_span_extra_before_start() -> None:
    early = (at(2026, 10, 1, 8), at(2026, 10, 1, 9))
    lo, _ = compute_span(at(2026, 10, 23, 9), at(2026, 10, 23, 10), (), [early])
    assert lo == datetime(2026, 10, 1, 0, tzinfo=UTC)


def test_window_intersects() -> None:
    now = datetime(2026, 10, 24, tzinfo=UTC)
    w = Window.around(now, timedelta(days=7), timedelta(days=180))
    assert w.intersects((now - timedelta(days=30), now - timedelta(days=1)))
    assert not w.intersects((now - timedelta(days=30), now - timedelta(days=8)))
    assert not w.intersects((now + timedelta(days=181), now + timedelta(days=182)))


def test_hash_is_stable_under_normalization() -> None:
    a = series(title="Standup", alarms=(Alarm(timedelta(0)), Alarm(timedelta(minutes=-5))))
    b = series(title="  Standup ", alarms=(Alarm(timedelta(minutes=-5)), Alarm(timedelta(0))))
    assert series_hash(a) == series_hash(b)
    assert len(series_hash(a)) == 32


def test_hash_changes_with_content_and_render_version(monkeypatch: pytest.MonkeyPatch) -> None:
    base = series()
    assert series_hash(base) != series_hash(replace(base, title="Other"))
    assert series_hash(base) != series_hash(replace(base, alarms=()))
    before = series_hash(base)
    monkeypatch.setattr(model, "RENDER_VERSION", model.RENDER_VERSION + 1)
    assert series_hash(base) != before


def test_alarm_policies() -> None:
    occ = Occurrence(
        original_start=at(2026, 10, 24, 9, 30),
        start=at(2026, 10, 24, 21),
        end=at(2026, 10, 24, 21, 5),
        title="late",
        description="",
        location="",
        alarms=(Alarm(timedelta(minutes=-1)),),
    )
    s = series(recurrence=("RRULE:FREQ=DAILY;COUNT=2",), overrides=(occ,))
    assert apply_alarm_policy(s, "copy") is s
    stripped = apply_alarm_policy(s, "strip")
    assert stripped.alarms == () and stripped.overrides[0].alarms == ()
    fixed = apply_alarm_policy(s, (timedelta(minutes=-10),))
    assert fixed.alarms == (Alarm(timedelta(minutes=-10)),)
    assert fixed.overrides[0].alarms == (Alarm(timedelta(minutes=-10)),)


def test_zone_is_preserved() -> None:
    assert at(2026, 10, 23).value.tzinfo == ZONE
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_model.py -v`

Expected: FAIL，收集阶段报错 `ImportError: cannot import name 'model' from 'calsync'`

- [ ] **Step 3: 写实现**

`src/calsync/model.py`：

````python
"""Unified event model, independent of any calendar service.

A ``Series`` is the unit of sync: a single event is a series with one
occurrence; a recurring event carries its recurrence lines plus every
individually modified instance (``Occurrence``).
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from dateutil.rrule import rrulestr

# Bump whenever render.py changes the bytes it produces: every managed
# resource is then rewritten once on the next cycle.
RENDER_VERSION = 1

MAX_TIME = datetime(9999, 12, 31, tzinfo=UTC)

AlarmPolicy = Literal["copy", "strip"] | tuple[timedelta, ...]


@dataclass(frozen=True, slots=True)
class When:
    """A point in time: an aware datetime plus its IANA zone, or an all-day date."""

    value: datetime | date
    tzid: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.value, datetime):
            if self.value.tzinfo is None:
                raise ValueError("When datetime must be timezone-aware")
            if self.tzid is None:
                raise ValueError("When datetime needs a tzid")
        elif self.tzid is not None:
            raise ValueError("all-day When must not carry a tzid")

    @property
    def all_day(self) -> bool:
        return not isinstance(self.value, datetime)

    def utc(self) -> datetime:
        """Instant in UTC. All-day dates are anchored at 00:00 UTC."""
        if isinstance(self.value, datetime):
            return self.value.astimezone(UTC)
        return datetime(self.value.year, self.value.month, self.value.day, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Alarm:
    offset: timedelta  # relative to the start; negative means before


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One modified instance of a recurring series.

    Fields are complete: per RFC 5545 a RECURRENCE-ID component replaces the
    instance entirely and inherits nothing from the master.
    """

    original_start: When
    start: When
    end: When
    title: str
    description: str
    location: str
    alarms: tuple[Alarm, ...]


@dataclass(frozen=True, slots=True)
class Series:
    source_id: str
    title: str
    description: str
    location: str
    start: When
    end: When
    recurrence: tuple[str, ...] = ()  # raw RRULE / RDATE lines
    exdates: tuple[When, ...] = ()
    overrides: tuple[Occurrence, ...] = ()
    alarms: tuple[Alarm, ...] = ()
    transparent: bool = False


@dataclass(frozen=True, slots=True)
class Window:
    start: datetime
    end: datetime

    @classmethod
    def around(cls, now: datetime, past: timedelta, future: timedelta) -> "Window":
        return cls(now - past, now + future)

    def intersects(self, span: tuple[datetime, datetime]) -> bool:
        lo, hi = span
        return lo < self.end and hi > self.start


def _alarm_key(alarm: Alarm) -> timedelta:
    return alarm.offset


def _normalize_occurrence(occ: Occurrence) -> Occurrence:
    return replace(
        occ,
        title=occ.title.strip(),
        description=occ.description.strip(),
        location=occ.location.strip(),
        alarms=tuple(sorted(set(occ.alarms), key=_alarm_key)),
    )


def normalize_series(series: Series) -> Series:
    """Canonical form: trimmed text, deduplicated and sorted collections."""
    return replace(
        series,
        title=series.title.strip(),
        description=series.description.strip(),
        location=series.location.strip(),
        recurrence=tuple(line.strip() for line in series.recurrence),
        exdates=tuple(sorted(set(series.exdates), key=When.utc)),
        overrides=tuple(
            sorted(
                (_normalize_occurrence(o) for o in series.overrides),
                key=lambda o: o.original_start.utc(),
            )
        ),
        alarms=tuple(sorted(set(series.alarms), key=_alarm_key)),
    )


def _canon(value: Any) -> Any:
    if isinstance(value, When):
        return [value.value.isoformat(), value.tzid]
    if isinstance(value, Alarm):
        return int(value.offset.total_seconds())
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _canon(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple | list):
        return [_canon(item) for item in value]
    return value


def series_hash(series: Series) -> str:
    """Stable content hash of the normalized series plus RENDER_VERSION."""
    payload = json.dumps(
        [RENDER_VERSION, _canon(normalize_series(series))],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.blake2b(payload.encode(), digest_size=16).hexdigest()


def apply_alarm_policy(series: Series, policy: AlarmPolicy) -> Series:
    if policy == "copy":
        return series
    alarms: tuple[Alarm, ...] = () if policy == "strip" else tuple(Alarm(o) for o in policy)
    return replace(
        series,
        alarms=alarms,
        overrides=tuple(replace(o, alarms=alarms) for o in series.overrides),
    )


def _rrule_anchor(start: When) -> datetime:
    if isinstance(start.value, datetime):
        return start.value
    return datetime(start.value.year, start.value.month, start.value.day)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def compute_span(
    start: When,
    end: When,
    recurrence: Sequence[str] = (),
    extra: Iterable[tuple[When, When]] = (),
) -> tuple[datetime, datetime]:
    """Time range from the first occurrence's start to the last occurrence's end.

    An RRULE without COUNT/UNTIL, or one that cannot be parsed, is treated as
    unbounded so the series is never mistaken for "already over".
    """
    lo = start.utc()
    duration = end.utc() - lo
    last_start = lo
    unbounded = False
    for line in recurrence:
        name, _, value = line.partition(":")
        if name.split(";", 1)[0].upper() != "RRULE":
            continue
        upper = value.upper()
        if "COUNT=" not in upper and "UNTIL=" not in upper:
            unbounded = True
            continue
        try:
            occurrences = list(rrulestr(value, dtstart=_rrule_anchor(start)))
        except (ValueError, TypeError):
            unbounded = True
            continue
        if occurrences:
            last_start = max(last_start, _as_utc(occurrences[-1]))
    hi = MAX_TIME if unbounded else last_start + duration
    for occ_start, occ_end in extra:
        lo = min(lo, occ_start.utc())
        if not unbounded:
            hi = max(hi, occ_end.utc())
    return lo, hi


def series_span(series: Series) -> tuple[datetime, datetime]:
    return compute_span(
        series.start,
        series.end,
        series.recurrence,
        ((o.start, o.end) for o in series.overrides),
    )
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `15 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/factories.py tests/test_model.py src/calsync/model.py
git commit -m "feat(model): unified event model with hashing, spans and alarm policies" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 时长解析与配置加载

解析 TOML 配置并一次性收集全部问题；代码中不出现任何具体账号。

**Files:**
- Create: `tests/test_config.py`
- Create: `src/calsync/durations.py`
- Create: `src/calsync/config.py`

**Interfaces:**
- Consumes:
  - `calsync.model.AlarmPolicy`
  - `calsync.errors.ConfigError`
- Produces:
  - `parse_duration(text: str) -> timedelta`（`30s`、`5m`、`2h`、`7d`、`1w`、`-15m`；非法时抛 `ValueError`）
  - 数据类：`CredentialRef(backend, ref)`、`Account(name, provider, credential, server_url=None, username=None, max_concurrency=2)`、`CalendarSel(account, calendar, create_if_missing=False)`、`WindowSpec(past, future)`、`DeleteGuard(max_ratio=0.5, max_count=10)`、`Rule(name, source, target, window, alarms, delete_guard)`、`NotifyConfig`、`RuntimeConfig(interval=60s, jitter=0.1)`、`Config(accounts, rules, google_client_secret_file=None, notify, runtime)`
  - `parse_config(data: Mapping, base_dir: Path) -> Config`、`load_config(path: Path) -> Config`、`default_config_path(environ=os.environ) -> Path`

**注意：**
- 第一版只允许 `google` 作源、`caldav` 作目标；违反时报配置错误。
- 相对路径（如 `client_secret_file`）相对配置文件所在目录解析。

- [ ] **Step 1: 写失败的测试**

`tests/test_config.py`：

````python
from datetime import timedelta
from pathlib import Path

import pytest

from calsync.config import CredentialRef, default_config_path, load_config, parse_config
from calsync.durations import parse_duration
from calsync.errors import ConfigError

VALID = {
    "google": {"client_secret_file": "google-client.json"},
    "notify": {"bark_url": "https://api.day.app/KEY"},
    "runtime": {"interval": "90s"},
    "accounts": {
        "me-google": {"provider": "google", "credential": {"backend": "keychain", "ref": "calsync/me"}},
        "family-icloud": {
            "provider": "caldav",
            "server_url": "https://caldav.icloud.com/",
            "username": "family@example.com",
            "credential": {"backend": "env", "ref": "FAMILY_PW"},
            "max_concurrency": 3,
        },
    },
    "rules": [
        {
            "name": "family",
            "source": {"account": "me-google", "calendar": "Family"},
            "target": {"account": "family-icloud", "calendar": "Family", "create_if_missing": True},
            "window": {"past": "7d", "future": "180d"},
            "alarms": ["-5m", "0m"],
        }
    ],
}


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("5m", 300), ("2h", 7200), ("7d", 604800), ("1w", 604800), ("-15m", -900), ("0m", 0)],
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["", "5", "m5", "5 minutes", "1.5h"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration(text)


def test_parse_valid_config(tmp_path: Path) -> None:
    cfg = parse_config(VALID, base_dir=tmp_path)
    assert cfg.google_client_secret_file == tmp_path / "google-client.json"
    assert cfg.runtime.interval == timedelta(seconds=90)
    assert cfg.accounts["family-icloud"].max_concurrency == 3
    assert cfg.accounts["me-google"].credential == CredentialRef("keychain", "calsync/me")
    (rule,) = cfg.rules
    assert rule.target.create_if_missing is True
    assert rule.alarms == (timedelta(minutes=-5), timedelta(0))
    assert rule.window.future == timedelta(days=180)
    assert rule.delete_guard.max_count == 10


def test_collects_all_problems(tmp_path: Path) -> None:
    broken = {
        "accounts": {
            "g": {"provider": "google", "credential": {"backend": "keychain", "ref": "x"}},
            "bad": {"provider": "exchange", "credential": {"backend": "keychain", "ref": "x"}},
            "dav": {"provider": "caldav", "credential": {"backend": "file", "ref": "/tmp/x"}},
        },
        "rules": [
            {
                "name": "a",
                "source": {"account": "nope", "calendar": "c"},
                "target": {"account": "dav", "calendar": "c"},
            },
            {
                "name": "b",
                "source": {"account": "g", "calendar": "c"},
                "target": {"account": "g", "calendar": "c"},
                "alarms": 5,
            },
            {
                "name": "c",
                "source": {"account": "g", "calendar": "c"},
                "target": {"account": "dav", "calendar": "c"},
                "window": {"past": "soon"},
            },
        ],
    }
    with pytest.raises(ConfigError) as info:
        parse_config(broken, base_dir=tmp_path)
    text = "\n".join(info.value.problems)
    assert "accounts.bad.provider" in text
    assert "accounts.dav.server_url" in text
    assert "accounts.dav.username" in text
    assert "rules[a].source.account: unknown account 'nope'" in text
    assert "rules[b].target.account: 'g' must be a caldav account" in text
    assert "rules[b].alarms" in text
    assert "rules[c].window.past" in text
    assert "google.client_secret_file" in text


def test_duplicate_rule_names(tmp_path: Path) -> None:
    data = {**VALID, "rules": VALID["rules"] * 2}
    with pytest.raises(ConfigError, match="duplicate rule name"):
        parse_config(data, base_dir=tmp_path)


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "missing.toml")


def test_load_config_reads_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[accounts.d]\nprovider = "caldav"\nserver_url = "https://dav.example.com/"\n'
        'username = "u"\ncredential = { backend = "env", ref = "PW" }\n'
    )
    cfg = load_config(path)
    assert cfg.rules == ()
    assert cfg.accounts["d"].server_url == "https://dav.example.com/"


def test_default_config_path_env_override() -> None:
    assert default_config_path({"CALSYNC_CONFIG": "/etc/calsync.toml"}) == Path("/etc/calsync.toml")
    assert default_config_path({}).name == "config.toml"
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_config.py -v`

Expected: FAIL，收集阶段报错 `ModuleNotFoundError: No module named 'calsync.config'`

- [ ] **Step 3: 写实现**

`src/calsync/durations.py`：

````python
"""Parse short duration strings such as ``30s``, ``5m``, ``7d`` or ``-15m``."""

import re
from datetime import timedelta

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_PATTERN = re.compile(r"^([+-]?)(\d+)([smhdw])$")


def parse_duration(text: str) -> timedelta:
    match = _PATTERN.match(text.strip())
    if match is None:
        raise ValueError(f"invalid duration {text!r}; expected e.g. 30s, 5m, 2h, 7d, -15m")
    sign, amount, unit = match.groups()
    seconds = int(amount) * _UNITS[unit]
    return timedelta(seconds=-seconds if sign == "-" else seconds)
````

`src/calsync/config.py`：

````python
"""Load and validate the TOML configuration.

Validation collects every problem before failing so a user fixes their file
in one pass. Nothing in the code refers to a concrete account: everything
comes from here.
"""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from calsync.durations import parse_duration
from calsync.errors import ConfigError
from calsync.model import AlarmPolicy

Backend = Literal["keychain", "env", "file"]
Provider = Literal["google", "caldav"]
_BACKENDS = ("keychain", "env", "file")
_PROVIDERS = ("google", "caldav")


@dataclass(frozen=True, slots=True)
class CredentialRef:
    backend: Backend
    ref: str


@dataclass(frozen=True, slots=True)
class Account:
    name: str
    provider: Provider
    credential: CredentialRef
    server_url: str | None = None
    username: str | None = None
    max_concurrency: int = 2


@dataclass(frozen=True, slots=True)
class CalendarSel:
    account: str
    calendar: str
    create_if_missing: bool = False


@dataclass(frozen=True, slots=True)
class WindowSpec:
    past: timedelta
    future: timedelta


@dataclass(frozen=True, slots=True)
class DeleteGuard:
    max_ratio: float = 0.5
    max_count: int = 10


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    source: CalendarSel
    target: CalendarSel
    window: WindowSpec
    alarms: AlarmPolicy
    delete_guard: DeleteGuard


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    bark_url: str | None = None
    webhook_url: str | None = None
    failure_threshold: int = 3


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    interval: timedelta = timedelta(seconds=60)
    jitter: float = 0.1


@dataclass(frozen=True, slots=True)
class Config:
    accounts: Mapping[str, Account]
    rules: tuple[Rule, ...]
    google_client_secret_file: Path | None = None
    notify: NotifyConfig = NotifyConfig()
    runtime: RuntimeConfig = RuntimeConfig()


def default_config_path(environ: Mapping[str, str] = os.environ) -> Path:
    explicit = environ.get("CALSYNC_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".config" / "calsync" / "config.toml"


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError([f"config file not found: {path}"]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{path}: {exc}"]) from None
    return parse_config(data, base_dir=path.parent)


def _resolve(raw: str, base_dir: Path) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else base_dir / path


def _duration(raw: Any, where: str, problems: list[str]) -> timedelta | None:
    if not isinstance(raw, str):
        problems.append(f"{where}: expected a duration string such as '5m'")
        return None
    try:
        return parse_duration(raw)
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
        return None


def _account(name: str, raw: Any, problems: list[str]) -> Account | None:
    where = f"accounts.{name}"
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be a table")
        return None
    provider = raw.get("provider")
    if provider not in _PROVIDERS:
        problems.append(f"{where}.provider: must be one of {', '.join(_PROVIDERS)}")
        return None
    cred = raw.get("credential")
    if (
        not isinstance(cred, Mapping)
        or cred.get("backend") not in _BACKENDS
        or not isinstance(cred.get("ref"), str)
        or not cred["ref"]
    ):
        problems.append(f'{where}.credential: expected {{ backend = keychain|env|file, ref = "..." }}')
        return None
    server_url = raw.get("server_url")
    username = raw.get("username")
    if provider == "caldav":
        if not isinstance(server_url, str) or not server_url.startswith(("https://", "http://")):
            problems.append(f"{where}.server_url: required http(s) URL for caldav accounts")
        if not isinstance(username, str) or not username:
            problems.append(f"{where}.username: required for caldav accounts")
    concurrency = raw.get("max_concurrency", 2)
    if not isinstance(concurrency, int) or concurrency < 1:
        problems.append(f"{where}.max_concurrency: must be an integer >= 1")
        concurrency = 1
    return Account(
        name=name,
        provider=provider,
        credential=CredentialRef(backend=cred["backend"], ref=cred["ref"]),
        server_url=server_url,
        username=username,
        max_concurrency=concurrency,
    )


def _selection(
    raw: Any,
    where: str,
    accounts: Mapping[str, Account],
    provider: Provider,
    problems: list[str],
) -> CalendarSel | None:
    if not isinstance(raw, Mapping):
        problems.append(f'{where}: expected {{ account = "...", calendar = "..." }}')
        return None
    account, calendar = raw.get("account"), raw.get("calendar")
    ok = True
    if not isinstance(calendar, str) or not calendar:
        problems.append(f"{where}.calendar: required")
        ok = False
    if not isinstance(account, str) or account not in accounts:
        problems.append(f"{where}.account: unknown account {account!r}")
        return None
    if accounts[account].provider != provider:
        problems.append(f"{where}.account: {account!r} must be a {provider} account")
    if not ok or not isinstance(calendar, str):
        return None
    return CalendarSel(account, calendar, bool(raw.get("create_if_missing", False)))


def _alarms(raw: Any, where: str, problems: list[str]) -> AlarmPolicy:
    if raw in ("copy", "strip"):
        return raw
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        offsets = [_duration(item, where, problems) for item in raw]
        return tuple(o for o in offsets if o is not None)
    problems.append(f'{where}: expected "copy", "strip" or a list such as ["-5m", "0m"]')
    return "copy"


def _rule(index: int, raw: Any, accounts: Mapping[str, Account], problems: list[str]) -> Rule | None:
    where = f"rules[{index}]"
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be a table")
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        problems.append(f"{where}.name: required")
        return None
    where = f"rules[{name}]"
    source = _selection(raw.get("source"), f"{where}.source", accounts, "google", problems)
    target = _selection(raw.get("target"), f"{where}.target", accounts, "caldav", problems)
    window_raw = raw.get("window", {})
    past = _duration(window_raw.get("past", "7d"), f"{where}.window.past", problems)
    future = _duration(window_raw.get("future", "180d"), f"{where}.window.future", problems)
    for label, value in (("past", past), ("future", future)):
        if value is not None and value < timedelta(0):
            problems.append(f"{where}.window.{label}: must not be negative")
    alarms = _alarms(raw.get("alarms", "copy"), f"{where}.alarms", problems)
    guard_raw = raw.get("delete_guard", {})
    guard = DeleteGuard(
        max_ratio=float(guard_raw.get("max_ratio", 0.5)),
        max_count=int(guard_raw.get("max_count", 10)),
    )
    if not 0 < guard.max_ratio <= 1:
        problems.append(f"{where}.delete_guard.max_ratio: must be in (0, 1]")
    if source is None or target is None or past is None or future is None:
        return None
    return Rule(name, source, target, WindowSpec(past, future), alarms, guard)


def parse_config(data: Mapping[str, Any], base_dir: Path) -> Config:
    problems: list[str] = []

    google = data.get("google", {})
    secret_file = google.get("client_secret_file")
    client_secret_file = _resolve(secret_file, base_dir) if isinstance(secret_file, str) else None

    notify_raw = data.get("notify", {})
    notify = NotifyConfig(
        bark_url=notify_raw.get("bark_url"),
        webhook_url=notify_raw.get("webhook_url"),
        failure_threshold=int(notify_raw.get("failure_threshold", 3)),
    )

    runtime_raw = data.get("runtime", {})
    interval = _duration(runtime_raw.get("interval", "60s"), "runtime.interval", problems)
    jitter = float(runtime_raw.get("jitter", 0.1))
    if not 0 <= jitter < 1:
        problems.append("runtime.jitter: must be in [0, 1)")

    accounts: dict[str, Account] = {}
    for name, raw in (data.get("accounts") or {}).items():
        account = _account(name, raw, problems)
        if account is not None:
            accounts[name] = account

    rules: list[Rule] = []
    seen: set[str] = set()
    for index, raw in enumerate(data.get("rules") or []):
        rule = _rule(index, raw, accounts, problems)
        if rule is None:
            continue
        if rule.name in seen:
            problems.append(f"rules[{rule.name}]: duplicate rule name")
        seen.add(rule.name)
        rules.append(rule)

    if any(a.provider == "google" for a in accounts.values()) and client_secret_file is None:
        problems.append("google.client_secret_file: required when a google account is configured")

    if problems:
        raise ConfigError(problems)
    return Config(
        accounts=accounts,
        rules=tuple(rules),
        google_client_secret_file=client_secret_file,
        notify=notify,
        runtime=RuntimeConfig(interval=interval or timedelta(seconds=60), jitter=jitter),
    )
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `33 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/test_config.py src/calsync/durations.py src/calsync/config.py
git commit -m "feat(config): TOML config loading with exhaustive validation" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 凭证后端

支持 macOS 钥匙串、环境变量、0600 权限文件三种凭证后端。

**Files:**
- Create: `tests/test_credentials.py`
- Create: `src/calsync/credentials.py`

**Interfaces:**
- Consumes:
  - `calsync.config.CredentialRef`
  - `calsync.errors.AuthError`、`ConfigError`
- Produces:
  - `read_secret(ref: CredentialRef, *, environ=os.environ) -> str`：缺失时抛 `AuthError`；文件权限不是 0600 时抛 `ConfigError`
  - `write_secret(ref: CredentialRef, value: str) -> None`：`env` 后端只读，抛 `ConfigError`
  - 钥匙串条目：service = `ref.ref`，user = `"calsync"`；`keyring` 在函数内延迟导入（测试通过替换 `credentials._keyring` 注入假实现）

**注意：**
- 模块名是 `credentials.py`，不要叫 `secrets.py`，避免和标准库同名。

- [ ] **Step 1: 写失败的测试**

`tests/test_credentials.py`：

````python
import os
from pathlib import Path

import pytest

from calsync import credentials
from calsync.config import CredentialRef
from calsync.credentials import read_secret, write_secret
from calsync.errors import AuthError, ConfigError


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str) -> str | None:
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        self.store[(service, user)] = value


def test_env_backend() -> None:
    ref = CredentialRef("env", "PW")
    assert read_secret(ref, environ={"PW": "s3cret"}) == "s3cret"
    with pytest.raises(AuthError, match="PW is not set"):
        read_secret(ref, environ={})
    with pytest.raises(ConfigError):
        write_secret(ref, "x")


def test_file_backend_roundtrip_and_mode(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "secret"
    ref = CredentialRef("file", str(path))
    write_secret(ref, "abc-def\n")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert read_secret(ref) == "abc-def"
    os.chmod(path, 0o644)
    with pytest.raises(ConfigError, match="must have mode 0600"):
        read_secret(ref)


def test_file_backend_missing(tmp_path: Path) -> None:
    with pytest.raises(AuthError, match="not found"):
        read_secret(CredentialRef("file", str(tmp_path / "nope")))


def test_keychain_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKeyring()
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)
    ref = CredentialRef("keychain", "calsync/test")
    with pytest.raises(AuthError, match="calsync auth"):
        read_secret(ref)
    write_secret(ref, "pw")
    assert fake.store == {("calsync/test", "calsync"): "pw"}
    assert read_secret(ref) == "pw"
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_credentials.py -v`

Expected: FAIL，收集阶段报错 `ImportError: cannot import name 'credentials' from 'calsync'`

- [ ] **Step 3: 写实现**

`src/calsync/credentials.py`：

````python
"""Credential backends: macOS keychain (via keyring), environment variables, 0600 files."""

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

from calsync.config import CredentialRef
from calsync.errors import AuthError, ConfigError

KEYRING_USER = "calsync"


def _keyring() -> ModuleType:
    import keyring  # imported lazily: only the keychain backend needs it

    return keyring


def read_secret(ref: CredentialRef, *, environ: Mapping[str, str] = os.environ) -> str:
    if ref.backend == "env":
        value = environ.get(ref.ref)
        if not value:
            raise AuthError(f"environment variable {ref.ref} is not set")
        return value
    if ref.backend == "file":
        path = Path(ref.ref).expanduser()
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            raise AuthError(f"credential file not found: {path}") from None
        if mode & 0o077:
            raise ConfigError([f"credential file {path} must have mode 0600 (found {mode:o})"])
        return path.read_text().strip()
    value = _keyring().get_password(ref.ref, KEYRING_USER)
    if not value:
        raise AuthError(f"no keychain entry {ref.ref!r}; run `calsync auth <account>`")
    return value


def write_secret(ref: CredentialRef, value: str) -> None:
    if ref.backend == "env":
        raise ConfigError([f"cannot store a secret in environment variable {ref.ref}"])
    if ref.backend == "file":
        path = Path(ref.ref).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        os.chmod(path, 0o600)
        return
    _keyring().set_password(ref.ref, KEYRING_USER, value)
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `37 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/test_credentials.py src/calsync/credentials.py
git commit -m "feat(credentials): keychain, env and 0600-file secret backends" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Google 事件映射（纯函数）

把 Google Calendar API 的事件资源转成统一模型：重复系列、单次改动、取消的实例、提醒、HTML 描述。

**Files:**
- Create: `tests/test_google_map.py`
- Create: `src/calsync/icalutil.py`
- Create: `src/calsync/sources/__init__.py`
- Create: `src/calsync/sources/base.py`
- Create: `src/calsync/sources/google_map.py`

**Interfaces:**
- Consumes:
  - `calsync.model`：`When`、`Alarm`、`Occurrence`、`Series`、`normalize_series`
- Produces:
  - `calsync.icalutil.when_from_ical(value, default_tz="UTC") -> When`、`parse_date_line(line, default_tz) -> list[When]`
  - `calsync.sources.base`：`RawEvent = Mapping[str, Any]`、`CalendarInfo(id, name)`、`CalendarMeta(timezone="UTC", default_alarms=())`、`CalendarPull(calendar_id, full, upserts=[], deletes=[], meta=CalendarMeta())`、`SourcePull(calendars: dict[str, CalendarPull], state: dict)`、`Source` 协议（`list_calendars`、`pull(calendar_refs, state)`、`to_series(raw_events, meta)`）
  - `calsync.sources.google_map`：`html_to_text(text) -> str`、`parse_when(obj, default_tz) -> When`、`alarms_from_reminders(reminders) -> tuple[Alarm, ...]`、`event_alarms(event, defaults)`、`split_recurrence(lines, tz)`、`build_series(raw_events, meta) -> list[Series]`

**注意：**
- 永远不读取 `attendees`、`organizer`、会议链接：目标服务器因此不可能替用户发出邀请。
- 只转换 `popup` 提醒；`email` 提醒忽略。`reminders.useDefault` 为真（或缺省）时使用日历默认提醒。
- 主事件不存在的孤立实例只记 debug 日志并跳过；格式错误的事件记 warning 并跳过，不影响其他事件。

- [ ] **Step 1: 写失败的测试**

`tests/test_google_map.py`：

````python
from datetime import date, datetime, timedelta

from calsync.model import Alarm, When
from calsync.sources.base import CalendarMeta
from calsync.sources.google_map import build_series, html_to_text, parse_when
from tests.factories import TZ, ZONE, google_event

META = CalendarMeta(timezone=TZ, default_alarms=(Alarm(timedelta(minutes=-10)),))


def test_html_to_text() -> None:
    assert html_to_text("plain text") == "plain text"
    assert html_to_text("<p>Step one\nStep two</p>") == "Step one\nStep two"
    assert html_to_text("a<br>b<br/>c &amp; d") == "a\nb\nc & d"
    assert html_to_text("<ul><li>x</li><li>y</li></ul>") == "x\ny"


def test_parse_when() -> None:
    w = parse_when({"dateTime": "2026-10-23T22:10:00+08:00", "timeZone": TZ}, "UTC")
    assert w == When(datetime(2026, 10, 23, 22, 10, tzinfo=ZONE), TZ)
    utc = parse_when({"dateTime": "2026-10-23T14:10:00Z"}, TZ)
    assert utc.tzid == TZ and utc.value.hour == 22
    assert parse_when({"date": "2026-10-23"}, TZ) == When(date(2026, 10, 23))


def test_single_event_with_reminder_override() -> None:
    raw = google_event(
        "e1",
        "Review",
        "2026-10-23T09:30:00+08:00",
        "2026-10-23T10:00:00+08:00",
        description="<p>Bring card</p>",
        transparency="transparent",
        attendees=[{"email": "someone@example.com"}],
    )
    (s,) = build_series([raw], META)
    assert s.source_id == "e1"
    assert s.description == "Bring card"
    assert s.alarms == (Alarm(timedelta(0)),)
    assert s.transparent is True


def test_default_and_email_reminders() -> None:
    uses_default = google_event(
        "e1", "A", "2026-10-23T09:00:00+08:00", "2026-10-23T09:05:00+08:00", reminders={"useDefault": True}
    )
    email_only = google_event(
        "e2",
        "B",
        "2026-10-23T09:00:00+08:00",
        "2026-10-23T09:05:00+08:00",
        reminders={"useDefault": False, "overrides": [{"method": "email", "minutes": 30}]},
    )
    by_id = {s.source_id: s for s in build_series([uses_default, email_only], META)}
    assert by_id["e1"].alarms == (Alarm(timedelta(minutes=-10)),)
    assert by_id["e2"].alarms == ()


def test_recurring_series_with_modified_and_cancelled_instances() -> None:
    master = google_event(
        "m1",
        "Standup",
        "2026-10-23T09:30:00+08:00",
        "2026-10-23T09:35:00+08:00",
        recurrence=["RRULE:FREQ=DAILY;COUNT=21", "EXDATE;TZID=Asia/Shanghai:20261030T093000"],
    )
    modified = google_event(
        "m1_20261024T013000Z",
        "Standup 2/21",
        "2026-10-24T21:00:00+08:00",
        "2026-10-24T21:05:00+08:00",
        recurringEventId="m1",
        originalStartTime={"dateTime": "2026-10-24T09:30:00+08:00", "timeZone": TZ},
    )
    cancelled = {
        "id": "m1_20261025T013000Z",
        "status": "cancelled",
        "recurringEventId": "m1",
        "originalStartTime": {"dateTime": "2026-10-25T09:30:00+08:00", "timeZone": TZ},
    }
    (s,) = build_series([modified, master, cancelled], META)
    assert s.recurrence == ("RRULE:FREQ=DAILY;COUNT=21",)
    assert [w.value.day for w in s.exdates] == [25, 30]
    (occ,) = s.overrides
    assert occ.title == "Standup 2/21"
    assert occ.original_start.value == datetime(2026, 10, 24, 9, 30, tzinfo=ZONE)
    assert occ.start.value.hour == 21
    assert occ.alarms == (Alarm(timedelta(0)),)


def test_cancelled_and_orphan_entries_are_dropped() -> None:
    gone = {"id": "x", "status": "cancelled"}
    orphan = google_event(
        "o_1",
        "Orphan",
        "2026-10-24T09:00:00+08:00",
        "2026-10-24T09:05:00+08:00",
        recurringEventId="missing",
        originalStartTime={"dateTime": "2026-10-24T09:00:00+08:00"},
    )
    broken = {"id": "b", "status": "confirmed", "summary": "no start"}
    assert build_series([gone, orphan, broken], META) == []


def test_all_day_event() -> None:
    raw = {
        "id": "d1",
        "status": "confirmed",
        "summary": "Checkup",
        "start": {"date": "2026-11-05"},
        "end": {"date": "2026-11-06"},
    }
    (s,) = build_series([raw], META)
    assert s.start == When(date(2026, 11, 5)) and s.end == When(date(2026, 11, 6))
    assert s.alarms == META.default_alarms
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_google_map.py -v`

Expected: FAIL，收集阶段报错 `ModuleNotFoundError: No module named 'calsync.sources'`

- [ ] **Step 3: 写实现**

`src/calsync/icalutil.py`：

````python
"""Small iCalendar helpers shared by the Google mapper and the renderer."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from icalendar import Event

from calsync.model import When


def when_from_ical(value: datetime | date, default_tz: str = "UTC") -> When:
    """Convert a decoded iCalendar value into a When, keeping its IANA zone."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(default_tz))
        key = getattr(value.tzinfo, "key", None)
        if key is None:
            return When(value.astimezone(UTC), "UTC")
        return When(value, key)
    return When(value)


def parse_date_line(line: str, default_tz: str) -> list[When]:
    """Parse one EXDATE or RDATE content line, e.g. ``EXDATE;TZID=Asia/Shanghai:20261025T221000``."""
    name = line.split(":", 1)[0].split(";", 1)[0].upper()
    component = Event.from_ical(f"BEGIN:VEVENT\r\n{line}\r\nEND:VEVENT\r\n")
    prop = component.get(name)
    if prop is None:
        return []
    props = prop if isinstance(prop, list) else [prop]
    return [when_from_ical(item.dt, default_tz) for p in props for item in p.dts]
````

`src/calsync/sources/__init__.py`：空文件（`touch src/calsync/sources/__init__.py`）。

`src/calsync/sources/base.py`：

````python
"""Source adapter interface.

A source owns its own incremental-sync cursors inside an opaque, JSON
serializable ``state`` dict. The engine persists that dict together with the
mirror in one transaction and hands it back on the next pull.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from calsync.model import Alarm, Series

RawEvent = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CalendarInfo:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class CalendarMeta:
    timezone: str = "UTC"
    default_alarms: tuple[Alarm, ...] = ()


@dataclass(slots=True)
class CalendarPull:
    calendar_id: str  # resolved ID, even if the rule referenced the calendar by name
    full: bool  # True: ``upserts`` is the complete calendar, replace the mirror
    upserts: list[dict[str, Any]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    meta: CalendarMeta = CalendarMeta()


@dataclass(slots=True)
class SourcePull:
    calendars: dict[str, CalendarPull]  # keyed by the calendar reference passed in
    state: dict[str, Any]


class Source(Protocol):
    async def list_calendars(self) -> list[CalendarInfo]: ...

    async def pull(self, calendar_refs: Sequence[str], state: Mapping[str, Any]) -> SourcePull: ...

    def to_series(self, raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Series]: ...
````

`src/calsync/sources/google_map.py`：

````python
"""Map Google Calendar API event resources to the unified model (pure functions)."""

import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

from calsync.icalutil import parse_date_line
from calsync.model import Alarm, Occurrence, Series, When, normalize_series
from calsync.sources.base import CalendarMeta, RawEvent

log = logging.getLogger(__name__)

_BLOCK_END = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_END:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(text: str) -> str:
    """Google stores rich descriptions as HTML; targets want plain text."""
    if "<" not in text and "&" not in text:
        return text.strip()
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    joined = "".join(parser.parts)
    lines = [line.rstrip() for line in joined.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_when(obj: Mapping[str, Any], default_tz: str) -> When:
    if "date" in obj:
        return When(date.fromisoformat(obj["date"]))
    tzid = obj.get("timeZone") or default_tz
    zone = ZoneInfo(tzid)
    value = datetime.fromisoformat(obj["dateTime"])
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return When(value.astimezone(zone), tzid)


def alarms_from_reminders(reminders: Iterable[Mapping[str, Any]]) -> tuple[Alarm, ...]:
    return tuple(
        Alarm(timedelta(minutes=-int(r.get("minutes", 0)))) for r in reminders if r.get("method") == "popup"
    )


def event_alarms(event: RawEvent, defaults: tuple[Alarm, ...]) -> tuple[Alarm, ...]:
    reminders = event.get("reminders") or {}
    if reminders.get("useDefault", True):
        return defaults
    return alarms_from_reminders(reminders.get("overrides") or [])


def split_recurrence(lines: Iterable[str], tz: str) -> tuple[tuple[str, ...], tuple[When, ...]]:
    rules: list[str] = []
    exdates: list[When] = []
    for line in lines:
        head = line.split(":", 1)[0].split(";", 1)[0].upper()
        if head == "EXDATE":
            exdates.extend(parse_date_line(line, tz))
        elif head in ("RRULE", "RDATE"):
            rules.append(line)
        else:
            log.debug("ignoring recurrence line %r", line)
    return tuple(rules), tuple(exdates)


def _series(master: RawEvent, exceptions: list[RawEvent], meta: CalendarMeta) -> Series:
    tz = master.get("start", {}).get("timeZone") or meta.timezone
    rules, exdates = split_recurrence(master.get("recurrence") or (), tz)
    excluded = list(exdates)
    overrides: list[Occurrence] = []
    if rules:
        for ex in exceptions:
            original = ex.get("originalStartTime")
            if not original:
                continue
            original_start = parse_when(original, tz)
            if ex.get("status") == "cancelled":
                excluded.append(original_start)
                continue
            overrides.append(
                Occurrence(
                    original_start=original_start,
                    start=parse_when(ex["start"], tz),
                    end=parse_when(ex["end"], tz),
                    title=ex.get("summary", ""),
                    description=html_to_text(ex.get("description", "")),
                    location=ex.get("location", ""),
                    alarms=event_alarms(ex, meta.default_alarms),
                )
            )
    return normalize_series(
        Series(
            source_id=master["id"],
            title=master.get("summary", ""),
            description=html_to_text(master.get("description", "")),
            location=master.get("location", ""),
            start=parse_when(master["start"], tz),
            end=parse_when(master["end"], tz),
            recurrence=rules,
            exdates=tuple(excluded),
            overrides=tuple(overrides),
            alarms=event_alarms(master, meta.default_alarms),
            transparent=master.get("transparency") == "transparent",
        )
    )


def build_series(raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Series]:
    """Group mirror entries into series: masters plus their modified/cancelled instances.

    Attendees, organizer and conference data are deliberately never read, so
    a target server can never send invitations on our behalf.
    """
    masters: dict[str, RawEvent] = {}
    exceptions: dict[str, list[RawEvent]] = defaultdict(list)
    for event in raw_events:
        parent = event.get("recurringEventId")
        if parent:
            exceptions[parent].append(event)
        elif event.get("status") != "cancelled":
            masters[event["id"]] = event
    result: list[Series] = []
    for master_id, master in masters.items():
        try:
            result.append(_series(master, exceptions.pop(master_id, []), meta))
        except (KeyError, ValueError) as exc:
            log.warning("skipping malformed event %s: %s", master_id, exc)
    for master_id, orphans in exceptions.items():
        log.debug("ignoring %d instance(s) of unknown series %s", len(orphans), master_id)
    return result
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `44 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/test_google_map.py src/calsync/icalutil.py src/calsync/sources/__init__.py src/calsync/sources/base.py src/calsync/sources/google_map.py
git commit -m "feat(google): map Google events to the unified model" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: 确定性命名与 iCalendar 渲染

生成可从文件名识别归属的资源名，把 Series 渲染成 .ics，并能从已写入的资源读回标记、哈希和跨度。

**Files:**
- Create: `tests/test_render.py`
- Create: `src/calsync/naming.py`
- Create: `src/calsync/render.py`

**Interfaces:**
- Consumes:
  - `calsync.model`：`Series`、`Alarm`、`When`、`compute_span`
  - `calsync.icalutil`：`parse_date_line`、`when_from_ical`
- Produces:
  - `calsync.naming`：`rule_tag(rule_name) -> str`（8 位）、`object_id(rule_name, source_calendar_id, source_id) -> str`（24 位）、`resource_name(rule_name, oid) -> "calsync-<tag>-<oid>.ics"`、`uid_for(oid) -> "<oid>@calsync"`、`parse_resource_name(href) -> tuple[tag, oid] | None`
  - `calsync.render`：`render(series, *, uid, rule_name, content_hash, now) -> bytes`、`Inspected(uid, rule, content_hash, span)`、`inspect(data: bytes) -> Inspected | None`、常量 `PRODID`

**注意：**
- 每个 `VEVENT`（含带 `RECURRENCE-ID` 的单次改动）都写 `X-CALSYNC-RULE` 和 `X-CALSYNC-HASH`，并各自带完整的 `VALARM`。
- 时区定义由 `icalendar` 的 `add_missing_timezones()` 生成。零分钟提醒序列化为 `TRIGGER:P0D`，这是合法写法。

- [ ] **Step 1: 写失败的测试**

`tests/test_render.py`：

````python
from datetime import UTC, datetime, timedelta

from calsync.model import Alarm, Occurrence, series_hash, series_span
from calsync.naming import object_id, parse_resource_name, resource_name, rule_tag, uid_for
from calsync.render import inspect, render
from tests.factories import at, day, series

NOW = datetime(2026, 10, 24, tzinfo=UTC)


def test_naming_is_deterministic_and_parseable() -> None:
    oid = object_id("family", "cal@group.calendar.google.com", "evt1")
    assert oid == object_id("family", "cal@group.calendar.google.com", "evt1")
    assert oid != object_id("other", "cal@group.calendar.google.com", "evt1")
    assert len(oid) == 24 and len(rule_tag("family")) == 8
    name = resource_name("family", oid)
    assert name == f"calsync-{rule_tag('family')}-{oid}.ics"
    assert parse_resource_name(f"/1234/calendars/home/{name}") == (rule_tag("family"), oid)
    assert parse_resource_name("/1234/calendars/home/ABC-DEF.ics") is None
    assert uid_for(oid) == f"{oid}@calsync"


def _recurring():
    occ = Occurrence(
        original_start=at(2026, 10, 24, 9, 30),
        start=at(2026, 10, 24, 21),
        end=at(2026, 10, 24, 21, 5),
        title="Standup (late)",
        description="",
        location="",
        alarms=(Alarm(timedelta(minutes=-5)),),
    )
    return series(
        recurrence=("RRULE:FREQ=DAILY;COUNT=21",),
        exdates=(at(2026, 10, 30, 9, 30),),
        overrides=(occ,),
    )


def test_render_recurring_series() -> None:
    s = _recurring()
    ics = render(s, uid="abc@calsync", rule_name="family", content_hash="h1", now=NOW).decode()
    assert "BEGIN:VTIMEZONE" in ics and "TZID:Asia/Shanghai" in ics
    assert "RRULE:FREQ=DAILY;COUNT=21" in ics
    assert "EXDATE;TZID=Asia/Shanghai:20261030T093000" in ics
    assert "RECURRENCE-ID;TZID=Asia/Shanghai:20261024T093000" in ics
    assert ics.count("BEGIN:VALARM") == 2  # master + override, each with its own alarm
    assert "TRIGGER:-PT5M" in ics and "TRIGGER:P0D" in ics
    assert ics.count("X-CALSYNC-RULE:family") == 2
    assert "ATTENDEE" not in ics and "ORGANIZER" not in ics


def test_render_all_day_and_no_alarms() -> None:
    s = series(start=day(2026, 11, 5), alarms=())
    ics = render(s, uid="d@calsync", rule_name="r", content_hash="h", now=NOW).decode()
    assert "DTSTART;VALUE=DATE:20261105" in ics
    assert "VALARM" not in ics


def test_inspect_roundtrip() -> None:
    s = _recurring()
    h = series_hash(s)
    found = inspect(render(s, uid="abc@calsync", rule_name="family", content_hash=h, now=NOW))
    assert found is not None
    assert (found.uid, found.rule, found.content_hash) == ("abc@calsync", "family", h)
    assert found.span == series_span(s)


def test_inspect_foreign_and_garbage() -> None:
    foreign = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:f1\r\n"
        b"DTSTAMP:20261024T000000Z\r\nDTSTART:20261025T010000Z\r\nDTEND:20261025T020000Z\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    found = inspect(foreign)
    assert found is not None and found.rule is None and found.content_hash is None
    assert found.span == (datetime(2026, 10, 25, 1, tzinfo=UTC), datetime(2026, 10, 25, 2, tzinfo=UTC))
    assert inspect(b"not ical") is None
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_render.py -v`

Expected: FAIL，收集阶段报错 `ModuleNotFoundError: No module named 'calsync.naming'`

- [ ] **Step 3: 写实现**

`src/calsync/naming.py`：

````python
"""Deterministic identities for managed resources.

The resource name alone says "calsync owns this, for rule X", so ownership
can be decided from a listing without downloading any event body.
"""

import hashlib
import re
from urllib.parse import unquote

_NAME = re.compile(r"^calsync-([0-9a-f]{8})-([0-9a-f]{24})\.ics$")


def _digest(text: str, length: int) -> str:
    return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()[:length]


def rule_tag(rule_name: str) -> str:
    return _digest(f"rule\0{rule_name}", 8)


def object_id(rule_name: str, source_calendar_id: str, source_id: str) -> str:
    return _digest(f"{rule_name}\0{source_calendar_id}\0{source_id}", 24)


def resource_name(rule_name: str, oid: str) -> str:
    return f"calsync-{rule_tag(rule_name)}-{oid}.ics"


def uid_for(oid: str) -> str:
    return f"{oid}@calsync"


def parse_resource_name(href: str) -> tuple[str, str] | None:
    """Return ``(rule_tag, object_id)`` if the href's last segment is a calsync name."""
    match = _NAME.match(unquote(href.rstrip("/").rsplit("/", 1)[-1]))
    return (match.group(1), match.group(2)) if match else None
````

`src/calsync/render.py`：

````python
"""Render a Series to iCalendar bytes, and inspect bytes we (may have) written."""

from dataclasses import dataclass
from datetime import UTC, datetime

from icalendar import Alarm as IcalAlarm
from icalendar import Calendar, Component, Event, vRecur

from calsync.icalutil import parse_date_line, when_from_ical
from calsync.model import Alarm, Series, When, compute_span

PRODID = "-//calsync//calsync//EN"


def _event(
    uid: str,
    start: When,
    end: When,
    title: str,
    description: str,
    location: str,
    alarms: tuple[Alarm, ...],
    transparent: bool,
    rule_name: str,
    content_hash: str,
    now: datetime,
) -> Event:
    event = Event()
    event.add("uid", uid)
    event.add("dtstamp", now.astimezone(UTC))
    event.add("dtstart", start.value)
    event.add("dtend", end.value)
    event.add("summary", title)
    if description:
        event.add("description", description)
    if location:
        event.add("location", location)
    event.add("transp", "TRANSPARENT" if transparent else "OPAQUE")
    event.add("x-calsync-rule", rule_name)
    event.add("x-calsync-hash", content_hash)
    for alarm in alarms:
        valarm = IcalAlarm()
        valarm.add("action", "DISPLAY")
        valarm.add("description", title or "Reminder")
        valarm.add("trigger", alarm.offset)
        event.add_component(valarm)
    return event


def render(series: Series, *, uid: str, rule_name: str, content_hash: str, now: datetime) -> bytes:
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    master = _event(
        uid,
        series.start,
        series.end,
        series.title,
        series.description,
        series.location,
        series.alarms,
        series.transparent,
        rule_name,
        content_hash,
        now,
    )
    default_tz = series.start.tzid or "UTC"
    for line in series.recurrence:
        name, _, value = line.partition(":")
        head = name.split(";", 1)[0].upper()
        if head == "RRULE":
            master.add("rrule", vRecur.from_ical(value))
        elif head == "RDATE":
            for when in parse_date_line(line, default_tz):
                master.add("rdate", when.value)
    for when in series.exdates:
        master.add("exdate", when.value)
    cal.add_component(master)
    for occ in series.overrides:
        event = _event(
            uid,
            occ.start,
            occ.end,
            occ.title,
            occ.description,
            occ.location,
            occ.alarms,
            series.transparent,
            rule_name,
            content_hash,
            now,
        )
        event.add("recurrence-id", occ.original_start.value)
        cal.add_component(event)
    cal.add_missing_timezones()
    return cal.to_ical()


@dataclass(frozen=True, slots=True)
class Inspected:
    uid: str
    rule: str | None
    content_hash: str | None
    span: tuple[datetime, datetime] | None


def _rrule_lines(component: Component) -> list[str]:
    value = component.get("rrule")
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [f"RRULE:{v.to_ical().decode()}" for v in values]


def _optional(component: Component, name: str) -> str | None:
    value = component.get(name)
    return None if value is None else str(value)


def inspect(data: bytes) -> Inspected | None:
    """Read identity, hash and span from a resource body; None if it is not parseable."""
    try:
        cal = Calendar.from_ical(data)
    except ValueError:
        return None
    events = list(cal.walk("VEVENT"))
    if not events:
        return None
    master = next((e for e in events if e.get("recurrence-id") is None), events[0])
    span: tuple[datetime, datetime] | None
    try:
        start = when_from_ical(master.decoded("dtstart"))
        end = when_from_ical(master.decoded("dtend")) if "dtend" in master else start
        extra = [
            (when_from_ical(e.decoded("dtstart")), when_from_ical(e.decoded("dtend")))
            for e in events
            if e is not master and "dtstart" in e and "dtend" in e
        ]
        span = compute_span(start, end, _rrule_lines(master), extra)
    except (KeyError, ValueError):
        span = None
    return Inspected(
        uid=str(master.get("uid", "")),
        rule=_optional(master, "x-calsync-rule"),
        content_hash=_optional(master, "x-calsync-hash"),
        span=span,
    )
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `49 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/test_render.py src/calsync/naming.py src/calsync/render.py
git commit -m "feat(render): deterministic resource names and iCalendar rendering" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: 规划器与防误删

纯函数比较应有集合与现有集合，产出新建 / 更新 / 删除计划；按跨度判断窗口；拦截可疑的大规模删除。

**Files:**
- Create: `tests/test_planner.py`
- Create: `src/calsync/planner.py`

**Interfaces:**
- Consumes:
  - `calsync.config.DeleteGuard`
  - `calsync.model`：`Series`、`Window`
- Produces:
  - `Desired(object_id, series, content_hash, span)`、`Actual(object_id, href, etag, content_hash, span)`
  - `Plan(create: list[Desired], update: list[tuple[Desired, Actual]], delete: list[Actual])`，属性 `empty`
  - `plan(desired, actual, window) -> Plan`（结果按 object_id 排序，确定性）
  - `check_delete_guard(p, actual_count, guard) -> str | None`、常量 `RATIO_GUARD_MIN = 3`

**注意：**
- 跨度未知（正文无法解析）的托管资源视为"在窗口内"：它是我们的，又不再需要，所以删除。
- 比例检查至少要删 3 条才生效，避免小日历删最后一两条事件时被拦。

- [ ] **Step 1: 写失败的测试**

`tests/test_planner.py`：

````python
from datetime import UTC, datetime, timedelta

from calsync.config import DeleteGuard
from calsync.model import Window
from calsync.planner import Actual, Desired, Plan, check_delete_guard, plan
from tests.factories import series

NOW = datetime(2026, 10, 24, tzinfo=UTC)
WINDOW = Window.around(NOW, timedelta(days=7), timedelta(days=180))
IN = (NOW + timedelta(days=1), NOW + timedelta(days=1, minutes=5))
OLD = (NOW - timedelta(days=30), NOW - timedelta(days=29))
ONGOING = (NOW - timedelta(days=30), NOW + timedelta(days=10))


def want(oid: str, h: str = "h", span=IN) -> Desired:
    return Desired(oid, series(source_id=oid), h, span)


def have(oid: str, h: str | None = "h", span=IN) -> Actual:
    return Actual(oid, f"/cal/{oid}.ics", '"e"', h, span)


def test_create_update_noop() -> None:
    p = plan(
        {"a": want("a"), "b": want("b", "new"), "c": want("c")},
        {"b": have("b", "old"), "c": have("c")},
        WINDOW,
    )
    assert [d.object_id for d in p.create] == ["a"]
    assert [(d.object_id, a.content_hash) for d, a in p.update] == [("b", "old")]
    assert p.delete == []


def test_unknown_remote_hash_forces_update() -> None:
    p = plan({"a": want("a")}, {"a": have("a", None)}, WINDOW)
    assert len(p.update) == 1


def test_delete_only_inside_window() -> None:
    p = plan(
        {}, {"old": have("old", span=OLD), "cur": have("cur"), "unknown": have("unknown", span=None)}, WINDOW
    )
    assert sorted(a.object_id for a in p.delete) == ["cur", "unknown"]


def test_ongoing_series_started_before_window_is_deleted() -> None:
    p = plan({}, {"s": have("s", span=ONGOING)}, WINDOW)
    assert [a.object_id for a in p.delete] == ["s"]


def test_desired_outside_window_is_ignored_or_removed() -> None:
    far = (NOW + timedelta(days=200), NOW + timedelta(days=201))
    assert plan({"f": want("f", span=far)}, {}, WINDOW).empty
    moved = plan({"f": want("f", span=far)}, {"f": have("f")}, WINDOW)
    assert [a.object_id for a in moved.delete] == ["f"]


def test_delete_guard() -> None:
    guard = DeleteGuard(max_ratio=0.5, max_count=10)
    many = Plan(delete=[have(str(i)) for i in range(11)])
    assert "max_count" in (check_delete_guard(many, 100, guard) or "")
    half = Plan(delete=[have(str(i)) for i in range(4)])
    assert "max_ratio" in (check_delete_guard(half, 6, guard) or "")
    assert check_delete_guard(Plan(delete=[have("a"), have("b")]), 2, guard) is None
    assert check_delete_guard(Plan(), 0, guard) is None
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_planner.py -v`

Expected: FAIL，收集阶段报错 `ModuleNotFoundError: No module named 'calsync.planner'`

- [ ] **Step 3: 写实现**

`src/calsync/planner.py`：

````python
"""Pure planning: desired state vs. actual state -> create / update / delete."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from calsync.config import DeleteGuard
from calsync.model import Series, Window

# The ratio check only applies once a plan deletes at least this many events,
# so removing the last one or two events of a small calendar is never blocked.
RATIO_GUARD_MIN = 3


@dataclass(frozen=True, slots=True)
class Desired:
    object_id: str
    series: Series
    content_hash: str
    span: tuple[datetime, datetime]


@dataclass(frozen=True, slots=True)
class Actual:
    object_id: str
    href: str
    etag: str | None
    content_hash: str | None
    span: tuple[datetime, datetime] | None


@dataclass(slots=True)
class Plan:
    create: list[Desired] = field(default_factory=list)
    update: list[tuple[Desired, Actual]] = field(default_factory=list)
    delete: list[Actual] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.create or self.update or self.delete)


def plan(desired: Mapping[str, Desired], actual: Mapping[str, Actual], window: Window) -> Plan:
    result = Plan()
    for oid, want in desired.items():
        if not window.intersects(want.span):
            continue
        have = actual.get(oid)
        if have is None:
            result.create.append(want)
        elif have.content_hash != want.content_hash:
            result.update.append((want, have))
    for oid, have in actual.items():
        want = desired.get(oid)
        if want is not None and window.intersects(want.span):
            continue
        # Unknown span (unparseable body) counts as "in window": it is ours and unwanted.
        if have.span is None or window.intersects(have.span):
            result.delete.append(have)
    result.create.sort(key=lambda d: d.object_id)
    result.update.sort(key=lambda pair: pair[0].object_id)
    result.delete.sort(key=lambda a: a.object_id)
    return result


def check_delete_guard(p: Plan, actual_count: int, guard: DeleteGuard) -> str | None:
    """Return a reason string if the plan deletes suspiciously much, else None."""
    count = len(p.delete)
    if count > guard.max_count:
        return f"plan deletes {count} events, more than max_count={guard.max_count}"
    if count >= RATIO_GUARD_MIN and actual_count and count > guard.max_ratio * actual_count:
        return f"plan deletes {count} of {actual_count} managed events, more than max_ratio={guard.max_ratio}"
    return None
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `55 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/test_planner.py src/calsync/planner.py
git commit -m "feat(planner): pure create/update/delete planning with delete guard" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: 共享 HTTP 层：请求计数与重试

统一的 httpx 客户端工厂（HTTP/2、gzip、代理、超时）、按种类计数的请求日志、带退避的重试。

**Files:**
- Create: `tests/test_http.py`
- Create: `src/calsync/http.py`

**Interfaces:**
- Consumes:
  - `calsync.__version__`
  - `calsync.errors.TransientError`
- Produces:
  - `USER_AGENT`（含 `gzip`，Google 文档要求）
  - `LoggedRequest(method, host, path, kind)`、`RequestLog`：`record(request)`、`count(kind=None)`、`kinds()`、`mark() -> int`、`since(mark) -> list[LoggedRequest]`
  - `make_client(log, *, transport=None, auth=None, timeout=30.0) -> httpx.AsyncClient`（无自定义 transport 时启用 HTTP/2；`trust_env=True` 以读取 `HTTPS_PROXY`）
  - `request_with_retry(client, method, url, *, kind, max_retries=3, sleep=asyncio.sleep, on_throttle=None, **kwargs) -> httpx.Response`：传输错误和 429/5xx 重试，429/503 时调用 `on_throttle`，用尽后抛 `TransientError`；其他 4xx 直接返回给调用方
  - 类型别名 `Sleep = Callable[[float], Awaitable[None]]`

**注意：**
- 每个请求都通过 `extensions={"calsync_kind": kind}` 打上种类标签，请求预算测试依赖它。

- [ ] **Step 1: 写失败的测试**

`tests/test_http.py`：

````python
import httpx
import pytest

from calsync.errors import TransientError
from calsync.http import USER_AGENT, RequestLog, make_client, request_with_retry

pytestmark = pytest.mark.anyio


async def no_sleep(_: float) -> None:
    return None


def scripted(*statuses: int, headers: dict[str, str] | None = None) -> httpx.MockTransport:
    queue = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(queue.pop(0), headers=headers or {}, request=request)

    return httpx.MockTransport(handler)


async def test_client_headers_and_log() -> None:
    log = RequestLog()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with make_client(log, transport=httpx.MockTransport(handler)) as client:
        await request_with_retry(client, "GET", "https://x.test/a", kind="probe", sleep=no_sleep)
    assert seen[0].headers["user-agent"] == USER_AGENT and "gzip" in USER_AGENT
    assert seen[0].headers["accept-encoding"] == "gzip"
    assert log.kinds() == ["probe"] and log.count("probe") == 1


async def test_retries_then_succeeds_and_reports_throttle() -> None:
    log = RequestLog()
    throttled: list[bool] = []
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with make_client(log, transport=scripted(503, 429, 200, headers={"Retry-After": "2"})) as client:
        response = await request_with_retry(
            client,
            "GET",
            "https://x.test/a",
            kind="k",
            sleep=record_sleep,
            on_throttle=lambda: throttled.append(True),
        )
    assert response.status_code == 200
    assert log.count() == 3 and sleeps == [2.0, 2.0] and len(throttled) == 2


async def test_gives_up_with_transient_error() -> None:
    async with make_client(RequestLog(), transport=scripted(500, 500, 500, 500)) as client:
        with pytest.raises(TransientError, match="HTTP 500"):
            await request_with_retry(client, "GET", "https://x.test/a", kind="k", sleep=no_sleep)


async def test_client_errors_are_returned_not_retried() -> None:
    log = RequestLog()
    async with make_client(log, transport=scripted(412)) as client:
        response = await request_with_retry(client, "PUT", "https://x.test/a", kind="k", sleep=no_sleep)
    assert response.status_code == 412 and log.count() == 1


async def test_transport_errors_are_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200)

    async with make_client(RequestLog(), transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(client, "GET", "https://x.test/a", kind="k", sleep=no_sleep)
    assert response.status_code == 200 and calls["n"] == 2
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_http.py -v`

Expected: FAIL，收集阶段报错 `ModuleNotFoundError: No module named 'calsync.http'`

- [ ] **Step 3: 写实现**

`src/calsync/http.py`：

````python
"""Shared HTTP plumbing: client factory, request accounting, retry with backoff.

Every request carries a ``calsync_kind`` extension (e.g. ``google-events``,
``sync-collection``). The request log records it so tests can assert the
per-cycle request budget exactly.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from calsync import __version__
from calsync.errors import TransientError

USER_AGENT = f"calsync/{__version__} (gzip)"
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
THROTTLE_STATUS = frozenset({429, 503})
MAX_RETRY_AFTER = 60.0

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class LoggedRequest:
    method: str
    host: str
    path: str
    kind: str


@dataclass(slots=True)
class RequestLog:
    entries: list[LoggedRequest] = field(default_factory=list)

    def record(self, request: httpx.Request) -> None:
        kind = str(request.extensions.get("calsync_kind", ""))
        self.entries.append(LoggedRequest(request.method, request.url.host, request.url.path, kind))

    def count(self, kind: str | None = None) -> int:
        return sum(1 for e in self.entries if kind is None or e.kind == kind)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.entries]

    def mark(self) -> int:
        return len(self.entries)

    def since(self, mark: int) -> list[LoggedRequest]:
        return self.entries[mark:]


def make_client(
    log: RequestLog,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    auth: httpx.Auth | None = None,
    timeout: float = 30.0,
) -> httpx.AsyncClient:
    async def on_request(request: httpx.Request) -> None:
        log.record(request)

    options: dict[str, Any] = {
        "headers": {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
        "timeout": httpx.Timeout(timeout, connect=10.0),
        "event_hooks": {"request": [on_request]},
        "auth": auth,
        "follow_redirects": False,
        "trust_env": True,  # honours HTTPS_PROXY / HTTP_PROXY / NO_PROXY
    }
    if transport is not None:
        options["transport"] = transport
    else:
        options["http2"] = True
    return httpx.AsyncClient(**options)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return min(float(raw), MAX_RETRY_AFTER)
    except ValueError:
        return None


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    kind: str,
    max_retries: int = 3,
    sleep: Sleep = asyncio.sleep,
    on_throttle: Callable[[], None] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request; retry transport errors and 429/5xx with exponential backoff."""
    delay = 1.0
    extensions = {**kwargs.pop("extensions", {}), "calsync_kind": kind}
    for attempt in range(max_retries + 1):
        last = attempt == max_retries
        try:
            response = await client.request(method, url, extensions=extensions, **kwargs)
        except httpx.TransportError as exc:
            if last:
                raise TransientError(f"{method} {url}: {exc!r}") from exc
            await sleep(delay)
            delay *= 2
            continue
        if response.status_code not in RETRY_STATUS:
            return response
        if response.status_code in THROTTLE_STATUS and on_throttle is not None:
            on_throttle()
        if last:
            raise TransientError(f"{method} {url}: HTTP {response.status_code}")
        await sleep(_retry_after(response) or delay)
        delay *= 2
    raise AssertionError("unreachable")
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `60 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/test_http.py src/calsync/http.py
git commit -m "feat(http): shared client factory, request log and retry with backoff" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Google 源：增量同步客户端

基于 httpx 的 Google Calendar REST 客户端：calendarList 与 events.list 都走 syncToken 增量，410 时全量重建；access token 刷新。

**Files:**
- Create: `tests/fakes/google.py`
- Create: `tests/test_google_source.py`
- Create: `src/calsync/sources/google_auth.py`
- Create: `src/calsync/sources/google.py`

**Interfaces:**
- Consumes:
  - `calsync.http`：`request_with_retry`、`make_client`、`RequestLog`
  - `calsync.sources.base`：`CalendarInfo`、`CalendarMeta`、`CalendarPull`、`SourcePull`、`RawEvent`
  - `calsync.sources.google_map`：`build_series`、`alarms_from_reminders`
- Produces:
  - `calsync.sources.google_auth`：`TokenProvider = Callable[[], Awaitable[str]]`、`StaticToken(token)`、`RefreshTokenProvider(client, *, client_id, client_secret, refresh_token, token_url=TOKEN_URL, clock=time.monotonic)`、`load_client_secret(path) -> (client_id, client_secret)`、`parse_refresh_token(secret) -> str`
  - `calsync.sources.google`：`BASE_URL`、`EVENTS_LIST_PARAMS`、`CALENDAR_LIST_PARAMS`、`GoogleSource(client, token, *, base_url=BASE_URL)`，实现 `Source` 协议：`list_calendars()`、`pull(calendar_refs, state) -> SourcePull`、`to_series(raw_events, meta)`
  - `pull` 的 `state` 是可 JSON 序列化的字典：`calendars_token`、`meta`（日历 ID → summary / timezone / default_alarms 秒数）、`events_tokens`（日历 ID → syncToken）
  - 测试替身 `tests/fakes/google.py`：`FakeGoogle(page_size=2500)`，方法 `add_calendar`、`set_default_reminders`、`put_event`、`cancel_event`、`expire_tokens`、`transport()`；常量 `BASE_URL = "https://google.test/calendar/v3"`

**注意：**
- `fields` 必须包含 `nextSyncToken,nextPageToken`，否则响应里没有游标，增量同步永远开始不了；测试 `test_missing_sync_token_in_fields_is_detected` 守护这一点。
- 不使用 `timeMin`/`timeMax`：它们不能和 `syncToken` 同时用，时间窗口在本地规划时应用。
- 规则里的日历可以写名称或 ID；名称通过 calendarList 的 summary 解析，重名时报配置错误。

- [ ] **Step 1: 写失败的测试**

`tests/fakes/google.py`：

````python
"""In-memory fake of the Google Calendar API, mounted on httpx.MockTransport.

Implements the parts calsync relies on: calendarList and events.list with
syncToken semantics, pagination, 410 on expired tokens, and dropping
nextSyncToken when the ``fields`` mask omits it (as Google does).
"""

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

BASE_URL = "https://google.test/calendar/v3"


@dataclass
class _Calendar:
    summary: str
    tz: str
    default_reminders: list[dict[str, Any]]
    changed_at: int
    events: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)


class FakeGoogle:
    def __init__(self, *, page_size: int = 2500) -> None:
        self.page_size = page_size
        self.generation = 0
        self._seq = 0
        self._calendars: dict[str, _Calendar] = {}

    # ----- test helpers -------------------------------------------------
    def _tick(self) -> int:
        self._seq += 1
        return self._seq

    def add_calendar(
        self,
        cid: str,
        summary: str,
        *,
        tz: str = "Asia/Shanghai",
        default_reminders: list[dict[str, Any]] | None = None,
    ) -> None:
        self._calendars[cid] = _Calendar(summary, tz, default_reminders or [], self._tick())

    def set_default_reminders(self, cid: str, reminders: list[dict[str, Any]]) -> None:
        cal = self._calendars[cid]
        cal.default_reminders = reminders
        cal.changed_at = self._tick()

    def put_event(self, cid: str, event: dict[str, Any]) -> None:
        self._calendars[cid].events[event["id"]] = (self._tick(), dict(event))

    def cancel_event(self, cid: str, event_id: str) -> None:
        _, old = self._calendars[cid].events[event_id]
        self.put_event(cid, {**old, "status": "cancelled"})

    def expire_tokens(self) -> None:
        self.generation += 1

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # ----- protocol -----------------------------------------------------
    def _token(self) -> str:
        return f"g{self.generation}-{self._seq}"

    def _since(self, token: str | None) -> int | None:
        """Sequence number encoded in a token; None for a full listing."""
        if token is None:
            return None
        gen, _, seq = token[1:].partition("-")
        if int(gen) != self.generation:
            raise LookupError
        return int(seq)

    def _page(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        params = request.url.params
        offset = int(params.get("pageToken", "0"))
        chunk = items[offset : offset + self.page_size]
        body: dict[str, Any] = {"items": chunk}
        if offset + self.page_size < len(items):
            body["nextPageToken"] = str(offset + self.page_size)
        elif "nextSyncToken" in params.get("fields", "nextSyncToken"):
            body["nextSyncToken"] = self._token()
        return httpx.Response(200, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"error": "unauthenticated"})
        path = request.url.path.removeprefix("/calendar/v3")
        try:
            since = self._since(request.url.params.get("syncToken"))
        except LookupError:
            return httpx.Response(
                410, json={"error": {"code": 410, "message": "Sync token is no longer valid"}}
            )
        if path == "/users/me/calendarList":
            items = [
                {"id": cid, "summary": c.summary, "timeZone": c.tz, "defaultReminders": c.default_reminders}
                for cid, c in self._calendars.items()
                if since is None or c.changed_at > since
            ]
            return self._page(request, items)
        if path.startswith("/calendars/") and path.endswith("/events"):
            cid = unquote(path[len("/calendars/") : -len("/events")])
            if cid not in self._calendars:
                return httpx.Response(404, json={"error": "notFound"})
            events = self._calendars[cid].events.values()
            items = [
                json.loads(json.dumps(e))
                for seq, e in sorted(events, key=lambda p: p[0])
                if since is None or seq > since
            ]
            return self._page(request, items)
        return httpx.Response(404, json={"error": f"unknown path {path}"})
````

`tests/test_google_source.py`：

````python
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from calsync.errors import AuthError, CalsyncError, ConfigError
from calsync.http import RequestLog, make_client
from calsync.model import Alarm
from calsync.sources import google as google_mod
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import (
    RefreshTokenProvider,
    StaticToken,
    load_client_secret,
    parse_refresh_token,
)
from tests.factories import google_event
from tests.fakes.google import BASE_URL, FakeGoogle

pytestmark = pytest.mark.anyio
CAL = "family@group.calendar.google.com"


def make_source(fake: FakeGoogle, log: RequestLog) -> GoogleSource:
    client = make_client(log, transport=fake.transport())
    return GoogleSource(client, StaticToken("t"), base_url=BASE_URL)


def seeded() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(CAL, "Family", default_reminders=[{"method": "popup", "minutes": 10}])
    fake.put_event(CAL, google_event("e1", "One", "2026-10-25T09:00:00+08:00", "2026-10-25T09:05:00+08:00"))
    fake.put_event(CAL, google_event("e2", "Two", "2026-10-26T09:00:00+08:00", "2026-10-26T09:05:00+08:00"))
    return fake


async def test_full_then_idle_then_delta() -> None:
    fake, log = seeded(), RequestLog()
    source = make_source(fake, log)

    first = await source.pull(["Family"], {})
    pull = first.calendars["Family"]
    assert pull.calendar_id == CAL and pull.full
    assert sorted(e["id"] for e in pull.upserts) == ["e1", "e2"]
    assert pull.meta.default_alarms == (Alarm(timedelta(minutes=-10)),)
    assert log.kinds() == ["google-calendarlist", "google-events"]

    mark = log.mark()
    idle = await source.pull(["Family"], first.state)
    assert idle.calendars["Family"].upserts == [] and not idle.calendars["Family"].full
    assert idle.calendars["Family"].meta.default_alarms == (Alarm(timedelta(minutes=-10)),)
    assert [e.kind for e in log.since(mark)] == ["google-calendarlist", "google-events"]

    fake.put_event(
        CAL, google_event("e1", "One (moved)", "2026-10-25T10:00:00+08:00", "2026-10-25T10:05:00+08:00")
    )
    fake.cancel_event(CAL, "e2")
    delta = (await source.pull([CAL], idle.state)).calendars[CAL]
    assert [e["summary"] for e in delta.upserts] == ["One (moved)"]
    assert delta.deletes == ["e2"]


async def test_expired_token_triggers_full_resync() -> None:
    fake = seeded()
    source = make_source(fake, RequestLog())
    state = (await source.pull([CAL], {})).state
    fake.expire_tokens()
    again = (await source.pull([CAL], state)).calendars[CAL]
    assert again.full and len(again.upserts) == 2


async def test_pagination() -> None:
    fake = seeded()
    fake.page_size = 1
    log = RequestLog()
    pull = (await make_source(fake, log).pull([CAL], {})).calendars[CAL]
    assert len(pull.upserts) == 2
    assert log.count("google-events") == 2


async def test_missing_sync_token_in_fields_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_mod, "EVENTS_LIST_PARAMS", {**google_mod.EVENTS_LIST_PARAMS, "fields": "items(id)"}
    )
    with pytest.raises(CalsyncError, match="nextSyncToken"):
        await make_source(seeded(), RequestLog()).pull([CAL], {})


async def test_unknown_calendar_name() -> None:
    with pytest.raises(ConfigError, match="not found"):
        await make_source(seeded(), RequestLog()).pull(["Nope"], {})


async def test_list_calendars_and_to_series() -> None:
    source = make_source(seeded(), RequestLog())
    assert [(c.id, c.name) for c in await source.list_calendars()] == [(CAL, "Family")]
    pull = (await source.pull([CAL], {})).calendars[CAL]
    assert sorted(s.title for s in source.to_series(pull.upserts, pull.meta)) == ["One", "Two"]


async def test_unauthorized_maps_to_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid"})

    client = make_client(RequestLog(), transport=httpx.MockTransport(handler))
    with pytest.raises(AuthError):
        await GoogleSource(client, StaticToken("t"), base_url=BASE_URL).list_calendars()


async def test_refresh_token_provider_caches_until_near_expiry() -> None:
    now = {"t": 1000.0}
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        calls.append(form)
        return httpx.Response(200, json={"access_token": f"a{len(calls)}", "expires_in": 3600})

    log = RequestLog()
    provider = RefreshTokenProvider(
        make_client(log, transport=httpx.MockTransport(handler)),
        client_id="cid",
        client_secret="cs",
        refresh_token="rt",
        clock=lambda: now["t"],
    )
    assert await provider() == "a1"
    now["t"] += 3000
    assert await provider() == "a1"
    now["t"] += 400  # within 5 minutes of expiry
    assert await provider() == "a2"
    assert calls[0]["grant_type"] == "refresh_token" and calls[0]["refresh_token"] == "rt"
    assert log.count("google-token") == 2


async def test_refresh_token_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    provider = RefreshTokenProvider(
        make_client(RequestLog(), transport=httpx.MockTransport(handler)),
        client_id="c",
        client_secret="s",
        refresh_token="r",
    )
    with pytest.raises(AuthError, match="invalid_grant"):
        await provider()


def test_client_secret_and_credential_parsing(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"installed": {"client_id": "id1", "client_secret": "sec1"}}))
    assert load_client_secret(path) == ("id1", "sec1")
    with pytest.raises(ConfigError):
        load_client_secret(tmp_path / "missing.json")
    assert parse_refresh_token('{"refresh_token": "rt"}') == "rt"
    with pytest.raises(AuthError):
        parse_refresh_token("not json")
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_google_source.py -v`

Expected: FAIL，收集阶段报错 `ImportError: cannot import name 'google' from 'calsync.sources'`

- [ ] **Step 3: 写实现**

`src/calsync/sources/google_auth.py`：

````python
"""Google OAuth access tokens.

Access tokens live only in memory and are refreshed 5 minutes before expiry.
The interactive browser consent flow (loopback + PKCE) is added by the
runtime plan; this module only exchanges a stored refresh token.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from calsync.errors import AuthError, ConfigError, TransientError
from calsync.http import request_with_retry

TOKEN_URL = "https://oauth2.googleapis.com/token"
REFRESH_MARGIN = 300.0

TokenProvider = Callable[[], Awaitable[str]]


class StaticToken:
    def __init__(self, token: str) -> None:
        self._token = token

    async def __call__(self) -> str:
        return self._token


class RefreshTokenProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_url: str = TOKEN_URL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._form = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        self._url = token_url
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._token is not None and self._clock() < self._expires_at - REFRESH_MARGIN

    async def __call__(self) -> str:
        if self._fresh():
            assert self._token is not None
            return self._token
        async with self._lock:
            if self._fresh():
                assert self._token is not None
                return self._token
            response = await request_with_retry(
                self._client, "POST", self._url, kind="google-token", data=self._form
            )
            if response.status_code in (400, 401):
                raise AuthError(f"Google refused the refresh token: {response.text[:200]}")
            if response.status_code >= 400:
                raise TransientError(f"Google token endpoint: HTTP {response.status_code}")
            data = response.json()
            token: str = data["access_token"]
            self._token = token
            self._expires_at = self._clock() + float(data.get("expires_in", 3600))
            return token


def load_client_secret(path: Path) -> tuple[str, str]:
    """Read client_id / client_secret from the JSON downloaded from Google Cloud Console."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError([f"google client secret file not found: {path}"]) from None
    except json.JSONDecodeError as exc:
        raise ConfigError([f"{path}: invalid JSON ({exc})"]) from None
    section = data.get("installed") or data.get("web") or data
    try:
        return section["client_id"], section["client_secret"]
    except KeyError:
        raise ConfigError([f"{path}: missing client_id/client_secret"]) from None


def parse_refresh_token(secret: str) -> str:
    """Stored Google credentials are JSON: {"refresh_token": "..."}."""
    try:
        token = json.loads(secret)["refresh_token"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise AuthError("stored Google credential is not valid JSON with a refresh_token") from None
    if not isinstance(token, str) or not token:
        raise AuthError("stored Google credential has an empty refresh_token")
    return token
````

`src/calsync/sources/google.py`：

````python
"""Google Calendar source: thin REST client with incremental sync.

Per cycle and Google account this costs exactly one ``calendarList`` request
plus one ``events.list`` request per source calendar when nothing changed.
"""

import asyncio
import copy
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import httpx

from calsync.errors import AuthError, CalsyncError, ConfigError, CursorExpired, NotFoundError
from calsync.http import request_with_retry
from calsync.model import Alarm, Series
from calsync.sources.base import (
    CalendarInfo,
    CalendarMeta,
    CalendarPull,
    RawEvent,
    SourcePull,
)
from calsync.sources.google_auth import TokenProvider
from calsync.sources.google_map import alarms_from_reminders, build_series

BASE_URL = "https://www.googleapis.com/calendar/v3"

EVENT_FIELDS = (
    "id,status,updated,summary,description,location,start,end,"
    "originalStartTime,recurringEventId,recurrence,reminders,transparency"
)
# Identical for the initial full sync and every incremental call, as Google
# requires. ``fields`` MUST keep nextSyncToken/nextPageToken or incremental
# sync silently never starts.
EVENTS_LIST_PARAMS: Mapping[str, str] = {
    "singleEvents": "false",
    "showDeleted": "true",
    "maxResults": "2500",
    "fields": f"nextPageToken,nextSyncToken,items({EVENT_FIELDS})",
}
CALENDAR_LIST_PARAMS: Mapping[str, str] = {
    "showDeleted": "true",
    "showHidden": "true",
    "maxResults": "250",
    "fields": "nextPageToken,nextSyncToken,items(id,summary,timeZone,defaultReminders,deleted)",
}


def _is_removal(event: Mapping[str, Any]) -> bool:
    return event.get("status") == "cancelled" and not event.get("recurringEventId")


def _meta_from_state(entry: Mapping[str, Any] | None) -> CalendarMeta:
    if not entry:
        return CalendarMeta()
    return CalendarMeta(
        timezone=entry.get("timezone") or "UTC",
        default_alarms=tuple(Alarm(timedelta(seconds=s)) for s in entry.get("default_alarms", [])),
    )


class GoogleSource:
    def __init__(self, client: httpx.AsyncClient, token: TokenProvider, *, base_url: str = BASE_URL) -> None:
        self._client = client
        self._token = token
        self._base = base_url.rstrip("/")

    async def _get(self, path: str, params: Mapping[str, str], kind: str) -> dict[str, Any]:
        token = await self._token()
        response = await request_with_retry(
            self._client,
            "GET",
            f"{self._base}{path}",
            kind=kind,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        status = response.status_code
        if status == 410:
            raise CursorExpired(path)
        if status in (401, 403):
            raise AuthError(f"Google API {status}: {response.text[:200]}")
        if status == 404:
            raise NotFoundError(path)
        if status >= 400:
            raise CalsyncError(f"Google API {status} for {path}: {response.text[:200]}")
        return response.json()

    async def _list_all(
        self, path: str, base: Mapping[str, str], sync_token: str | None, kind: str
    ) -> tuple[list[dict[str, Any]], str]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            params = dict(base)
            if sync_token:
                params["syncToken"] = sync_token
            if page:
                params["pageToken"] = page
            data = await self._get(path, params, kind)
            items.extend(data.get("items", []))
            page = data.get("nextPageToken")
            if page:
                continue
            next_token = data.get("nextSyncToken")
            if not next_token:
                raise CalsyncError(f"Google response for {path} has no nextSyncToken")
            return items, next_token

    async def list_calendars(self) -> list[CalendarInfo]:
        items, _ = await self._list_all(
            "/users/me/calendarList", CALENDAR_LIST_PARAMS, None, "google-calendarlist"
        )
        return [
            CalendarInfo(item["id"], item.get("summary", item["id"]))
            for item in items
            if not item.get("deleted")
        ]

    async def _refresh_calendar_meta(self, state: dict[str, Any]) -> None:
        meta: dict[str, Any] = state.setdefault("meta", {})
        token = state.get("calendars_token")
        path = "/users/me/calendarList"
        try:
            items, next_token = await self._list_all(path, CALENDAR_LIST_PARAMS, token, "google-calendarlist")
        except CursorExpired:
            meta.clear()
            items, next_token = await self._list_all(path, CALENDAR_LIST_PARAMS, None, "google-calendarlist")
        for item in items:
            if item.get("deleted"):
                meta.pop(item["id"], None)
                continue
            alarms = alarms_from_reminders(item.get("defaultReminders") or [])
            meta[item["id"]] = {
                "summary": item.get("summary", ""),
                "timezone": item.get("timeZone") or "UTC",
                "default_alarms": [int(a.offset.total_seconds()) for a in alarms],
            }
        state["calendars_token"] = next_token

    @staticmethod
    def _resolve(ref: str, meta: Mapping[str, Any]) -> str:
        if ref in meta:
            return ref
        matches = [cid for cid, entry in meta.items() if entry.get("summary") == ref]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ConfigError([f"Google calendar name {ref!r} is ambiguous; use its ID"])
        if "@" in ref:
            return ref  # an ID the user can access but has not added to their list
        raise ConfigError([f"Google calendar {ref!r} not found; run `calsync calendars <account>`"])

    async def _pull_events(
        self, calendar_id: str, sync_token: str | None
    ) -> tuple[list[dict[str, Any]], str, bool]:
        path = f"/calendars/{quote(calendar_id, safe='')}/events"
        try:
            items, next_token = await self._list_all(path, EVENTS_LIST_PARAMS, sync_token, "google-events")
            return items, next_token, sync_token is None
        except CursorExpired:
            items, next_token = await self._list_all(path, EVENTS_LIST_PARAMS, None, "google-events")
            return items, next_token, True

    async def pull(self, calendar_refs: Sequence[str], state: Mapping[str, Any]) -> SourcePull:
        new_state: dict[str, Any] = copy.deepcopy(dict(state))
        await self._refresh_calendar_meta(new_state)
        meta = new_state["meta"]
        tokens: dict[str, str] = new_state.setdefault("events_tokens", {})
        resolved = {ref: self._resolve(ref, meta) for ref in calendar_refs}
        unique_ids = sorted(set(resolved.values()))
        results = await asyncio.gather(*(self._pull_events(cid, tokens.get(cid)) for cid in unique_ids))
        pulls: dict[str, CalendarPull] = {}
        for cid, (items, next_token, full) in zip(unique_ids, results, strict=True):
            tokens[cid] = next_token
            pulls[cid] = CalendarPull(
                calendar_id=cid,
                full=full,
                upserts=[item for item in items if not _is_removal(item)],
                deletes=[item["id"] for item in items if _is_removal(item)],
                meta=_meta_from_state(meta.get(cid)),
            )
        return SourcePull(calendars={ref: pulls[cid] for ref, cid in resolved.items()}, state=new_state)

    def to_series(self, raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Series]:
        return build_series(raw_events, meta)
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `70 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/fakes/google.py tests/test_google_source.py src/calsync/sources/google_auth.py src/calsync/sources/google.py
git commit -m "feat(google): incremental Google Calendar source and token refresh" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: WebDAV / CalDAV 协议层

最小协议层：PROPFIND、REPORT、条件 PUT/DELETE、MKCALENDAR，手动跟随重定向，href 一律转成绝对地址，错误映射到统一类型。

**Files:**
- Create: `tests/test_dav.py`
- Create: `src/calsync/targets/__init__.py`
- Create: `src/calsync/targets/dav.py`

**Interfaces:**
- Consumes:
  - `calsync.http`：`request_with_retry`、`Sleep`
  - `calsync.errors`：`AuthError`、`CalsyncError`、`ConflictError`、`CursorExpired`、`NotFoundError`
- Produces:
  - 命名空间常量 `DAV`、`CALDAV`、`CS`，`q(ns, name) -> "{ns}name"`，`MULTIGET_BATCH = 100`、`MAX_REDIRECTS = 5`
  - `DavResponse(href, status, props)`：`href` 为绝对 URL；方法 `text(name)`、`href_prop(name)`（同样返回绝对 URL）
  - `parse_multistatus(body, base_url) -> (list[DavResponse], sync_token | None)`
  - 请求体构造：`propfind_body`、`sync_collection_body(token)`、`calendar_query_etags_body()`、`multiget_body(hrefs)`、`mkcalendar_body(name)`
  - `DavClient(client, base_url, *, sleep, on_throttle)`：`url(href)`、`propfind(href, props, *, depth)`、`report(href, body, *, kind, depth=1)`、`put(href, data, *, if_match=None, create_only=False) -> etag | None`、`delete(href, *, if_match=None)`、`mkcalendar(href, name)`
  - 错误映射：401/403 → `AuthError`；403/409 且正文含 `valid-sync-token` → `CursorExpired`；404 → `NotFoundError`；412 → `ConflictError`

**注意：**
- 重定向必须手动跟随：httpx 自带的跟随在跨主机时会丢掉认证头。iCloud 的日历在另一个主机上（`pNN-caldav.icloud.com`）。禁止跟随改变协议（https → http）的重定向。
- 不要调用 `ET.register_namespace`：它会修改全局状态，Radicale 也会改它，结果互相干扰。测试因此解析 XML 后再断言，不比较前缀字符串。

- [ ] **Step 1: 写失败的测试**

`tests/test_dav.py`：

````python
import xml.etree.ElementTree as ET

import httpx
import pytest

from calsync.errors import AuthError, CalsyncError, ConflictError, CursorExpired, NotFoundError
from calsync.http import RequestLog, make_client
from calsync.targets.dav import (
    CALDAV,
    CS,
    DAV,
    DavClient,
    multiget_body,
    parse_multistatus,
    q,
    sync_collection_body,
)

pytestmark = pytest.mark.anyio

SYNC_RESPONSE = b"""<?xml version='1.0' encoding='utf-8'?>
<multistatus xmlns="DAV:"><sync-token>http://example.com/sync/2</sync-token>
<response><href>/u/cal/calsync-aaaaaaaa-bbbbbbbbbbbbbbbbbbbbbbbb.ics</href>
<propstat><prop><getetag>"e1"</getetag></prop><status>HTTP/1.1 200 OK</status></propstat></response>
<response><href>/u/cal/gone%20file.ics</href><status>HTTP/1.1 404 Not Found</status></response>
</multistatus>"""

PROPFIND_RESPONSE = b"""<?xml version='1.0' encoding='utf-8'?>
<multistatus xmlns="DAV:" xmlns:CS="http://calendarserver.org/ns/">
<response><href>/u/cal/</href>
<propstat><prop><displayname>Family</displayname><CS:getctag>"c1"</CS:getctag></prop><status>HTTP/1.1 200 OK</status></propstat>
<propstat><prop><sync-token/></prop><status>HTTP/1.1 404 Not Found</status></propstat>
</response></multistatus>"""


def test_parse_sync_collection() -> None:
    responses, token = parse_multistatus(SYNC_RESPONSE, "https://p01-dav.test/u/cal/")
    assert token == "http://example.com/sync/2"
    first, gone = responses
    assert first.status == 200 and first.text(q(DAV, "getetag")) == '"e1"'
    assert first.href == "https://p01-dav.test/u/cal/calsync-aaaaaaaa-bbbbbbbbbbbbbbbbbbbbbbbb.ics"
    assert (
        gone.href == "https://p01-dav.test/u/cal/gone%20file.ics" and gone.status == 404 and gone.props == {}
    )


def test_parse_skips_failed_propstat() -> None:
    (item,), token = parse_multistatus(PROPFIND_RESPONSE, "https://dav.test/")
    assert token is None
    assert item.text(q(DAV, "displayname")) == "Family"
    assert item.text(q(CS, "getctag")) == '"c1"'
    assert q(DAV, "sync-token") not in item.props


def test_request_bodies() -> None:
    initial = ET.fromstring(sync_collection_body(None))
    assert initial.tag == q(DAV, "sync-collection")
    assert initial.find(q(DAV, "sync-token")).text is None
    assert ET.fromstring(sync_collection_body("tok")).find(q(DAV, "sync-token")).text == "tok"
    body = ET.fromstring(multiget_body(["/a.ics", "/b.ics"]))
    assert [h.text for h in body.findall(q(DAV, "href"))] == ["/a.ics", "/b.ics"]
    assert body.find(f"{q(DAV, 'prop')}/{q(CALDAV, 'calendar-data')}") is not None


def client_with(status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> DavClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers or {})

    return DavClient(make_client(RequestLog(), transport=httpx.MockTransport(handler)), "https://dav.test/")


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, b"", AuthError),
        (403, b"", AuthError),
        (403, b"<error xmlns='DAV:'><valid-sync-token/></error>", CursorExpired),
        (409, b"<error xmlns='DAV:'><valid-sync-token/></error>", CursorExpired),
        (404, b"", NotFoundError),
        (412, b"", ConflictError),
    ],
)
async def test_error_mapping(status: int, body: bytes, error: type[Exception]) -> None:
    with pytest.raises(error):
        await client_with(status, body).report("/cal/", sync_collection_body("t"), kind="sync-collection")


async def test_put_headers_and_etag() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, headers={"ETag": '"new"'})

    dav = DavClient(
        make_client(RequestLog(), transport=httpx.MockTransport(handler)), "https://dav.test/root/"
    )
    assert await dav.put("/root/cal/x.ics", b"ICS", create_only=True) == '"new"'
    assert await dav.put("/root/cal/x.ics", b"ICS", if_match='"old"') == '"new"'
    assert seen[0].headers["if-none-match"] == "*" and "if-match" not in seen[0].headers
    assert seen[1].headers["if-match"] == '"old"'
    assert str(seen[0].url) == "https://dav.test/root/cal/x.ics"


async def test_redirect_loop_and_scheme_change_are_refused() -> None:
    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": str(request.url)})

    def downgrade(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "http://dav.test/"})

    for handler, message in ((loop, "too many redirects"), (downgrade, "refusing redirect")):
        dav = DavClient(
            make_client(RequestLog(), transport=httpx.MockTransport(handler)), "https://dav.test/"
        )
        with pytest.raises(CalsyncError, match=message):
            await dav.propfind("", [q(DAV, "displayname")], depth=0)
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_dav.py -v`

Expected: FAIL，收集阶段报错 `ModuleNotFoundError: No module named 'calsync.targets'`

- [ ] **Step 3: 写实现**

`src/calsync/targets/__init__.py`：空文件（`touch src/calsync/targets/__init__.py`）。

`src/calsync/targets/dav.py`：

````python
"""Minimal WebDAV / CalDAV protocol layer on top of httpx.

Only what calsync needs: PROPFIND, REPORT (calendar-query, calendar-multiget,
sync-collection), MKCALENDAR, conditional PUT and DELETE. XML is parsed with
the standard library (Python's bundled expat rejects entity-expansion bombs).
"""

import asyncio
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from calsync.errors import AuthError, CalsyncError, ConflictError, CursorExpired, NotFoundError
from calsync.http import Sleep, request_with_retry

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CS = "http://calendarserver.org/ns/"

MULTIGET_BATCH = 100
MAX_REDIRECTS = 5
REDIRECT_STATUS = frozenset({301, 302, 307, 308})


def q(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


@dataclass(frozen=True, slots=True)
class DavResponse:
    href: str  # absolute URL, resolved against the URL of the response that listed it
    status: int
    props: dict[str, ET.Element]

    def text(self, name: str) -> str | None:
        element = self.props.get(name)
        if element is None or element.text is None:
            return None
        return element.text.strip() or None

    def href_prop(self, name: str) -> str | None:
        """An href-valued property, resolved to an absolute URL like ``href``."""
        element = self.props.get(name)
        if element is None:
            return None
        child = element.find(q(DAV, "href"))
        return None if child is None or not child.text else urljoin(self.href, child.text.strip())


def _status_code(text: str | None) -> int | None:
    if not text:
        return None
    parts = text.split()
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None


def parse_multistatus(body: bytes, base_url: str) -> tuple[list[DavResponse], str | None]:
    """Parse a 207 body. Hrefs become absolute URLs: iCloud serves calendars from a
    different host (pNN-caldav.icloud.com) than the one discovery starts on."""
    root = ET.fromstring(body)
    responses: list[DavResponse] = []
    for node in root.findall(q(DAV, "response")):
        href = urljoin(base_url, (node.findtext(q(DAV, "href")) or "").strip())
        props: dict[str, ET.Element] = {}
        for propstat in node.findall(q(DAV, "propstat")):
            if _status_code(propstat.findtext(q(DAV, "status"))) != 200:
                continue
            prop = propstat.find(q(DAV, "prop"))
            if prop is not None:
                for child in prop:
                    props[child.tag] = child
        status = _status_code(node.findtext(q(DAV, "status"))) or 200
        responses.append(DavResponse(href, status, props))
    token = root.findtext(q(DAV, "sync-token"))
    return responses, token.strip() if token else None


def _xml(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def propfind_body(props: Sequence[str]) -> bytes:
    root = ET.Element(q(DAV, "propfind"))
    prop = ET.SubElement(root, q(DAV, "prop"))
    for name in props:
        ET.SubElement(prop, name)
    return _xml(root)


def sync_collection_body(token: str | None) -> bytes:
    root = ET.Element(q(DAV, "sync-collection"))
    ET.SubElement(root, q(DAV, "sync-token")).text = token or None
    ET.SubElement(root, q(DAV, "sync-level")).text = "1"
    prop = ET.SubElement(root, q(DAV, "prop"))
    ET.SubElement(prop, q(DAV, "getetag"))
    return _xml(root)


def calendar_query_etags_body() -> bytes:
    root = ET.Element(q(CALDAV, "calendar-query"))
    prop = ET.SubElement(root, q(DAV, "prop"))
    ET.SubElement(prop, q(DAV, "getetag"))
    filt = ET.SubElement(root, q(CALDAV, "filter"))
    vcal = ET.SubElement(filt, q(CALDAV, "comp-filter"), {"name": "VCALENDAR"})
    ET.SubElement(vcal, q(CALDAV, "comp-filter"), {"name": "VEVENT"})
    return _xml(root)


def multiget_body(hrefs: Sequence[str]) -> bytes:
    root = ET.Element(q(CALDAV, "calendar-multiget"))
    prop = ET.SubElement(root, q(DAV, "prop"))
    ET.SubElement(prop, q(DAV, "getetag"))
    ET.SubElement(prop, q(CALDAV, "calendar-data"))
    for href in hrefs:
        ET.SubElement(root, q(DAV, "href")).text = href
    return _xml(root)


def mkcalendar_body(display_name: str) -> bytes:
    root = ET.Element(q(CALDAV, "mkcalendar"))
    prop = ET.SubElement(ET.SubElement(root, q(DAV, "set")), q(DAV, "prop"))
    ET.SubElement(prop, q(DAV, "displayname")).text = display_name
    components = ET.SubElement(prop, q(CALDAV, "supported-calendar-component-set"))
    ET.SubElement(components, q(CALDAV, "comp"), {"name": "VEVENT"})
    return _xml(root)


_XML_HEADERS = {"Content-Type": "application/xml; charset=utf-8"}


class DavClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        sleep: Sleep = asyncio.sleep,
        on_throttle: Callable[[], None] | None = None,
    ) -> None:
        self._client = client
        self._base = base_url
        self._sleep = sleep
        self._on_throttle = on_throttle

    def url(self, href: str) -> str:
        return urljoin(self._base, href)

    async def _send(
        self,
        method: str,
        href: str,
        *,
        kind: str,
        ok: tuple[int, ...],
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = self.url(href)
        # Redirects are followed by hand: httpx drops credentials on a cross-host
        # redirect and may rewrite the method, both wrong for WebDAV.
        for _ in range(MAX_REDIRECTS + 1):
            response = await request_with_retry(
                self._client,
                method,
                url,
                kind=kind,
                content=content,
                headers=headers,
                sleep=self._sleep,
                on_throttle=self._on_throttle,
            )
            location = response.headers.get("location")
            if response.status_code not in REDIRECT_STATUS or not location:
                break
            target = urljoin(url, location)
            if urlsplit(target).scheme != urlsplit(url).scheme:
                raise CalsyncError(f"{method} {href}: refusing redirect to {target}")
            url = target
        else:
            raise CalsyncError(f"{method} {href}: too many redirects")
        status = response.status_code
        if status in ok:
            return response
        sync_token_invalid = b"valid-sync-token" in response.content
        if status in (403, 409) and sync_token_invalid:
            raise CursorExpired(href)
        if status in (401, 403):
            raise AuthError(f"{method} {href}: HTTP {status}")
        if status == 404:
            raise NotFoundError(href)
        if status == 412:
            raise ConflictError(href)
        raise CalsyncError(f"{method} {href}: HTTP {status}")

    async def propfind(self, href: str, props: Sequence[str], *, depth: int) -> list[DavResponse]:
        response = await self._send(
            "PROPFIND",
            href,
            kind="propfind",
            ok=(207,),
            content=propfind_body(props),
            headers={**_XML_HEADERS, "Depth": str(depth)},
        )
        return parse_multistatus(response.content, str(response.url))[0]

    async def report(
        self, href: str, body: bytes, *, kind: str, depth: int = 1
    ) -> tuple[list[DavResponse], str | None]:
        response = await self._send(
            "REPORT",
            href,
            kind=kind,
            ok=(207,),
            content=body,
            headers={**_XML_HEADERS, "Depth": str(depth)},
        )
        return parse_multistatus(response.content, str(response.url))

    async def put(
        self, href: str, data: bytes, *, if_match: str | None = None, create_only: bool = False
    ) -> str | None:
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if create_only:
            headers["If-None-Match"] = "*"
        elif if_match is not None:
            headers["If-Match"] = if_match
        response = await self._send(
            "PUT", href, kind="put", ok=(200, 201, 204), content=data, headers=headers
        )
        return response.headers.get("etag")

    async def delete(self, href: str, *, if_match: str | None = None) -> None:
        headers = {"If-Match": if_match} if if_match else None
        await self._send("DELETE", href, kind="delete", ok=(200, 204), headers=headers)

    async def mkcalendar(self, href: str, display_name: str) -> None:
        await self._send(
            "MKCALENDAR",
            href,
            kind="mkcalendar",
            ok=(201,),
            content=mkcalendar_body(display_name),
            headers=_XML_HEADERS,
        )
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `81 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/test_dav.py src/calsync/targets/__init__.py src/calsync/targets/dav.py
git commit -m "feat(dav): minimal WebDAV/CalDAV protocol layer" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: CalDAV 目标适配器

发现日历、按需新建、变化检测降级阶梯（sync-collection → getctag → 全量列表）、multiget、带并发限制的条件写入。

**Files:**
- Create: `tests/test_caldav_discovery.py`
- Create: `tests/test_caldav_target.py`
- Create: `src/calsync/targets/base.py`
- Create: `src/calsync/targets/caldav.py`

**Interfaces:**
- Consumes:
  - `calsync.targets.dav`：全部
  - `calsync.naming.parse_resource_name`
  - `calsync.http.Sleep`
- Produces:
  - `calsync.targets.base`：`DetectMode = Literal["sync", "ctag", "full"]`、`TargetCalendar(href, name, mode)`、`TargetDelta(changed: dict[href, etag|None], deleted: tuple[str, ...], full: bool, cursor)`、`FetchedObject(href, etag, data)`、`CachedObject(etag, rule_tag, object_id, content_hash, span)`、`TargetCache(cursor=None, objects={})`、`Target` 协议（`reset`、`list_calendars`、`ensure_calendar(ref, *, create)`、`changes(cal, cursor)`、`fetch(cal, hrefs)`、`href_for(cal, name)`、`create(href, data)`、`update(href, etag, data)`、`delete(href, etag)`）
  - `calsync.targets.caldav`：`Limiter(limit)`（`throttle()` 降为 1、`reset()` 恢复、`async with`）、`CalDavTarget(client, server_url, *, max_concurrency=2, sleep=asyncio.sleep)`，属性 `limiter`

**注意：**
- `changes()` 只返回资源名属于 calsync 的 href；用户自己建的事件对引擎完全不可见。
- `test_caldav_target.py` 标记为 `e2e`，连接 `tests/conftest.py` 启动的本地 Radicale；`test_caldav_discovery.py` 用 MockTransport 模拟 iCloud 的跨主机发现。

- [ ] **Step 1: 写失败的测试**

`tests/test_caldav_discovery.py`：

````python
"""Discovery against a mocked iCloud-like server: redirects and a home set on another host."""

import httpx
import pytest

from calsync.http import RequestLog, make_client
from calsync.targets.caldav import CalDavTarget

pytestmark = pytest.mark.anyio


def _multistatus(*responses: str) -> bytes:
    return (
        "<?xml version='1.0'?><multistatus xmlns='DAV:' xmlns:C='urn:ietf:params:xml:ns:caldav'>"
        + "".join(responses)
        + "</multistatus>"
    ).encode()


async def test_discovery_follows_redirects_and_home_on_other_host() -> None:
    """iCloud: discovery starts on caldav.icloud.com, calendars live on pNN-caldav.icloud.com."""
    seen: list[tuple[str, str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append((request.method, url, "authorization" in request.headers))
        if url == "https://dav.test/":
            return httpx.Response(301, headers={"Location": "https://dav.test/root/"})
        if url == "https://dav.test/root/":
            return httpx.Response(
                207,
                content=_multistatus(
                    "<response><href>/root/</href><propstat><prop><current-user-principal>"
                    "<href>/123/principal/</href></current-user-principal></prop>"
                    "<status>HTTP/1.1 200 OK</status></propstat></response>"
                ),
            )
        if url == "https://dav.test/123/principal/":
            return httpx.Response(
                207,
                content=_multistatus(
                    "<response><href>/123/principal/</href><propstat><prop><C:calendar-home-set>"
                    "<href>https://p07-dav.test/123/calendars/</href></C:calendar-home-set></prop>"
                    "<status>HTTP/1.1 200 OK</status></propstat></response>"
                ),
            )
        if url == "https://p07-dav.test/123/calendars/":
            return httpx.Response(
                207,
                content=_multistatus(
                    "<response><href>/123/calendars/home/</href><propstat><prop>"
                    "<resourcetype><collection/><C:calendar/></resourcetype><displayname>Home</displayname>"
                    "</prop><status>HTTP/1.1 200 OK</status></propstat></response>"
                ),
            )
        return httpx.Response(404)

    client = make_client(RequestLog(), transport=httpx.MockTransport(handler), auth=httpx.BasicAuth("u", "p"))
    target = CalDavTarget(client, "https://dav.test/")
    (cal,) = await target.list_calendars()
    assert cal.href == "https://p07-dav.test/123/calendars/home/" and cal.name == "Home"
    assert target.href_for(cal, "x.ics") == "https://p07-dav.test/123/calendars/home/x.ics"
    assert [m for m, _, _ in seen] == ["PROPFIND"] * 4  # redirect, root, principal, home
    assert all(has_auth for _, _, has_auth in seen)
````

`tests/test_caldav_target.py`：

````python
"""CalDavTarget against a real local Radicale server."""

from dataclasses import replace

import httpx
import pytest

from calsync.errors import ConflictError, NotFoundError
from calsync.http import RequestLog, make_client
from calsync.naming import resource_name
from calsync.targets.caldav import CalDavTarget, Limiter

pytestmark = [pytest.mark.anyio, pytest.mark.e2e]

OID_A = "a" * 24
OID_B = "b" * 24


def ics(uid: str, summary: str = "x") -> bytes:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:20261024T000000Z\r\nDTSTART:20261025T010000Z\r\n"
        f"DTEND:20261025T020000Z\r\nSUMMARY:{summary}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


@pytest.fixture
def log() -> RequestLog:
    return RequestLog()


@pytest.fixture
def target(radicale_url: str, dav_user: str, log: RequestLog) -> CalDavTarget:
    client = make_client(log, auth=httpx.BasicAuth(dav_user, "pw"))
    return CalDavTarget(client, radicale_url, max_concurrency=2)


async def test_ensure_creates_and_finds_calendar(target: CalDavTarget) -> None:
    with pytest.raises(NotFoundError):
        await target.ensure_calendar("Family", create=False)
    cal = await target.ensure_calendar("Family", create=True)
    assert cal.name == "Family" and cal.mode == "sync"
    assert (await target.ensure_calendar("Family", create=False)).href == cal.href
    assert (await target.ensure_calendar(cal.href, create=False)).href == cal.href


async def test_write_detect_fetch_cycle(target: CalDavTarget, log: RequestLog) -> None:
    cal = await target.ensure_calendar("Family", create=True)
    initial = await target.changes(cal, None)
    assert initial.full and initial.changed == {}

    href_a = target.href_for(cal, resource_name("r", OID_A))
    etag = await target.create(href_a, ics(f"{OID_A}@calsync"))
    assert etag
    with pytest.raises(ConflictError):
        await target.create(href_a, ics(f"{OID_A}@calsync"))
    await target.create(target.href_for(cal, "foreign-event.ics"), ics("foreign"))

    delta = await target.changes(cal, initial.cursor)
    assert not delta.full and delta.changed == {href_a: etag}  # foreign resource is invisible

    mark = log.mark()
    idle = await target.changes(cal, delta.cursor)
    assert idle.changed == {} and idle.deleted == ()
    assert [e.kind for e in log.since(mark)] == ["sync-collection"]

    with pytest.raises(ConflictError):
        await target.update(href_a, '"stale"', ics(f"{OID_A}@calsync", "y"))
    new_etag = await target.update(href_a, etag, ics(f"{OID_A}@calsync", "y"))
    (fetched,) = await target.fetch(cal, [href_a])
    assert fetched.etag == new_etag and b"SUMMARY:y" in fetched.data

    await target.delete(href_a, new_etag)
    await target.delete(href_a, new_etag)  # already gone: no error
    gone = await target.changes(cal, idle.cursor)
    assert gone.deleted == (href_a,)


async def test_invalid_sync_token_falls_back_to_full(target: CalDavTarget) -> None:
    cal = await target.ensure_calendar("Family", create=True)
    href = target.href_for(cal, resource_name("r", OID_B))
    await target.create(href, ics(f"{OID_B}@calsync"))
    delta = await target.changes(cal, "http://radicale.org/ns/sync/deadbeef")
    assert delta.full and list(delta.changed) == [href]


async def test_ctag_and_full_modes(target: CalDavTarget, log: RequestLog) -> None:
    cal = await target.ensure_calendar("Family", create=True)
    href = target.href_for(cal, resource_name("r", OID_A))
    await target.create(href, ics(f"{OID_A}@calsync"))

    ctag_cal = replace(cal, mode="ctag")
    first = await target.changes(ctag_cal, None)
    assert first.full and list(first.changed) == [href]
    mark = log.mark()
    same = await target.changes(ctag_cal, first.cursor)
    assert same.changed == {} and not same.full
    assert [e.kind for e in log.since(mark)] == ["propfind"]

    full = await target.changes(replace(cal, mode="full"), None)
    assert full.full and list(full.changed) == [href]


async def test_limiter_throttle_and_reset() -> None:
    limiter = Limiter(3)
    limiter.throttle()
    assert limiter.limit == 1
    limiter.reset()
    assert limiter.limit == 3
    async with limiter:
        pass
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_caldav_discovery.py tests/test_caldav_target.py -v`

Expected: FAIL，收集阶段报错 `ModuleNotFoundError: No module named 'calsync.targets.caldav'`

- [ ] **Step 3: 写实现**

`src/calsync/targets/base.py`：

````python
"""Target adapter interface and the local cache of managed target resources."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

DetectMode = Literal["sync", "ctag", "full"]


@dataclass(frozen=True, slots=True)
class TargetCalendar:
    href: str
    name: str
    mode: DetectMode  # best change-detection rung the server supports


@dataclass(frozen=True, slots=True)
class TargetDelta:
    changed: dict[str, str | None]  # managed href -> etag (None if the server omitted it)
    deleted: tuple[str, ...]
    full: bool  # True: ``changed`` lists every managed resource that exists
    cursor: str | None


@dataclass(frozen=True, slots=True)
class FetchedObject:
    href: str
    etag: str | None
    data: bytes


@dataclass(frozen=True, slots=True)
class CachedObject:
    etag: str | None  # None: we wrote it and the server returned no ETag yet
    rule_tag: str
    object_id: str
    content_hash: str | None
    span: tuple[datetime, datetime] | None


@dataclass(slots=True)
class TargetCache:
    cursor: str | None = None
    objects: dict[str, CachedObject] = field(default_factory=dict)  # href -> object


class Target(Protocol):
    def reset(self) -> None: ...

    async def list_calendars(self) -> list[TargetCalendar]: ...

    async def ensure_calendar(self, ref: str, *, create: bool) -> TargetCalendar: ...

    async def changes(self, cal: TargetCalendar, cursor: str | None) -> TargetDelta: ...

    async def fetch(self, cal: TargetCalendar, hrefs: Sequence[str]) -> list[FetchedObject]: ...

    def href_for(self, cal: TargetCalendar, name: str) -> str: ...

    async def create(self, href: str, data: bytes) -> str | None: ...

    async def update(self, href: str, etag: str | None, data: bytes) -> str | None: ...

    async def delete(self, href: str, etag: str | None) -> None: ...
````

`src/calsync/targets/caldav.py`：

````python
"""CalDAV target: discovery, change-detection ladder, conditional writes, throttling."""

import asyncio
import uuid
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx

from calsync.errors import ConfigError, CursorExpired, NotFoundError
from calsync.http import Sleep
from calsync.naming import parse_resource_name
from calsync.targets.base import DetectMode, FetchedObject, TargetCalendar, TargetDelta
from calsync.targets.dav import (
    CALDAV,
    CS,
    DAV,
    MULTIGET_BATCH,
    DavClient,
    DavResponse,
    calendar_query_etags_body,
    multiget_body,
    q,
    sync_collection_body,
)

CALENDAR_PROPS = (
    q(DAV, "resourcetype"),
    q(DAV, "displayname"),
    q(CS, "getctag"),
    q(DAV, "sync-token"),
    q(DAV, "supported-report-set"),
)


class Limiter:
    """Concurrency limit that can drop to 1 when the server starts throttling."""

    def __init__(self, limit: int) -> None:
        self._configured = limit
        self._limit = limit
        self._active = 0
        self._cond = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    def throttle(self) -> None:
        self._limit = 1

    def reset(self) -> None:
        self._limit = self._configured

    async def __aenter__(self) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: self._active < self._limit)
            self._active += 1

    async def __aexit__(self, *_: object) -> None:
        async with self._cond:
            self._active -= 1
            self._cond.notify_all()


def _same_href(href: str, ref: str) -> bool:
    """``href`` is absolute; ``ref`` may be an absolute URL or just its path."""
    if "://" not in ref:
        href = urlsplit(href).path
    return href.rstrip("/") == ref.rstrip("/")


def _detect_mode(response: DavResponse) -> DetectMode:
    reports = response.props.get(q(DAV, "supported-report-set"))
    if reports is not None and reports.find(f".//{q(DAV, 'sync-collection')}") is not None:
        return "sync"
    if response.text(q(CS, "getctag")):
        return "ctag"
    return "full"


class CalDavTarget:
    def __init__(
        self,
        client: httpx.AsyncClient,
        server_url: str,
        *,
        max_concurrency: int = 2,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._limiter = Limiter(max_concurrency)
        self._dav = DavClient(client, server_url, sleep=sleep, on_throttle=self._limiter.throttle)
        self._home: str | None = None

    @property
    def limiter(self) -> Limiter:
        return self._limiter

    def reset(self) -> None:
        self._limiter.reset()

    async def _calendar_home(self) -> str:
        if self._home is None:
            (root,) = await self._dav.propfind("", [q(DAV, "current-user-principal")], depth=0)
            principal = root.href_prop(q(DAV, "current-user-principal"))
            if principal is None:
                raise NotFoundError("server did not report current-user-principal")
            (pr,) = await self._dav.propfind(principal, [q(CALDAV, "calendar-home-set")], depth=0)
            home = pr.href_prop(q(CALDAV, "calendar-home-set"))
            if home is None:
                raise NotFoundError("server did not report calendar-home-set")
            self._home = home
        return self._home

    async def list_calendars(self) -> list[TargetCalendar]:
        home = await self._calendar_home()
        calendars = []
        for item in await self._dav.propfind(home, CALENDAR_PROPS, depth=1):
            kind = item.props.get(q(DAV, "resourcetype"))
            if kind is None or kind.find(q(CALDAV, "calendar")) is None:
                continue
            name = item.text(q(DAV, "displayname")) or item.href.rstrip("/").rsplit("/", 1)[-1]
            calendars.append(TargetCalendar(item.href, name, _detect_mode(item)))
        return calendars

    async def ensure_calendar(self, ref: str, *, create: bool) -> TargetCalendar:
        calendars = await self.list_calendars()
        matches = [c for c in calendars if _same_href(c.href, ref)] or [c for c in calendars if c.name == ref]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ConfigError([f"CalDAV calendar {ref!r} matches {len(matches)} calendars; use its href"])
        if not create:
            raise NotFoundError(f"CalDAV calendar {ref!r} not found")
        href = f"{(await self._calendar_home()).rstrip('/')}/{uuid.uuid4().hex}/"
        await self._dav.mkcalendar(href, ref)
        for cal in await self.list_calendars():
            if _same_href(cal.href, href):
                return cal
        raise NotFoundError(f"created calendar {href} but it is not listed")

    @staticmethod
    def _managed(cal: TargetCalendar, responses: list[DavResponse]) -> list[DavResponse]:
        return [r for r in responses if not _same_href(r.href, cal.href) and parse_resource_name(r.href)]

    async def _full_listing(self, cal: TargetCalendar, cursor: str | None) -> TargetDelta:
        responses, _ = await self._dav.report(cal.href, calendar_query_etags_body(), kind="calendar-query")
        changed = {r.href: r.text(q(DAV, "getetag")) for r in self._managed(cal, responses)}
        return TargetDelta(changed, (), True, cursor)

    async def _sync(self, cal: TargetCalendar, token: str | None) -> TargetDelta:
        responses, new_token = await self._dav.report(
            cal.href, sync_collection_body(token), kind="sync-collection"
        )
        changed: dict[str, str | None] = {}
        deleted: list[str] = []
        for r in self._managed(cal, responses):
            if r.status == 404:
                deleted.append(r.href)
            else:
                changed[r.href] = r.text(q(DAV, "getetag"))
        return TargetDelta(changed, tuple(deleted), token is None, new_token)

    async def changes(self, cal: TargetCalendar, cursor: str | None) -> TargetDelta:
        if cal.mode == "sync":
            try:
                return await self._sync(cal, cursor)
            except CursorExpired:
                return await self._sync(cal, None)
        if cal.mode == "ctag":
            (item,) = await self._dav.propfind(cal.href, [q(CS, "getctag")], depth=0)
            ctag = item.text(q(CS, "getctag"))
            if cursor is not None and ctag == cursor:
                return TargetDelta({}, (), False, cursor)
            return await self._full_listing(cal, ctag)
        return await self._full_listing(cal, None)

    async def fetch(self, cal: TargetCalendar, hrefs: Sequence[str]) -> list[FetchedObject]:
        objects: list[FetchedObject] = []
        for start in range(0, len(hrefs), MULTIGET_BATCH):
            batch = hrefs[start : start + MULTIGET_BATCH]
            paths = [urlsplit(href).path for href in batch]
            responses, _ = await self._dav.report(cal.href, multiget_body(paths), kind="calendar-multiget")
            for r in responses:
                data = r.props.get(q(CALDAV, "calendar-data"))
                if r.status == 200 and data is not None and data.text:
                    objects.append(FetchedObject(r.href, r.text(q(DAV, "getetag")), data.text.encode()))
        return objects

    def href_for(self, cal: TargetCalendar, name: str) -> str:
        return f"{cal.href.rstrip('/')}/{name}"

    async def create(self, href: str, data: bytes) -> str | None:
        async with self._limiter:
            return await self._dav.put(href, data, create_only=True)

    async def update(self, href: str, etag: str | None, data: bytes) -> str | None:
        async with self._limiter:
            return await self._dav.put(href, data, if_match=etag)

    async def delete(self, href: str, etag: str | None) -> None:
        async with self._limiter:
            try:
                await self._dav.delete(href, if_match=etag)
            except NotFoundError:
                return
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `87 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/test_caldav_discovery.py tests/test_caldav_target.py src/calsync/targets/base.py src/calsync/targets/caldav.py
git commit -m "feat(caldav): CalDAV target with change-detection ladder and throttling" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: 本地状态存储与源镜像

SQLite（WAL）持久化源镜像、游标、目标日历、目标缓存和运行记录；所有表都可重建，模式版本不符时直接清空重建。

**Files:**
- Create: `tests/test_store.py`
- Create: `src/calsync/mirror.py`
- Create: `src/calsync/store.py`

**Interfaces:**
- Consumes:
  - `calsync.sources.base.CalendarPull`
  - `calsync.targets.base`：`CachedObject`、`TargetCache`、`TargetCalendar`
- Produces:
  - `calsync.mirror`：`Mirror = dict[str, dict[str, Any]]`、`apply_pull(mirror, pull) -> None`
  - `calsync.store`：`SCHEMA_VERSION = 1`、`RunRecord(rule, started_at, finished_at, status, created, updated, deleted, failed, requests, error)`、`Store(path)`（上下文管理器）：`load_source(account) -> (state, {calendar: Mirror})`、`save_source(account, state, pulls)`、`load_target_calendar(account, ref)`、`save_target_calendar(account, ref, cal)`、`forget_target_calendar(account, ref)`、`load_target_cache(account, calendar_href)`、`save_target_cache(account, calendar_href, cache)`、`record_runs(runs)`、`latest_runs()`、`close()`

**注意：**
- 不要在事务里调用 `executescript()`：它会先提交当前事务。建表语句逐条 `execute`。
- 镜像行和产生它们的游标在同一个事务里提交。
- 目标日历的 href 变化时（日历被移动或重建），游标自动清空。

- [ ] **Step 1: 写失败的测试**

`tests/test_store.py`：

````python
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from calsync import store as store_mod
from calsync.mirror import apply_pull
from calsync.sources.base import CalendarPull
from calsync.store import RunRecord, Store
from calsync.targets.base import CachedObject, TargetCache, TargetCalendar

T0 = datetime(2026, 10, 24, tzinfo=UTC)
T1 = datetime(2026, 10, 25, tzinfo=UTC)


def test_apply_pull() -> None:
    mirror = {"old": {"id": "old"}}
    apply_pull(mirror, CalendarPull("c", full=False, upserts=[{"id": "a"}], deletes=["old"]))
    assert mirror == {"a": {"id": "a"}}
    apply_pull(mirror, CalendarPull("c", full=True, upserts=[{"id": "b"}]))
    assert mirror == {"b": {"id": "b"}}


def test_source_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "state" / "state.db"
    with Store(db) as store:
        store.save_source(
            "g",
            {"calendars_token": "t1"},
            [
                CalendarPull("cal", full=True, upserts=[{"id": "a", "summary": "日程"}, {"id": "b"}]),
            ],
        )
        store.save_source(
            "g",
            {"calendars_token": "t2"},
            [
                CalendarPull("cal", full=False, upserts=[{"id": "c"}], deletes=["a"]),
            ],
        )
    with Store(db) as store:
        state, mirrors = store.load_source("g")
        assert state == {"calendars_token": "t2"}
        assert sorted(mirrors["cal"]) == ["b", "c"]
        assert store.load_source("other") == ({}, {})


def test_target_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cal = TargetCalendar("/u/cal/", "Family", "sync")
    cache = TargetCache(
        cursor="tok1",
        objects={
            "/u/cal/x.ics": CachedObject('"e1"', "tag", "oid", "hash", (T0, T1)),
            "/u/cal/y.ics": CachedObject(None, "tag", "oid2", None, None),
        },
    )
    with Store(db) as store:
        assert store.load_target_calendar("d", "Family") is None
        store.save_target_calendar("d", "Family", cal)
        store.save_target_cache("d", cal.href, cache)
    with Store(db) as store:
        assert store.load_target_calendar("d", "Family") == cal
        loaded = store.load_target_cache("d", cal.href)
        assert loaded.cursor == "tok1" and loaded.objects == cache.objects
        store.save_target_calendar("d", "Family", TargetCalendar("/u/other/", "Family", "sync"))
        assert store.load_target_cache("d", "/u/other/").cursor is None  # moved: cursor reset
        store.forget_target_calendar("d", "Family")
        assert store.load_target_calendar("d", "Family") is None


def test_runs(tmp_path: Path) -> None:
    with Store(tmp_path / "s.db") as store:
        store.record_runs([RunRecord("a", T0, T0, "failed", 0, 0, 0, 0, 3, "boom")])
        store.record_runs(
            [
                RunRecord("a", T1, T1, "ok", 1, 0, 0, 0, 4, None),
                RunRecord("b", T1, T1, "ok", 0, 0, 0, 0, 4, None),
            ]
        )
        latest = store.latest_runs()
    assert [(r.rule, r.status, r.requests) for r in latest] == [("a", "ok", 4), ("b", "ok", 4)]


def test_schema_version_mismatch_rebuilds(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "s.db"
    with Store(db) as store:
        store.save_source("g", {"x": 1}, [])
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", store_mod.SCHEMA_VERSION + 1)
    with Store(db) as store:
        assert store.load_source("g") == ({}, {})
    version = sqlite3.connect(db).execute("SELECT value FROM meta").fetchone()[0]
    assert int(version) == store_mod.SCHEMA_VERSION
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_store.py -v`

Expected: FAIL，收集阶段报错 `ImportError: cannot import name 'store' from 'calsync'`

- [ ] **Step 3: 写实现**

`src/calsync/mirror.py`：

````python
"""Apply a source pull to the in-memory mirror of one calendar."""

from typing import Any

from calsync.sources.base import CalendarPull

Mirror = dict[str, dict[str, Any]]  # event id -> raw event


def apply_pull(mirror: Mirror, pull: CalendarPull) -> None:
    if pull.full:
        mirror.clear()
    for event in pull.upserts:
        mirror[event["id"]] = event
    for event_id in pull.deletes:
        mirror.pop(event_id, None)
````

`src/calsync/store.py`：

````python
"""SQLite persistence. Every table is a rebuildable cache, so a schema change
simply drops and recreates everything instead of migrating."""

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from calsync.mirror import Mirror
from calsync.sources.base import CalendarPull
from calsync.targets.base import CachedObject, TargetCache, TargetCalendar

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE source_state (account TEXT PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE source_event (
    account TEXT NOT NULL, calendar TEXT NOT NULL, event_id TEXT NOT NULL, raw TEXT NOT NULL,
    PRIMARY KEY (account, calendar, event_id)
);
CREATE TABLE target_calendar (
    account TEXT NOT NULL, ref TEXT NOT NULL, href TEXT NOT NULL, name TEXT NOT NULL,
    mode TEXT NOT NULL, cursor TEXT,
    PRIMARY KEY (account, ref)
);
CREATE TABLE target_object (
    account TEXT NOT NULL, calendar_href TEXT NOT NULL, href TEXT NOT NULL, etag TEXT,
    rule_tag TEXT NOT NULL, object_id TEXT NOT NULL, content_hash TEXT,
    span_start TEXT, span_end TEXT,
    PRIMARY KEY (account, calendar_href, href)
);
CREATE TABLE run (
    id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL,
    updated INTEGER NOT NULL, deleted INTEGER NOT NULL, failed INTEGER NOT NULL,
    requests INTEGER NOT NULL, error TEXT
);
CREATE INDEX run_rule ON run (rule, id);
CREATE TABLE alert (key TEXT PRIMARY KEY, state TEXT NOT NULL, last_sent_at TEXT);
"""
_TABLES = ("meta", "source_state", "source_event", "target_calendar", "target_object", "run", "alert")


@dataclass(frozen=True, slots=True)
class RunRecord:
    rule: str
    started_at: datetime
    finished_at: datetime
    status: str
    created: int
    updated: int
    deleted: int
    failed: int
    requests: int
    error: str | None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _ensure_schema(self) -> None:
        try:
            row = self._db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.OperationalError:
            row = None
        if row and int(row[0]) == SCHEMA_VERSION:
            return
        with self._tx():
            for table in _TABLES:
                self._db.execute(f"DROP TABLE IF EXISTS {table}")
            # executescript() would commit our open transaction, so run statements one by one.
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    self._db.execute(statement)
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    def _tx(self) -> "_Transaction":
        return _Transaction(self._db)

    # ----- source side --------------------------------------------------
    def load_source(self, account: str) -> tuple[dict[str, Any], dict[str, Mirror]]:
        row = self._db.execute("SELECT state FROM source_state WHERE account = ?", (account,)).fetchone()
        state = json.loads(row[0]) if row else {}
        mirrors: dict[str, Mirror] = {}
        for calendar, event_id, raw in self._db.execute(
            "SELECT calendar, event_id, raw FROM source_event WHERE account = ?", (account,)
        ):
            mirrors.setdefault(calendar, {})[event_id] = json.loads(raw)
        return state, mirrors

    def save_source(self, account: str, state: Mapping[str, Any], pulls: Iterable[CalendarPull]) -> None:
        """Mirror rows and the cursor that produced them commit together."""
        with self._tx():
            for pull in pulls:
                if pull.full:
                    self._db.execute(
                        "DELETE FROM source_event WHERE account = ? AND calendar = ?",
                        (account, pull.calendar_id),
                    )
                self._db.executemany(
                    "INSERT OR REPLACE INTO source_event (account, calendar, event_id, raw) VALUES (?, ?, ?, ?)",
                    [
                        (account, pull.calendar_id, e["id"], json.dumps(e, ensure_ascii=False))
                        for e in pull.upserts
                    ],
                )
                self._db.executemany(
                    "DELETE FROM source_event WHERE account = ? AND calendar = ? AND event_id = ?",
                    [(account, pull.calendar_id, event_id) for event_id in pull.deletes],
                )
            self._db.execute(
                "INSERT OR REPLACE INTO source_state (account, state) VALUES (?, ?)",
                (account, json.dumps(state, ensure_ascii=False)),
            )

    # ----- target side --------------------------------------------------
    def load_target_calendar(self, account: str, ref: str) -> TargetCalendar | None:
        row = self._db.execute(
            "SELECT href, name, mode FROM target_calendar WHERE account = ? AND ref = ?", (account, ref)
        ).fetchone()
        return TargetCalendar(row[0], row[1], row[2]) if row else None

    def save_target_calendar(self, account: str, ref: str, cal: TargetCalendar) -> None:
        self._db.execute(
            "INSERT INTO target_calendar (account, ref, href, name, mode) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (account, ref) DO UPDATE SET href = excluded.href, name = excluded.name, "
            "mode = excluded.mode, cursor = CASE WHEN target_calendar.href = excluded.href "
            "THEN target_calendar.cursor ELSE NULL END",
            (account, ref, cal.href, cal.name, cal.mode),
        )

    def forget_target_calendar(self, account: str, ref: str) -> None:
        self._db.execute("DELETE FROM target_calendar WHERE account = ? AND ref = ?", (account, ref))

    def load_target_cache(self, account: str, calendar_href: str) -> TargetCache:
        row = self._db.execute(
            "SELECT cursor FROM target_calendar WHERE account = ? AND href = ? LIMIT 1",
            (account, calendar_href),
        ).fetchone()
        cache = TargetCache(cursor=row[0] if row else None)
        for href, etag, tag, oid, content_hash, lo, hi in self._db.execute(
            "SELECT href, etag, rule_tag, object_id, content_hash, span_start, span_end "
            "FROM target_object WHERE account = ? AND calendar_href = ?",
            (account, calendar_href),
        ):
            span = (_dt(lo), _dt(hi)) if lo and hi else None
            cache.objects[href] = CachedObject(etag, tag, oid, content_hash, span)  # type: ignore[arg-type]
        return cache

    def save_target_cache(self, account: str, calendar_href: str, cache: TargetCache) -> None:
        rows = [
            (
                account,
                calendar_href,
                href,
                obj.etag,
                obj.rule_tag,
                obj.object_id,
                obj.content_hash,
                obj.span[0].isoformat() if obj.span else None,
                obj.span[1].isoformat() if obj.span else None,
            )
            for href, obj in cache.objects.items()
        ]
        with self._tx():
            self._db.execute(
                "DELETE FROM target_object WHERE account = ? AND calendar_href = ?", (account, calendar_href)
            )
            self._db.executemany("INSERT INTO target_object VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self._db.execute(
                "UPDATE target_calendar SET cursor = ? WHERE account = ? AND href = ?",
                (cache.cursor, account, calendar_href),
            )

    # ----- runs ---------------------------------------------------------
    def record_runs(self, runs: Sequence[RunRecord]) -> None:
        with self._tx():
            self._db.executemany(
                "INSERT INTO run (rule, started_at, finished_at, status, created, updated, deleted, "
                "failed, requests, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r.rule,
                        r.started_at.isoformat(),
                        r.finished_at.isoformat(),
                        r.status,
                        r.created,
                        r.updated,
                        r.deleted,
                        r.failed,
                        r.requests,
                        r.error,
                    )
                    for r in runs
                ],
            )

    def latest_runs(self) -> list[RunRecord]:
        rows = self._db.execute(
            "SELECT rule, started_at, finished_at, status, created, updated, deleted, failed, requests, error "
            "FROM run WHERE id IN (SELECT MAX(id) FROM run GROUP BY rule) ORDER BY rule"
        ).fetchall()
        return [
            RunRecord(r[0], datetime.fromisoformat(r[1]), datetime.fromisoformat(r[2]), *r[3:]) for r in rows
        ]


class _Transaction:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def __enter__(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: object, *_: object) -> None:
        self._db.execute("ROLLBACK" if exc_type else "COMMIT")
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `92 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/test_store.py src/calsync/mirror.py src/calsync/store.py
git commit -m "feat(store): SQLite state as a rebuildable cache" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: 同步引擎

编排一个同步周期：拉取 → 检测 → 规划 → 防误删 → 推送 → 记录；规则之间隔离；内存缓存跨周期复用并在每个阶段落盘。

**Files:**
- Create: `tests/harness.py`
- Create: `tests/test_engine.py`
- Create: `src/calsync/engine.py`

**Interfaces:**
- Consumes:
  - Task 2–12 的全部公开接口。
- Produces:
  - `RuleResult(rule, status="ok", created=0, updated=0, deleted=0, failed=0, error=None, error_kind=None, plan=None)`；`status` 取值 `ok | partial | failed | blocked`，`error_kind` 取值 `auth | guard | other`
  - `CycleResult(started_at, finished_at, rules: dict[str, RuleResult], requests)`，属性 `ok`
  - `apply_target_delta(cache, delta) -> list[str]`（需要拉正文的 href）
  - `Engine(config, store, sources, targets, *, request_log=None, clock=...)`，`await engine.cycle(*, rules=None, dry_run=False, allow_deletes=False) -> CycleResult`
  - 测试工具 `tests/harness.py`：`GCAL`、`NOW`、`config_for(rules, *, extra_google=False)`、`rule(name="family", *, source="Family", target="Family", **extra)`、`build(tmp_path, radicale_url, dav_user, fake, config, *, broken_google=False, clock=...) -> Harness(fake, log, engine, target, store)`

**注意：**
- 新建遇到 412（资源已存在，通常是本地状态丢了）时，取最新 etag 后改为更新，所以冷启动不会产生重复事件。
- 自己写入后若服务器返回了 ETag，下一轮变化检测看到相同 etag 就不再拉正文；没返回 ETag 时缓存记为"待定"，下一轮直接采信服务器给的 etag。
- 一条规则失败只影响它自己；某个源账号拉取失败，只让引用它的规则失败。

- [ ] **Step 1: 写失败的测试**

`tests/harness.py`：

````python
"""Wire an Engine to FakeGoogle (in memory) and a real local Radicale server."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from calsync.config import Config, parse_config
from calsync.engine import Engine
from calsync.http import RequestLog, make_client
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import StaticToken
from calsync.store import Store
from calsync.targets.caldav import CalDavTarget
from tests.fakes.google import BASE_URL, FakeGoogle

GCAL = "family@group.calendar.google.com"
NOW = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)


def config_for(rules: list[dict[str, Any]], *, extra_google: bool = False) -> Config:
    accounts: dict[str, Any] = {
        "me": {"provider": "google", "credential": {"backend": "env", "ref": "UNUSED"}},
        "dav": {
            "provider": "caldav",
            "server_url": "http://unused/",
            "username": "u",
            "credential": {"backend": "env", "ref": "UNUSED"},
        },
    }
    if extra_google:
        accounts["broken"] = {"provider": "google", "credential": {"backend": "env", "ref": "UNUSED"}}
    data = {"google": {"client_secret_file": "unused.json"}, "accounts": accounts, "rules": rules}
    return parse_config(data, base_dir=Path("."))


def rule(
    name: str = "family", *, source: str = "Family", target: str = "Family", **extra: Any
) -> dict[str, Any]:
    return {
        "name": name,
        "source": {"account": "me", "calendar": source},
        "target": {"account": "dav", "calendar": target, "create_if_missing": True},
        **extra,
    }


@dataclass
class Harness:
    fake: FakeGoogle
    log: RequestLog
    engine: Engine
    target: CalDavTarget
    store: Store


def build(
    tmp_path: Path,
    radicale_url: str,
    dav_user: str,
    fake: FakeGoogle,
    config: Config,
    *,
    broken_google: bool = False,
    clock: Any = lambda: NOW,
) -> Harness:
    log = RequestLog()
    google_client = make_client(log, transport=fake.transport())
    sources: dict[str, Any] = {"me": GoogleSource(google_client, StaticToken("t"), base_url=BASE_URL)}
    if broken_google:

        def deny(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "revoked"})

        broken = make_client(log, transport=httpx.MockTransport(deny))
        sources["broken"] = GoogleSource(broken, StaticToken("t"), base_url=BASE_URL)
    dav_client = make_client(log, auth=httpx.BasicAuth(dav_user, "pw"))
    target = CalDavTarget(dav_client, radicale_url, max_concurrency=2)
    store = Store(tmp_path / "state.db")
    engine = Engine(config, store, sources, {"dav": target}, request_log=log, clock=clock)
    return Harness(fake, log, engine, target, store)
````

`tests/test_engine.py`：

````python
from pathlib import Path

import pytest

from calsync.engine import apply_target_delta
from calsync.naming import parse_resource_name
from calsync.render import inspect
from calsync.targets.base import CachedObject, TargetCache, TargetDelta
from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, Harness, build, config_for, rule

pytestmark = pytest.mark.anyio


def seeded() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(
        GCAL,
        google_event(
            "standup",
            "Standup",
            "2026-10-23T09:30:00+08:00",
            "2026-10-23T09:35:00+08:00",
            recurrence=["RRULE:FREQ=DAILY;COUNT=21"],
        ),
    )
    fake.put_event(
        GCAL,
        google_event(
            "standup_20261025T013000Z",
            "Standup (late)",
            "2026-10-25T21:00:00+08:00",
            "2026-10-25T21:05:00+08:00",
            recurringEventId="standup",
            originalStartTime={"dateTime": "2026-10-25T09:30:00+08:00", "timeZone": "Asia/Shanghai"},
        ),
    )
    fake.put_event(
        GCAL, google_event("checkup", "Checkup", "2026-10-28T10:00:00+08:00", "2026-10-28T11:00:00+08:00")
    )
    return fake


async def managed(h: Harness) -> dict[str, bytes]:
    cal = await h.target.ensure_calendar("Family", create=False)
    delta = await h.target.changes(cal, None)
    return {o.href: o.data for o in await h.target.fetch(cal, list(delta.changed))}


# ----- pure ---------------------------------------------------------------


def test_apply_target_delta() -> None:
    mine = CachedObject('"e1"', "t", "o1", "h", None)
    pending = CachedObject(None, "t", "o2", "h", None)
    cache = TargetCache("c0", {"/a": mine, "/b": pending, "/c": mine, "/d": mine})
    need = apply_target_delta(
        cache, TargetDelta({"/a": '"e1"', "/b": '"e2"', "/c": '"x"', "/new": '"n"'}, ("/d",), False, "c1")
    )
    assert need == ["/c", "/new"]
    assert cache.objects["/b"].etag == '"e2"' and "/d" not in cache.objects
    full = apply_target_delta(cache, TargetDelta({"/a": '"e1"'}, (), True, "c2"))
    assert full == [] and list(cache.objects) == ["/a"]


# ----- against Radicale -----------------------------------------------------

pytest_e2e = pytest.mark.e2e


@pytest_e2e
async def test_first_cycle_creates_then_idles(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    h = build(tmp_path, radicale_url, dav_user, seeded(), config_for([rule()]))
    first = await h.engine.cycle()
    res = first.rules["family"]
    assert (res.status, res.created, res.updated, res.deleted) == ("ok", 2, 0, 0)

    bodies = await managed(h)
    assert len(bodies) == 2 and all(parse_resource_name(href) for href in bodies)
    series_body = next(b for b in bodies.values() if b"RRULE" in b)
    assert b"RECURRENCE-ID" in series_body and series_body.count(b"BEGIN:VALARM") == 2

    second = await h.engine.cycle()
    assert second.rules["family"].plan.empty
    mark = h.log.mark()
    third = await h.engine.cycle()
    assert third.rules["family"].plan.empty
    assert sorted(e.kind for e in h.log.since(mark)) == [
        "google-calendarlist",
        "google-events",
        "sync-collection",
    ]


@pytest_e2e
async def test_update_delete_and_foreign_untouched(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    fake = seeded()
    h = build(tmp_path, radicale_url, dav_user, fake, config_for([rule()]))
    await h.engine.cycle()
    cal = await h.target.ensure_calendar("Family", create=False)
    foreign = h.target.href_for(cal, "my-own-event.ics")
    await h.target.create(
        foreign,
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:mine\r\n"
            b"DTSTAMP:20261024T000000Z\r\nDTSTART:20261026T010000Z\r\nDTEND:20261026T020000Z\r\n"
            b"END:VEVENT\r\nEND:VCALENDAR\r\n"
        ),
    )

    fake.put_event(
        GCAL,
        google_event("checkup", "Checkup (moved)", "2026-10-29T10:00:00+08:00", "2026-10-29T11:00:00+08:00"),
    )
    res = (await h.engine.cycle()).rules["family"]
    assert (res.created, res.updated, res.deleted) == (0, 1, 0)
    assert any(b"Checkup (moved)" in b for b in (await managed(h)).values())

    fake.cancel_event(GCAL, "checkup")
    res = (await h.engine.cycle()).rules["family"]
    assert (res.created, res.updated, res.deleted) == (0, 0, 1)
    listing = await h.target.changes(cal, None)
    assert len(listing.changed) == 1  # only the recurring series is still managed
    (still_there,) = await h.target.fetch(cal, [foreign])
    assert b"UID:mine" in still_there.data


@pytest_e2e
async def test_dry_run_writes_nothing(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    h = build(tmp_path, radicale_url, dav_user, seeded(), config_for([rule()]))
    await h.target.ensure_calendar("Family", create=True)
    result = await h.engine.cycle(dry_run=True)
    assert len(result.rules["family"].plan.create) == 2
    assert await managed(h) == {}
    assert h.store.latest_runs() == []


@pytest_e2e
async def test_delete_guard_blocks_mass_delete(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for i in range(12):
        fake.put_event(
            GCAL,
            google_event(
                f"e{i}",
                f"E{i}",
                f"2026-10-{25 + i % 3}T09:00:00+08:00",
                f"2026-10-{25 + i % 3}T09:05:00+08:00",
            ),
        )
    h = build(tmp_path, radicale_url, dav_user, fake, config_for([rule()]))
    await h.engine.cycle()
    for i in range(12):
        fake.cancel_event(GCAL, f"e{i}")
    blocked = (await h.engine.cycle()).rules["family"]
    assert blocked.status == "blocked" and blocked.error_kind == "guard"
    assert len(await managed(h)) == 12
    forced = (await h.engine.cycle(allow_deletes=True)).rules["family"]
    assert forced.deleted == 12 and await managed(h) == {}


@pytest_e2e
async def test_lost_state_reconciles_without_duplicates(
    tmp_path: Path, radicale_url: str, dav_user: str
) -> None:
    fake = seeded()
    h = build(tmp_path, radicale_url, dav_user, fake, config_for([rule()]))
    await h.engine.cycle()
    h.store.close()
    (tmp_path / "state.db").unlink()
    fresh = build(tmp_path, radicale_url, dav_user, fake, config_for([rule()]))
    res = (await fresh.engine.cycle()).rules["family"]
    # updated == 0 relies on the server keeping X-CALSYNC-HASH (Radicale does; for iCloud see
    # spec §13 item 2 - if it strips the property, each resource is rewritten once here).
    assert (res.status, res.created, res.updated, res.deleted) == ("ok", 0, 0, 0)
    assert "calendar-multiget" in fresh.log.kinds()
    assert len(await managed(fresh)) == 2


@pytest_e2e
async def test_failing_source_only_fails_its_rules(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    rules = [rule("good"), {**rule("bad"), "source": {"account": "broken", "calendar": "Other"}}]
    h = build(
        tmp_path, radicale_url, dav_user, seeded(), config_for(rules, extra_google=True), broken_google=True
    )
    result = await h.engine.cycle()
    assert result.rules["good"].status == "ok" and result.rules["good"].created == 2
    assert result.rules["bad"].status == "failed" and result.rules["bad"].error_kind == "auth"
    assert not result.ok
    assert {r.rule: r.status for r in h.store.latest_runs()} == {"bad": "failed", "good": "ok"}


@pytest_e2e
async def test_two_rules_share_one_target_calendar(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    fake = seeded()
    fake.add_calendar("work@group.calendar.google.com", "Work")
    fake.put_event(
        "work@group.calendar.google.com",
        google_event("w1", "Standup", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00"),
    )
    rules = [rule("family"), rule("work", source="Work")]
    h = build(tmp_path, radicale_url, dav_user, fake, config_for(rules))
    result = await h.engine.cycle()
    assert result.rules["family"].created == 2 and result.rules["work"].created == 1
    fake.cancel_event("work@group.calendar.google.com", "w1")
    result = await h.engine.cycle()
    assert result.rules["work"].deleted == 1 and result.rules["family"].plan.empty
    titles = [inspect(b) for b in (await managed(h)).values()]
    assert len(titles) == 2 and {t.rule for t in titles if t} == {"family"}
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_engine.py -v`

Expected: FAIL，收集阶段报错 `ModuleNotFoundError: No module named 'calsync.engine'`

- [ ] **Step 3: 写实现**

`src/calsync/engine.py`：

````python
"""One sync cycle: ingest sources, detect target changes, plan, guard, push, record.

The engine keeps mirrors and target caches in memory across cycles (the
daemon case) and persists them after every phase, so a fresh process picks
up exactly where the last one stopped.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from calsync.config import Config, Rule
from calsync.errors import AuthError, ConflictError, NotFoundError
from calsync.http import RequestLog
from calsync.mirror import Mirror, apply_pull
from calsync.model import Window, apply_alarm_policy, series_hash, series_span
from calsync.naming import object_id, parse_resource_name, resource_name, rule_tag, uid_for
from calsync.planner import Actual, Desired, Plan, check_delete_guard, plan
from calsync.render import inspect, render
from calsync.sources.base import CalendarMeta, Source
from calsync.store import RunRecord, Store
from calsync.targets.base import CachedObject, Target, TargetCache, TargetCalendar, TargetDelta

log = logging.getLogger(__name__)

Status = Literal["ok", "partial", "failed", "blocked"]
ErrorKind = Literal["auth", "guard", "other"]


@dataclass(slots=True)
class RuleResult:
    rule: str
    status: Status = "ok"
    created: int = 0
    updated: int = 0
    deleted: int = 0
    failed: int = 0
    error: str | None = None
    error_kind: ErrorKind | None = None
    plan: Plan | None = None

    def fail(self, exc: BaseException) -> None:
        self.status = "failed"
        self.error = f"{type(exc).__name__}: {exc}"
        self.error_kind = "auth" if isinstance(exc, AuthError) else "other"


@dataclass(slots=True)
class CycleResult:
    started_at: datetime
    finished_at: datetime
    rules: dict[str, RuleResult] = field(default_factory=dict)
    requests: int = 0

    @property
    def ok(self) -> bool:
        return all(r.status == "ok" for r in self.rules.values())


def apply_target_delta(cache: TargetCache, delta: TargetDelta) -> list[str]:
    """Fold a change listing into the cache; return hrefs whose bodies must be fetched."""
    if delta.full:
        for href in [h for h in cache.objects if h not in delta.changed]:
            del cache.objects[href]
    for href in delta.deleted:
        cache.objects.pop(href, None)
    need: list[str] = []
    for href, etag in delta.changed.items():
        known = cache.objects.get(href)
        if known is None:
            need.append(href)
        elif known.etag is None and etag is not None:
            cache.objects[href] = replace(known, etag=etag)  # our own write: adopt the etag
        elif etag is None or known.etag != etag:
            need.append(href)
    return need


TargetKey = tuple[str, str]  # (account, calendar ref from config)


class Engine:
    def __init__(
        self,
        config: Config,
        store: Store,
        sources: Mapping[str, Source],
        targets: Mapping[str, Target],
        *,
        request_log: RequestLog | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._store = store
        self._sources = sources
        self._targets = targets
        self._log = request_log
        self._clock = clock
        self._source_state: dict[str, dict[str, Any]] = {}
        self._mirrors: dict[str, dict[str, Mirror]] = {}
        self._meta: dict[tuple[str, str], CalendarMeta] = {}
        self._resolved: dict[tuple[str, str], str] = {}
        self._calendars: dict[TargetKey, TargetCalendar] = {}
        self._caches: dict[tuple[str, str], TargetCache] = {}

    # ----- phase 1: ingest ---------------------------------------------
    async def _ingest(self, account: str, rules: list[Rule]) -> None:
        if account not in self._source_state:
            self._source_state[account], self._mirrors[account] = self._store.load_source(account)
        refs = sorted({r.source.calendar for r in rules})
        pull = await self._sources[account].pull(refs, self._source_state[account])
        unique = {p.calendar_id: p for p in pull.calendars.values()}
        self._store.save_source(account, pull.state, unique.values())
        self._source_state[account] = pull.state
        mirrors = self._mirrors[account]
        for ref, cal_pull in pull.calendars.items():
            self._resolved[(account, ref)] = cal_pull.calendar_id
            self._meta[(account, cal_pull.calendar_id)] = cal_pull.meta
        for cal_id, cal_pull in unique.items():
            apply_pull(mirrors.setdefault(cal_id, {}), cal_pull)

    # ----- phase 2: detect ---------------------------------------------
    async def _detect(self, key: TargetKey, rules: list[Rule], dry_run: bool) -> None:
        account, ref = key
        target = self._targets[account]
        cal = self._calendars.get(key) or self._store.load_target_calendar(account, ref)
        if cal is None:
            wants_create = any(r.target.create_if_missing for r in rules)
            try:
                cal = await target.ensure_calendar(ref, create=wants_create and not dry_run)
            except NotFoundError:
                if dry_run and wants_create:
                    raise NotFoundError(
                        f"target calendar {ref!r} does not exist yet; a real run creates it"
                    ) from None
                raise
            self._store.save_target_calendar(account, ref, cal)
        self._calendars[key] = cal
        cache_key = (account, cal.href)
        cache = self._caches.get(cache_key) or self._store.load_target_cache(account, cal.href)
        try:
            delta = await target.changes(cal, cache.cursor)
        except NotFoundError:
            # Calendar deleted or moved: rediscover on the next cycle.
            self._store.forget_target_calendar(account, ref)
            self._calendars.pop(key, None)
            raise
        need = apply_target_delta(cache, delta)
        for obj in await target.fetch(cal, need) if need else []:
            name = parse_resource_name(obj.href)
            if name is None:
                continue
            found = inspect(obj.data)
            cache.objects[obj.href] = CachedObject(
                etag=obj.etag,
                rule_tag=name[0],
                object_id=name[1],
                content_hash=found.content_hash if found else None,
                span=found.span if found else None,
            )
        cache.cursor = delta.cursor
        self._caches[cache_key] = cache
        self._store.save_target_cache(account, cal.href, cache)

    # ----- phase 3-5: plan, guard, push --------------------------------
    def _desired(self, rule: Rule) -> dict[str, Desired]:
        account = rule.source.account
        cal_id = self._resolved[(account, rule.source.calendar)]
        meta = self._meta.get((account, cal_id), CalendarMeta())
        series = self._sources[account].to_series(self._mirrors[account].get(cal_id, {}).values(), meta)
        desired: dict[str, Desired] = {}
        for item in series:
            shaped = apply_alarm_policy(item, rule.alarms)
            oid = object_id(rule.name, cal_id, item.source_id)
            desired[oid] = Desired(oid, shaped, series_hash(shaped), series_span(shaped))
        return desired

    def _cache_for(self, rule: Rule) -> tuple[TargetCalendar, TargetCache]:
        key = (rule.target.account, rule.target.calendar)
        cal = self._calendars[key]
        return cal, self._caches[(rule.target.account, cal.href)]

    async def _run_rule(self, rule: Rule, result: RuleResult, *, dry_run: bool, allow_deletes: bool) -> None:
        cal, cache = self._cache_for(rule)
        tag = rule_tag(rule.name)
        actual = {
            obj.object_id: Actual(obj.object_id, href, obj.etag, obj.content_hash, obj.span)
            for href, obj in cache.objects.items()
            if obj.rule_tag == tag
        }
        window = Window.around(self._clock(), rule.window.past, rule.window.future)
        result.plan = plan(self._desired(rule), actual, window)
        reason = None if allow_deletes else check_delete_guard(result.plan, len(actual), rule.delete_guard)
        if reason:
            result.status, result.error, result.error_kind = "blocked", reason, "guard"
            return
        if not dry_run and not result.plan.empty:
            await self._push(rule, cal, cache, result.plan, result)

    async def _push(
        self, rule: Rule, cal: TargetCalendar, cache: TargetCache, p: Plan, result: RuleResult
    ) -> None:
        target = self._targets[rule.target.account]
        tag = rule_tag(rule.name)
        now = self._clock()

        def body(want: Desired) -> bytes:
            return render(
                want.series,
                uid=uid_for(want.object_id),
                rule_name=rule.name,
                content_hash=want.content_hash,
                now=now,
            )

        async def current_etag(href: str) -> str | None:
            fetched = await target.fetch(cal, [href])
            return fetched[0].etag if fetched else None

        def remember(href: str, etag: str | None, want: Desired) -> None:
            cache.objects[href] = CachedObject(etag, tag, want.object_id, want.content_hash, want.span)

        async def create(want: Desired) -> None:
            href = target.href_for(cal, resource_name(rule.name, want.object_id))
            data = body(want)
            try:
                etag = await target.create(href, data)
            except ConflictError:  # exists already (e.g. local state was lost)
                etag = await target.update(href, await current_etag(href), data)
            remember(href, etag, want)
            result.created += 1

        async def update(want: Desired, have: Actual) -> None:
            data = body(want)
            try:
                etag = await target.update(have.href, have.etag, data)
            except ConflictError:
                etag = await target.update(have.href, await current_etag(have.href), data)
            remember(have.href, etag, want)
            result.updated += 1

        async def delete(have: Actual) -> None:
            try:
                await target.delete(have.href, have.etag)
            except ConflictError:
                await target.delete(have.href, await current_etag(have.href))
            cache.objects.pop(have.href, None)
            result.deleted += 1

        phases: list[list[Coroutine[Any, Any, None]]] = [
            [create(w) for w in p.create],
            [update(w, a) for w, a in p.update],
            [delete(a) for a in p.delete],
        ]
        for jobs in phases:
            for outcome in await asyncio.gather(*jobs, return_exceptions=True):
                if isinstance(outcome, Exception):
                    result.failed += 1
                    result.error = f"{type(outcome).__name__}: {outcome}"
                    if isinstance(outcome, AuthError):
                        result.error_kind = "auth"
                    log.warning("rule %s: write failed: %s", rule.name, outcome)
        if result.failed:
            done = result.created + result.updated + result.deleted
            result.status = "partial" if done else "failed"
            result.error_kind = result.error_kind or "other"

    # ----- the cycle ----------------------------------------------------
    async def cycle(
        self,
        *,
        rules: Sequence[str] | None = None,
        dry_run: bool = False,
        allow_deletes: bool = False,
    ) -> CycleResult:
        started = self._clock()
        mark = self._log.mark() if self._log else 0
        selected = [r for r in self._config.rules if rules is None or r.name in rules]
        results = {r.name: RuleResult(r.name) for r in selected}
        for target in self._targets.values():
            target.reset()

        by_source: dict[str, list[Rule]] = defaultdict(list)
        for rule in selected:
            by_source[rule.source.account].append(rule)
        outcomes = await asyncio.gather(
            *(self._ingest(a, rs) for a, rs in by_source.items()), return_exceptions=True
        )
        for (_, rs), outcome in zip(by_source.items(), outcomes, strict=True):
            if isinstance(outcome, Exception):
                for rule in rs:
                    results[rule.name].fail(outcome)

        by_target: dict[TargetKey, list[Rule]] = defaultdict(list)
        for rule in selected:
            if results[rule.name].status == "ok":
                by_target[(rule.target.account, rule.target.calendar)].append(rule)
        outcomes = await asyncio.gather(
            *(self._detect(k, rs, dry_run) for k, rs in by_target.items()), return_exceptions=True
        )
        for (_, rs), outcome in zip(by_target.items(), outcomes, strict=True):
            if isinstance(outcome, Exception):
                for rule in rs:
                    results[rule.name].fail(outcome)

        async def guarded(rule: Rule) -> None:
            try:
                await self._run_rule(rule, results[rule.name], dry_run=dry_run, allow_deletes=allow_deletes)
            except Exception as exc:  # isolate rules from each other
                log.exception("rule %s failed", rule.name)
                results[rule.name].fail(exc)

        await asyncio.gather(*(guarded(r) for r in selected if results[r.name].status == "ok"))

        if not dry_run:
            touched = {
                (r.target.account, self._calendars[(r.target.account, r.target.calendar)].href)
                for r in selected
                if (r.target.account, r.target.calendar) in self._calendars
            }
            for account, href in touched:
                self._store.save_target_cache(account, href, self._caches[(account, href)])

        finished = self._clock()
        requests = (self._log.mark() - mark) if self._log else 0
        if not dry_run:
            self._store.record_runs(
                [
                    RunRecord(
                        r.rule,
                        started,
                        finished,
                        r.status,
                        r.created,
                        r.updated,
                        r.deleted,
                        r.failed,
                        requests,
                        r.error,
                    )
                    for r in results.values()
                ]
            )
        return CycleResult(started, finished, results, requests)
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `100 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/harness.py tests/test_engine.py src/calsync/engine.py
git commit -m "feat(engine): sync cycle orchestration with rule isolation" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: 文件锁、组装与完整 CLI

单实例锁、按配置构建适配器，以及 `config check`、`calendars`、`auth`、`run` 命令和退出码。

**Files:**
- Replace: `tests/test_cli.py`
- Create: `src/calsync/lock.py`
- Create: `src/calsync/wiring.py`
- Replace: `src/calsync/cli.py`

**Interfaces:**
- Consumes:
  - Task 1–13 的全部公开接口。
- Produces:
  - `calsync.lock`：`AlreadyRunning(pid)`、`RunLock(path)`（上下文管理器，排他非阻塞 flock，写入 PID）
  - `calsync.wiring`：`default_state_dir(environ=os.environ) -> Path`（`$CALSYNC_STATE_DIR` 或 `~/.local/state/calsync`）、`Adapters(log, sources, targets, clients)`（`aclose()`）、`build_adapters(config, names, *, environ) -> Adapters`、`accounts_in_use(config, rule_names=None) -> set[str]`、`build_engine(config, adapters, store) -> Engine`、`state_paths(state_dir) -> (db_path, lock_path)`
  - `calsync.cli`：`main(argv=None, *, out=sys.stdout, stdin=sys.stdin) -> int`；退出码常量 `EXIT_OK=0`、`EXIT_FAILED=1`、`EXIT_CONFIG=2`、`EXIT_AUTH=3`、`EXIT_RUNNING=4`
  - 命令：`calsync [--config PATH] [--state-dir DIR] config check | calendars <account> | auth <account> [--refresh-token-stdin] | run [--rule NAME]... [--dry-run] [--allow-deletes]`

**注意：**
- 这一步会整体替换 Task 1 的 `src/calsync/cli.py` 和 `tests/test_cli.py`。
- 各命令在函数内部延迟导入重模块，`--version` 和 `config check` 不加载 httpx、icalendar。
- Google 的浏览器授权在计划 3 实现；这一版用 `auth <google账号> --refresh-token-stdin` 读入已有的 refresh token。

- [ ] **Step 1: 写失败的测试**

`tests/test_cli.py`：

````python
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
````

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_cli.py -v`

Expected: FAIL，收集阶段报错 `ImportError: cannot import name 'EXIT_AUTH' from 'calsync.cli'`

- [ ] **Step 3: 写实现**

`src/calsync/lock.py`：

````python
"""Single-instance lock: an exclusive, non-blocking flock that also records the PID."""

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import IO

from calsync.errors import CalsyncError


class AlreadyRunning(CalsyncError):
    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        super().__init__(f"another calsync process holds the lock (pid {pid or 'unknown'})")


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fh = self.path.open("a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            text = fh.read().strip()
            fh.close()
            raise AlreadyRunning(int(text) if text.isdigit() else None) from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
````

`src/calsync/wiring.py`：

````python
"""Build adapters and the engine from configuration. The only place that
turns account definitions into live clients."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from calsync.config import Account, Config
from calsync.credentials import read_secret
from calsync.engine import Engine
from calsync.errors import ConfigError
from calsync.http import RequestLog, make_client
from calsync.sources.base import Source
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import RefreshTokenProvider, load_client_secret, parse_refresh_token
from calsync.store import Store
from calsync.targets.base import Target
from calsync.targets.caldav import CalDavTarget


def default_state_dir(environ: Mapping[str, str] = os.environ) -> Path:
    explicit = environ.get("CALSYNC_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".local" / "state" / "calsync"


@dataclass
class Adapters:
    log: RequestLog
    sources: dict[str, Source] = field(default_factory=dict)
    targets: dict[str, Target] = field(default_factory=dict)
    clients: list[httpx.AsyncClient] = field(default_factory=list)

    async def aclose(self) -> None:
        for client in self.clients:
            await client.aclose()


def _google_source(
    config: Config, account: Account, client: httpx.AsyncClient, environ: Mapping[str, str]
) -> GoogleSource:
    if config.google_client_secret_file is None:
        raise ConfigError(["google.client_secret_file: required for google accounts"])
    client_id, client_secret = load_client_secret(config.google_client_secret_file)
    refresh = parse_refresh_token(read_secret(account.credential, environ=environ))
    token = RefreshTokenProvider(
        client, client_id=client_id, client_secret=client_secret, refresh_token=refresh
    )
    return GoogleSource(client, token)


def build_adapters(config: Config, names: set[str], *, environ: Mapping[str, str] = os.environ) -> Adapters:
    adapters = Adapters(RequestLog())
    google_client: httpx.AsyncClient | None = None
    for name in sorted(names):
        account = config.accounts[name]
        if account.provider == "google":
            if google_client is None:
                google_client = make_client(adapters.log)
                adapters.clients.append(google_client)
            adapters.sources[name] = _google_source(config, account, google_client, environ)
        else:
            assert account.server_url is not None and account.username is not None
            password = read_secret(account.credential, environ=environ)
            client = make_client(adapters.log, auth=httpx.BasicAuth(account.username, password))
            adapters.clients.append(client)
            adapters.targets[name] = CalDavTarget(
                client, account.server_url, max_concurrency=account.max_concurrency
            )
    return adapters


def accounts_in_use(config: Config, rule_names: list[str] | None = None) -> set[str]:
    rules = [r for r in config.rules if not rule_names or r.name in rule_names]
    return {r.source.account for r in rules} | {r.target.account for r in rules}


def build_engine(config: Config, adapters: Adapters, store: Store) -> Engine:
    return Engine(config, store, adapters.sources, adapters.targets, request_log=adapters.log)


def state_paths(state_dir: Path) -> tuple[Path, Path]:
    return state_dir / "state.db", state_dir / "run.lock"
````

`src/calsync/cli.py`：

````python
"""Command-line entry point. Heavy modules are imported inside each command
so ``calsync --version`` and ``config check`` start instantly."""

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from calsync import __version__

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_AUTH, EXIT_RUNNING = 0, 1, 2, 3, 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calsync", description="One-way calendar sync.")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument(
        "--config", type=Path, help="config file (default: $CALSYNC_CONFIG or ~/.config/calsync/config.toml)"
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="state directory (default: $CALSYNC_STATE_DIR or ~/.local/state/calsync)",
    )
    sub = parser.add_subparsers(dest="command")

    config = sub.add_parser("config", help="configuration commands")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("check", help="validate the configuration file")

    calendars = sub.add_parser("calendars", help="list calendars of an account")
    calendars.add_argument("account")

    auth = sub.add_parser("auth", help="store credentials for an account")
    auth.add_argument("account")
    auth.add_argument(
        "--refresh-token-stdin", action="store_true", help="google: read an existing refresh token from stdin"
    )

    run = sub.add_parser("run", help="run one sync cycle")
    run.add_argument("--rule", action="append", dest="rules", help="only this rule (repeatable)")
    run.add_argument("--dry-run", action="store_true", help="show the plan without writing")
    run.add_argument("--allow-deletes", action="store_true", help="bypass the mass-delete guard once")
    return parser


def _load(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from calsync.config import default_config_path, load_config

    return load_config(args.config or default_config_path())


def _state_dir(args: argparse.Namespace) -> Path:
    from calsync.wiring import default_state_dir

    return args.state_dir or default_state_dir()


def cmd_config_check(args: argparse.Namespace, out: TextIO) -> int:
    config = _load(args)
    out.write(f"ok: {len(config.accounts)} accounts, {len(config.rules)} rules\n")
    return EXIT_OK


async def _calendars(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.wiring import build_adapters

    config = _load(args)
    if args.account not in config.accounts:
        out.write(f"unknown account {args.account!r}\n")
        return EXIT_CONFIG
    adapters = build_adapters(config, {args.account})
    try:
        if args.account in adapters.sources:
            for cal in await adapters.sources[args.account].list_calendars():
                out.write(f"{cal.id}\t{cal.name}\n")
        else:
            for tcal in await adapters.targets[args.account].list_calendars():
                out.write(f"{tcal.href}\t{tcal.name}\t{tcal.mode}\n")
    finally:
        await adapters.aclose()
    return EXIT_OK


def _read_stdin_secret(prompt: str, stdin: TextIO) -> str:
    if stdin.isatty():
        return getpass.getpass(prompt)
    return stdin.read().strip()


async def _auth(args: argparse.Namespace, out: TextIO, stdin: TextIO) -> int:
    import json

    from calsync.credentials import write_secret
    from calsync.wiring import build_adapters

    config = _load(args)
    account = config.accounts.get(args.account)
    if account is None:
        out.write(f"unknown account {args.account!r}\n")
        return EXIT_CONFIG
    if account.provider == "google":
        if not args.refresh_token_stdin:
            out.write("google browser sign-in is not available yet; pass --refresh-token-stdin\n")
            return EXIT_CONFIG
        secret = json.dumps({"refresh_token": _read_stdin_secret("Refresh token: ", stdin)})
    else:
        secret = _read_stdin_secret("App-specific password: ", stdin)
    write_secret(account.credential, secret)
    adapters = build_adapters(config, {args.account})
    try:
        adapter = adapters.sources.get(args.account) or adapters.targets[args.account]
        found = await adapter.list_calendars()
    finally:
        await adapters.aclose()
    out.write(f"ok: credentials stored, {len(found)} calendars visible\n")
    return EXIT_OK


def _print_cycle(result, out: TextIO, verbose: bool) -> None:  # type: ignore[no-untyped-def]
    for name, r in result.rules.items():
        out.write(
            f"rule={name} status={r.status} created={r.created} updated={r.updated} "
            f"deleted={r.deleted} failed={r.failed}" + (f" error={r.error!r}" if r.error else "") + "\n"
        )
        if verbose and r.plan is not None:
            for want in r.plan.create:
                out.write(f"  + {want.series.title} @ {want.series.start.value.isoformat()}\n")
            for want, _ in r.plan.update:
                out.write(f"  ~ {want.series.title} @ {want.series.start.value.isoformat()}\n")
            for have in r.plan.delete:
                out.write(f"  - {have.href}\n")
    out.write(f"requests={result.requests}\n")


async def _run(args: argparse.Namespace, out: TextIO) -> int:
    from calsync.lock import RunLock
    from calsync.store import Store
    from calsync.wiring import accounts_in_use, build_adapters, build_engine, state_paths

    config = _load(args)
    unknown = [r for r in args.rules or [] if r not in {rule.name for rule in config.rules}]
    if unknown:
        out.write(f"unknown rule(s): {', '.join(unknown)}\n")
        return EXIT_CONFIG
    db_path, lock_path = state_paths(_state_dir(args))
    with RunLock(lock_path):
        adapters = build_adapters(config, accounts_in_use(config, args.rules))
        store = Store(db_path)
        try:
            engine = build_engine(config, adapters, store)
            result = await engine.cycle(
                rules=args.rules, dry_run=args.dry_run, allow_deletes=args.allow_deletes
            )
        finally:
            store.close()
            await adapters.aclose()
    _print_cycle(result, out, verbose=args.dry_run)
    if any(r.error_kind == "auth" for r in result.rules.values()):
        return EXIT_AUTH
    return EXIT_OK if result.ok else EXIT_FAILED


def main(argv: Sequence[str] | None = None, *, out: TextIO = sys.stdout, stdin: TextIO = sys.stdin) -> int:
    args = _parser().parse_args(argv)
    if args.version:
        out.write(f"calsync {__version__}\n")
        return EXIT_OK
    from calsync.errors import AuthError, ConfigError
    from calsync.lock import AlreadyRunning

    try:
        if args.command == "config":
            return cmd_config_check(args, out)
        if args.command == "calendars":
            return asyncio.run(_calendars(args, out))
        if args.command == "auth":
            return asyncio.run(_auth(args, out, stdin))
        if args.command == "run":
            return asyncio.run(_run(args, out))
    except ConfigError as exc:
        out.write("configuration error:\n" + "".join(f"  - {p}\n" for p in exc.problems))
        return EXIT_CONFIG
    except AuthError as exc:
        out.write(f"authentication error: {exc}\n")
        return EXIT_AUTH
    except AlreadyRunning as exc:
        out.write(f"{exc}\n")
        return EXIT_RUNNING
    _parser().print_help(out)
    return EXIT_CONFIG
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `106 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/test_cli.py src/calsync/lock.py src/calsync/wiring.py src/calsync/cli.py
git commit -m "feat(cli): run, auth, calendars and config check commands" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: 请求预算回归测试与文档

把规格 §8.1 的请求预算变成必须通过的测试，补全 README，完成整体验证。

**Files:**
- Create: `tests/e2e/test_request_budget.py`
- Replace: `README.md`

**Interfaces:**
- Consumes:
  - `tests/harness.py`、`tests/fakes/google.py`、`tests/factories.py`。
- Produces:
  - 无新接口。

**注意：**
- 这个测试守护已有行为，第一次运行就应该通过。如果失败，说明前面某个任务让同步变贵了：修实现，不要放宽断言。
- 预算：空闲周期 = 每个 Google 账号 1 次 `calendarList` + 每个源日历 1 次 `events.list` + 每个目标日历 1 次 `sync-collection`；刚写入后的那一轮也不能出现 `calendar-multiget`；改 k 个事件只多出 k 次 `put`。

- [ ] **Step 1: 写回归测试**

`tests/e2e/test_request_budget.py`：

````python
"""Spec §8.1 request budget, enforced. If this fails, a change made sync more expensive."""

from collections import Counter
from pathlib import Path

import pytest

from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, build, config_for, rule

pytestmark = [pytest.mark.anyio, pytest.mark.e2e]

WORK = "work@group.calendar.google.com"


def fake_with_two_calendars() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.add_calendar(WORK, "Work")
    for i in range(5):
        fake.put_event(
            GCAL,
            google_event(
                f"f{i}",
                f"F{i}",
                f"2026-10-2{5 + i % 3}T09:00:00+08:00",
                f"2026-10-2{5 + i % 3}T09:05:00+08:00",
            ),
        )
    fake.put_event(
        WORK,
        google_event(
            "w1",
            "Standup",
            "2026-10-26T09:00:00+08:00",
            "2026-10-26T09:15:00+08:00",
            recurrence=["RRULE:FREQ=DAILY;COUNT=10"],
        ),
    )
    return fake


async def test_idle_cycles_meet_budget(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    fake = fake_with_two_calendars()
    rules = [rule("family"), rule("work", source="Work", target="Work")]
    h = build(tmp_path, radicale_url, dav_user, fake, config_for(rules))
    await h.engine.cycle()  # cold start

    # The cycle right after our own writes must not download any body.
    mark = h.log.mark()
    after_write = await h.engine.cycle()
    assert all(r.plan.empty for r in after_write.rules.values())
    assert "calendar-multiget" not in [e.kind for e in h.log.since(mark)]

    for _ in range(2):
        mark = h.log.mark()
        idle = await h.engine.cycle()
        assert all(r.plan.empty for r in idle.rules.values())
        kinds = Counter(e.kind for e in h.log.since(mark))
        # 1 calendarList per Google account + 1 events.list per source calendar
        # + 1 change check per target calendar. Nothing else.
        assert kinds == Counter({"google-calendarlist": 1, "google-events": 2, "sync-collection": 2})
        assert idle.requests == 5


async def test_k_changes_cost_k_writes(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    fake = fake_with_two_calendars()
    h = build(tmp_path, radicale_url, dav_user, fake, config_for([rule("family")]))
    await h.engine.cycle()
    await h.engine.cycle()
    for i in range(3):
        fake.put_event(
            GCAL, google_event(f"f{i}", f"F{i} v2", "2026-10-27T10:00:00+08:00", "2026-10-27T10:05:00+08:00")
        )
    mark = h.log.mark()
    result = await h.engine.cycle()
    assert result.rules["family"].updated == 3
    kinds = Counter(e.kind for e in h.log.since(mark))
    assert kinds == Counter({"google-calendarlist": 1, "google-events": 1, "sync-collection": 1, "put": 3})
````

- [ ] **Step 2: 运行测试，确认通过**

Run: `uv run pytest tests/e2e/test_request_budget.py -v`

Expected: PASS。这些测试守护已有行为；如果失败，修实现，不要放宽断言。

- [ ] **Step 3: 更新文档**

`README.md`：

````markdown
# calsync

One-way calendar sync engine. It keeps one or more target calendars in step
with a source calendar, continuously and cheaply.

First release: **Google Calendar → CalDAV** (iCloud, iCloud China, Fastmail,
Nextcloud, Radicale, …).

## Why

Google calendars can only be shared with Google accounts, iCloud calendars
only with Apple IDs. calsync bridges them one way: write events wherever you
like (for example through an AI assistant connected to Google Calendar) and
have them appear, with their alerts, in an iCloud calendar.

## Design in one paragraph

calsync keeps a local mirror of each source calendar, updated with Google's
incremental `syncToken`, and detects target changes with CalDAV
`sync-collection` (falling back to `getctag`, then a plain listing). Every
resource it writes has a deterministic name (`calsync-<rule>-<id>.ics`), so
ownership is visible from a listing alone and calsync never touches events it
did not create. All local state is a rebuildable cache. An idle cycle costs one
`calendarList` request per Google account, one `events.list` request per source
calendar and one change check per target calendar, and the test suite enforces
that budget. See `docs/superpowers/specs/2026-10-23-calsync-design.md`.

## Install

```sh
uv tool install git+https://github.com/Aryous/calsync
```

## Configure

`~/.config/calsync/config.toml` (override with `CALSYNC_CONFIG`):

```toml
[google]
client_secret_file = "~/.config/calsync/google-client.json"

[accounts.me-google]
provider = "google"
credential = { backend = "keychain", ref = "calsync/me-google" }

[accounts.family-icloud]
provider = "caldav"
server_url = "https://caldav.icloud.com/"   # China mainland: https://caldav.icloud.com.cn/
username = "family@example.com"
credential = { backend = "keychain", ref = "calsync/family-icloud" }

[[rules]]
name = "family"
source = { account = "me-google", calendar = "Family" }
target = { account = "family-icloud", calendar = "Family", create_if_missing = true }
window = { past = "7d", future = "180d" }
alarms = "copy"          # or "strip", or ["-5m", "0m"]
```

Credential backends: `keychain` (macOS keychain via keyring), `env`
(environment variable), `file` (path to a file with mode 0600).

## Use

```sh
calsync config check
calsync auth family-icloud                      # reads an app-specific password from stdin
calsync auth me-google --refresh-token-stdin    # until the browser sign-in lands
calsync calendars family-icloud
calsync run --dry-run                           # show the plan
calsync run                                     # one sync cycle
```

`calsync run --allow-deletes` lets one cycle through the mass-delete guard.

Exit codes: 0 ok, 1 a rule failed, 2 configuration error, 3 authentication
error, 4 another calsync process is running.

## Develop

```sh
uv sync
uv run pytest            # unit tests plus end-to-end tests against a local Radicale
uv run ruff check src tests && uv run pyright
```

Test data is synthetic. Never commit real calendar content or credentials.
````

- [ ] **Step 4: 运行全部测试和质量检查**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`

Expected: pytest 输出 `108 passed`；ruff 输出 `All checks passed!`；pyright 输出 `0 errors, 0 warnings, 0 informations`。

- [ ] **Step 5: 提交**

```bash
git add tests/e2e/test_request_budget.py README.md
git commit -m "test: enforce the idle-cycle request budget; document usage" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


## 完成标准

全部 15 个任务完成后：

- [ ] `uv run pytest` 输出 `108 passed`。
- [ ] `uv run ruff check src tests`、`uv run ruff format --check src tests`、`uv run pyright` 全部 0 问题。
- [ ] 推送到 `origin/main` 后，GitHub Actions 的 `ci` 工作流通过。
- [ ] 调用 superpowers:requesting-code-review 做一次完整审查，按 superpowers:receiving-code-review 处理意见。

## 规格覆盖对照

| 规格章节 | 落在哪个任务 |
| --- | --- |
| §2 核心概念（单向、托管资源、本地即缓存） | 6、11、12、13 |
| §4 配置与校验 | 3 |
| §5 统一事件模型、哈希、`RENDER_VERSION` | 2 |
| §6.2 Google 源（增量、默认提醒、重复事件、HTML、不同步参与者） | 5、9 |
| §6.3 CalDAV 目标（命名、标记、降级阶梯、条件写入、限流、跨主机发现） | 6、10、11 |
| §7 同步周期（拉取、检测、规划、防误删、推送、记录、规则隔离、dry-run） | 7、13 |
| §8 性能（请求预算、增量、字段裁剪、gzip、HTTP/2、度量） | 8、9、11、15 |
| §9 本地状态 | 12 |
| §10.2 单次运行与锁 | 14 |
| §12 命令行（`config check`、`calendars`、`auth`、`run`） | 14 |
| §14 测试（单元、Radicale 端到端、请求预算、合成数据） | 全部 |

不在本计划内、留给后续计划：

- §10.1 常驻进程、§10.3 `trigger`、§10.4 launchd / Docker、§11 告警、§12 的 `daemon` / `trigger` / `status` / `service`、Google 浏览器授权：计划 3。
- §13 真实环境验证：计划 2。
