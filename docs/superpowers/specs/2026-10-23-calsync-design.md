# calsync 设计规格

- 日期：2026-10-23
- 状态：设计已确认，待实现
- 作者：Aryous 与 Claude

## 1. 背景与目标

用户在 ChatGPT iOS 客户端里口述生活安排，ChatGPT 通过 Google MCP 写入 Google 日历。其中一部分日历需要出现在家人的 iPhone 上，并且到点提醒。Google 日历只能共享给 Google 账号，iCloud 日历只能共享给 Apple ID，两边没有可写的互通方式。

calsync 是一个通用的单向日历同步工具：定时从源日历拉取事件，写入一个或多个目标日历。账号、日历和同步策略全部来自配置，代码中不包含任何具体账号。

### 成功标准

1. 在 Google 日历中新建、修改、删除事件后，一个同步周期内（默认 5 分钟）目标 iCloud 日历出现相同变化。
2. 事件提醒按规则策略写入目标日历，并在目标设备上按时触发（具体写法见 §9 实验结论）。
3. 同步不会修改或删除目标日历中非 calsync 创建的事件。
4. 同步异常持续时，通过 Bark 推送告警；恢复后推送恢复通知。
5. 同一程序可以在 Mac mini（launchd）和 Docker 中运行。

### 非目标（第一版不做）

- 双向同步。
- Google 增量同步（syncToken）与推送通知（webhook）。
- 网页界面。
- ICS 订阅源、CalDAV 源、Google 目标等其他适配器。
- 内容隐藏（只同步忙碌状态等）。
- 邮件类提醒。

## 2. 核心概念

| 概念 | 含义 |
| --- | --- |
| 账号（Account） | 一个日历服务账号：类型（`google` / `caldav`）、服务器地址、用户名、凭证引用 |
| 日历（Calendar） | 某个账号下的一个日历，用 ID 或名称引用 |
| 规则（Rule） | 一个源日历 → 一个目标日历，附带时间范围、提醒策略等选项 |
| 系列（Series） | 统一事件模型的基本单位。单次事件是只有一次的系列；重复事件包含重复规则和单次改动 |
| 托管事件 | 目标日历中由 calsync 写入、带有 calsync 标记的事件 |

同步方向始终是单向：源 → 目标。在目标日历中手动修改托管事件，会在下一轮被源的内容覆盖。若需要 A、B 互通，配置 A→B 和 B→A 两条规则；引擎跳过带 calsync 标记的源事件，避免循环复制（第一版没有 CalDAV 源，此逻辑随 CalDAV 源适配器一起实现）。

## 3. 模块结构

```
calsync/
  config.py      读取并校验 TOML 配置
  secrets.py     凭证后端：keychain / env / file
  model.py       统一事件模型、规范化、内容哈希
  sources/
    base.py      源适配器接口
    google.py    Google Calendar API
  targets/
    base.py      目标适配器接口
    caldav.py    CalDAV（iCloud、iCloud 中国区、通用）
  render.py      统一模型 → iCalendar（.ics）
  engine.py      拉取 → 比对 → 安全检查 → 执行
  state.py       SQLite：运行记录、连续失败计数
  notify.py      Bark / 通用 webhook
  lock.py        文件锁，防止并发运行
  cli.py         命令行入口
```

技术栈：Python 3.12+，uv 管理依赖。主要依赖：`google-api-python-client`、`google-auth-oauthlib`、`caldav`、`icalendar`、`keyring`、`httpx`（通知）。

每个模块只依赖接口：引擎只认识 `Source` / `Target` 接口和统一事件模型，不感知具体服务。

## 4. 配置

默认路径 `~/.config/calsync/config.toml`，可由环境变量 `CALSYNC_CONFIG` 覆盖。

```toml
[google]
client_secret_file = "~/.config/calsync/google-client.json"

[notify]
bark_url = "https://api.day.app/<key>"   # 可选；或 webhook_url
failure_threshold = 3                    # 连续失败多少轮后告警

[defaults]
interval = "5m"

[accounts.me-google]
provider = "google"
credential = { backend = "keychain", ref = "calsync/me-google" }

[accounts.family-icloud]
provider = "caldav"
server_url = "https://caldav.icloud.com/"   # 中国区：https://caldav.icloud.com.cn/
username = "family@example.com"
credential = { backend = "keychain", ref = "calsync/family-icloud" }

[[rules]]
name = "family-schedule"
source = { account = "me-google", calendar = "<日历 ID 或名称>" }
target = { account = "family-icloud", calendar = "家庭日程", create_if_missing = true }
window = { past = "7d", future = "180d" }
alarms = "copy"
delete_guard = { max_ratio = 0.5, max_count = 10 }
```

### 字段规则

- `server_url` 是账号字段，不是代码常量。
- `credential.backend`：
  - `keychain`：通过 `keyring` 存取，`ref` 为条目名。
  - `env`：`ref` 为环境变量名。
  - `file`：`ref` 为文件路径，文件权限必须是 `0600`，否则拒绝读取。
- Google 账号的凭证是 OAuth refresh token（JSON）；CalDAV 账号的凭证是 App 专用密码。
- `calendar` 可以填 ID 或名称。按名称匹配到多个或零个时报错，不猜测。推荐使用 `calsync calendars` 列出的 ID。
- `alarms`：
  - `"copy"`：照搬源事件的提醒。
  - `"strip"`：不写任何提醒。
  - 偏移数组，例如 `["-5m", "0m"]`：所有事件统一使用这组提醒。
- `rules[].name` 在配置内唯一，参与生成目标 UID，**修改规则名等于重建该规则的全部托管事件**。
- 配置加载时校验：引用的账号必须存在、规则名唯一、时长格式合法。校验失败时不运行任何规则。

## 5. 统一事件模型

```python
@dataclass(frozen=True)
class Alarm:
    offset: timedelta          # 相对开始时间，负数表示提前

@dataclass(frozen=True)
class Occurrence:              # 单次改动的实例
    original_start: When       # 对应 RECURRENCE-ID
    start: When
    end: When
    title: str | None          # None 表示沿用主事件
    description: str | None
    location: str | None
    alarms: tuple[Alarm, ...] | None

@dataclass(frozen=True)
class Series:
    source_id: str             # 源系统内的事件 ID（重复事件为主事件 ID）
    title: str
    description: str
    location: str
    start: When                # When = 带时区的 datetime + 原始 IANA 时区名，或全天 date
    end: When
    recurrence: tuple[str, ...]        # 原样保留的 RRULE / RDATE 行
    exdates: tuple[When, ...]
    overrides: tuple[Occurrence, ...]
    alarms: tuple[Alarm, ...]
    transparent: bool          # 是否显示为空闲
```

- 规范化：字符串去除首尾空白；集合字段排序；时间统一到原始时区表达。
- 内容哈希：对规范化后的字段做 SHA-256，取前 16 位十六进制。提醒策略在渲染前应用，所以策略变化会改变哈希，触发更新。

## 6. 适配器

### 6.1 接口

```python
class Source(Protocol):
    def list_calendars(self) -> list[CalendarInfo]: ...
    def fetch(self, calendar_id: str, window: Window) -> list[Series]: ...

class Target(Protocol):
    def list_calendars(self) -> list[CalendarInfo]: ...
    def ensure_calendar(self, ref: CalendarRef, create: bool) -> str: ...
    def list_managed(self, calendar_id: str, rule: str) -> dict[str, Remote]: ...
    def create(self, calendar_id: str, uid: str, ics: bytes) -> None: ...
    def update(self, calendar_id: str, remote: Remote, ics: bytes) -> None: ...
    def delete(self, calendar_id: str, remote: Remote) -> None: ...
```

`Remote` 包含 `href`、`etag`、`content_hash`，以及 `span_start` / `span_end`：该资源覆盖的时间跨度，即第一次发生的开始到最后一次发生的结束。无结束的重复规则 `span_end` 记为无穷大。跨度从资源的 `DTSTART`、`DTEND` 和 RRULE 的 `UNTIL` / `COUNT` 计算。

适配器只抛出统一的错误类型：`AuthError`（凭证失效，不重试）、`TransientError`（网络、429、5xx，可重试）、`ConflictError`（412 版本冲突）、`ConfigError`。

### 6.2 Google 源

- 授权：用户自建 Google Cloud 项目和桌面应用 OAuth 客户端，scope 为 `https://www.googleapis.com/auth/calendar.readonly`。
- **OAuth 同意屏幕必须发布为 "In production"**。Google 官方文档说明：外部用户类型、处于 "Testing" 状态的项目，refresh token 7 天后失效。个人使用无需审核，授权时会出现"未经验证的应用"提示。
- 每轮完整拉取：`events.list(calendarId, timeMin, timeMax, singleEvents=false, showDeleted=false)`，自动翻页。
- 提醒映射：
  - `reminders.useDefault = true` 时，读取 `calendarList.get(calendarId).defaultReminders`。
  - 只映射 `method = "popup"`；`email` 忽略。
  - `minutes = N` 映射为 `offset = -N 分钟`。
- 重复事件：
  - 主事件的 `recurrence` 原样进入 `Series.recurrence`（其中 `EXDATE` 解析到 `exdates`）。
  - 带 `recurringEventId` 的事件按 `originalStartTime` 归入对应主事件的 `overrides`。
  - `status = "cancelled"` 的实例转为 `exdates`。
  - 主事件不在窗口内、但有实例落在窗口内时，按 `recurringEventId` 额外 `events.get` 主事件。
- 全天事件：`start.date` / `end.date` 映射为 date 类型的 `When`。
- 网络：HTTP 客户端遵循 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量。

### 6.3 CalDAV 目标

- 发现：从 `server_url` 解析 principal 和 calendar-home-set，列出日历（基于 `caldav` 库）。
- `ensure_calendar`：按 ID 或名称定位；不存在且 `create_if_missing = true` 时用 MKCALENDAR 创建。
- 每个系列写成一个 `.ics` 资源，资源路径 `<日历>/<uid>.ics`，内容包含：
  - `VTIMEZONE`（由 `icalendar` 生成）。
  - 主 `VEVENT`，以及每个单次改动对应一个带 `RECURRENCE-ID` 的 `VEVENT`。
  - 每个提醒对应一个 `VALARM`（`ACTION:DISPLAY`，`TRIGGER:-PT5M` 形式）。
- 托管标记（写在每个 `VEVENT` 上）：
  - `UID:<sha256(rule + "\0" + source_calendar_id + "\0" + source_id)[:24]>@calsync`
  - `X-CALSYNC-RULE:<规则名>`
  - `X-CALSYNC-HASH:<内容哈希>`
- `list_managed`：REPORT `calendar-query` 取出全部 VEVENT 资源，只保留 `X-CALSYNC-RULE` 等于当前规则、且 UID 以 `@calsync` 结尾的资源。
- 写入：
  - 新建：PUT 并带 `If-None-Match: *`。若返回 412（资源已存在），重新读取后转为更新。
  - 更新：PUT 并带 `If-Match: <etag>`。若返回 412，重新读取最新 etag，重试一次。
  - 删除：DELETE 并带 `If-Match: <etag>`。404 视为已删除。
- 限流：每个账号串行请求；429 / 503 按 `Retry-After` 或指数退避（1s、2s、4s，最多 3 次）重试，仍失败则抛出 `TransientError`。

## 7. 同步流程

每条规则每轮执行：

1. **拉源**：`source.fetch(calendar, window)` → 应用提醒策略 → 渲染 → 计算哈希 → 得到 `desired: dict[uid, (series, ics, hash)]`。
2. **读目标**：`target.list_managed(calendar, rule)` → `actual: dict[uid, Remote]`。
3. **比对**：
   - `desired` 有、`actual` 无 → 新建。
   - 两边都有、哈希不同 → 更新。
   - `actual` 有、`desired` 无，且 `[span_start, span_end]` 与窗口有交集 → 删除。
   - `actual` 有、`desired` 无，且跨度与窗口没有交集 → 不处理（例如早已结束的旧事件）。
   - 用跨度而不是开始时间判断，是为了覆盖"系列开始于窗口之前、但仍在持续"的情况：这类系列在源头被删除后，目标副本必须被清理。
4. **安全检查**：计划删除数 `> max_count`，或 `> max_ratio × len(actual)`（`len(actual) > 0` 时），则整轮不执行，记为失败并立即告警，告警中列出计划删除的事件标题。
5. **执行**：先新建，再更新，最后删除。单个事件失败时记录错误，继续处理其余事件；本轮最终状态为"部分失败"。
6. **记录**：写入 `state.py`（新建、更新、删除、失败的数量，耗时，错误信息）。

`--dry-run` 执行第 1–4 步，打印计划，不写入，也不更新失败计数。

规则之间相互隔离：一条规则抛出异常，只影响它自己的运行记录。

## 8. 告警、状态与运行

### 8.1 告警

| 情况 | 行为 |
| --- | --- |
| `AuthError` | 立即告警："账号 X 授权失效，请运行 `calsync auth X`"。同一账号在恢复前只告警一次 |
| 防误删拦截 | 立即告警，附计划删除的事件列表 |
| 其他失败 | 同一规则连续失败达到 `failure_threshold` 轮时告警一次 |
| 恢复 | 曾告警过的规则首次成功时推送"已恢复" |

告警发送失败只写日志，不影响同步结果。

### 8.2 状态

SQLite 文件默认在 `~/.local/state/calsync/state.db`（Docker 中为数据卷），表：

- `runs(rule, started_at, finished_at, status, created, updated, deleted, failed, error)`
- `alerts(key, state, last_sent_at)`：去重告警。

状态库只服务于告警和 `calsync status`。删除它不会影响同步正确性。

### 8.3 并发

`calsync run` 启动时获取 `~/.local/state/calsync/run.lock` 的非阻塞文件锁。获取失败时直接退出（退出码 0，写一行日志）。

### 8.4 命令行

```
calsync auth <account>                 Google：本地浏览器 OAuth；CalDAV：从标准输入读取密码。写入凭证后端并验证可用
calsync calendars <account>            列出日历的 ID 和名称
calsync run [--rule NAME] [--dry-run]  执行一轮
calsync daemon [--interval 5m]         常驻循环执行 run
calsync status                         每条规则最近一次运行结果
calsync config check                   只校验配置
```

退出码：0 成功；1 至少一条规则失败；2 配置错误；3 授权错误。

### 8.5 部署

- **Mac mini**：`uv tool install` 安装；`~/Library/LaunchAgents/com.aryous.calsync.plist` 每 300 秒执行 `calsync run`，并设置 `HTTPS_PROXY` 指向本机 Surge；日志写入 `~/Library/Logs/calsync/`，按天轮转，保留 14 天。
- **Docker**：`Dockerfile` 基于 `python:3.12-slim`，入口 `calsync daemon`；配置、凭证文件、状态目录通过挂载传入；凭证后端使用 `env` 或 `file`。

## 9. 待验证事项（实现第一步）

以下事项决定默认写法，必须在主体实现前用真实 iCloud 测试日历验证，结论回写本节：

1. **自定义属性**：iCloud 是否保留 `X-CALSYNC-RULE` / `X-CALSYNC-HASH`。
   - 若不保留，托管标记退回到只用 UID 后缀 `@calsync` 加规则前缀，内容哈希改存 SQLite（此时 SQLite 成为必要状态，§8.2 的说法需修改）。
2. **共享日历的提醒**：
   - 背景：Apple 开源的 CalendarServer 把 `VALARM` 当作每个用户自己的数据（`twistedcaldav/datafilters/peruserdata.py`）。iCloud 线上是否同样处理尚未确认。
   - 实验 A：账号 1 写入带 `VALARM` 的事件到日历 C，C 共享给账号 2，观察账号 2 的设备是否提醒。
   - 实验 B：直接用账号 2 的凭证写入账号 2 自己的日历，观察提醒。
   - 结论决定"给家人同步"的推荐配置：目标写在自己账号再共享，还是直接写在家人账号。
3. **iCloud 中国区**：`caldav.icloud.com.cn` 的发现和写入流程是否与国际区一致（有中国区账号时验证）。

## 10. 测试

1. **单元测试**（pytest，无网络）：
   - 模型规范化与哈希稳定性。
   - 渲染：时区、RRULE、EXDATE、单次改动、全天事件、提醒策略三种模式。
   - Google 响应 → 统一模型：用真实 API 返回的 JSON 作为测试样本（去除个人信息后存入 `tests/fixtures/`）。
   - 比对逻辑：新建、更新、删除、窗口外不处理、防误删阈值。
   - 配置校验与凭证后端（`file` 后端的权限检查）。
2. **端到端测试**：本地启动 Radicale，覆盖"首次同步 → 源修改 → 源删除 → 手动建的非托管事件不受影响 → 防误删拦截"。Google 源用基于 fixtures 的假实现替代。
3. **真实验证**：§9 的实验，以及在 Mac mini 上用一条测试规则连续运行 24 小时，确认无重复写入、无误删、告警正常。
