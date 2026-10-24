# calsync 设计规格

- 日期：2026-10-23（v2 修订：2026-10-24）
- 状态：v2 待确认
- 作者：Aryous 与 Claude

> v2 相对 v1 的变化：
> - 引入源日历本地镜像和 Google 增量同步。
> - 目标侧采用"变化检测降级阶梯"。
> - Google 和 CalDAV 客户端改为基于 httpx 自写。
> - 常驻进程成为推荐运行方式，并新增"性能设计"一节，把每轮请求数写成可测试的预算。
> - 去除了背景中的个人信息，为开源做准备。

## 1. 背景与目标

一个常见场景：用户通过 AI 助手（例如 ChatGPT 的 Google 日历连接器）口述安排，事件写入 Google 日历；其中某些日历需要出现在家人的 iPhone 上，并且到点提醒。Google 日历只能共享给 Google 账号，iCloud 日历只能共享给 Apple ID，两者之间没有可写的互通方式。

calsync 是一个通用的单向日历同步引擎：持续把源日历的变化同步到一个或多个目标日历。账号、日历和同步策略全部来自配置，代码中不包含任何具体账号。

### 成功标准

1. 源日历中新建、修改、删除事件后，一个同步周期内（默认 60 秒）目标日历出现相同变化。
2. 事件提醒按规则策略写入目标日历，并在目标设备上按时触发（写法以 §13 实验结论为准）。
3. 同步不会修改或删除目标日历中非 calsync 创建的事件。
4. 没有变化的同步周期满足 §8.1 的请求预算，并由自动化测试保证。
5. 同步异常持续时通过 Bark 或 webhook 告警，恢复后通知。
6. 同一程序可以在 macOS（launchd）和 Docker 中运行。

### 非目标（第一版不做）

- 双向同步。
- Google 推送通知（webhook）：架构上预留触发入口，见 §10.3。
- 网页界面。
- ICS 订阅源、CalDAV 源、Google 目标等其他适配器。
- 内容隐藏（只同步忙碌状态等）。
- 邮件类提醒。

## 2. 核心概念

| 概念 | 含义 |
| --- | --- |
| 账号（Account） | 一个日历服务账号：类型（`google` / `caldav`）、服务器地址、用户名、凭证引用 |
| 日历（Calendar） | 某个账号下的一个日历，用 ID 或名称引用 |
| 规则（Rule） | 一个源日历 → 一个目标日历，附带时间窗口、提醒策略等选项 |
| 系列（Series） | 统一事件模型的基本单位。单次事件是只有一次的系列；重复事件包含重复规则和单次改动 |
| 源镜像（Source mirror） | 本地保存的源日历完整副本，靠增量同步保持最新。它是缓存，丢失后从源头全量重建 |
| 目标缓存（Target cache） | 本地记录的"目标日历里有哪些 calsync 托管资源、版本号、内容哈希"。它也是缓存，丢失后从目标重建 |
| 托管资源 | 目标日历中由 calsync 写入、带 calsync 标记的事件资源 |

**真相在两端，本地只是缓存**：源事件的真相在源服务，托管关系的真相在目标服务（资源名、UID 和 `X-CALSYNC-*` 标记）。删除整个本地状态目录，下一轮会走一次冷启动（§8.1），结果与删除前一致。

同步方向始终是单向：源 → 目标。在目标日历中手动修改托管事件，会在下一轮被源的内容覆盖。若需要 A、B 互通，配置 A→B 和 B→A 两条规则；引擎会跳过带 calsync 标记的源事件，避免循环复制。第一版没有 CalDAV 源，所以这个跳过逻辑随 CalDAV 源适配器一起实现。

## 3. 架构总览

```
            ┌──────────── 一个同步周期 ────────────┐
 Google ──► │ 拉取：每个源日历一次增量请求          │
            │   └─► 源镜像（内存 + SQLite）          │
            │ 规划：每条规则在内存中计算             │
            │   应有集合（镜像 ∩ 时间窗口）          │
            │   vs 现有集合（目标缓存）              │
            │   → 新建 / 更新 / 删除 计划            │
 CalDAV ◄─► │ 检测：每个目标日历一次变化查询         │
            │   └─► 目标缓存                          │
            │ 推送：按账号限流执行计划               │
            └────────────────────────────────────────┘
```

模块：

```
calsync/
  config.py        读取并校验 TOML 配置
  secrets.py       凭证后端：keychain / env / file
  model.py         统一事件模型、规范化、内容哈希
  http.py          共享 httpx 客户端工厂：HTTP/2、gzip、代理、超时、请求计数
  sources/
    base.py        源适配器接口
    google.py      Google Calendar REST 客户端 + 映射到统一模型
    google_auth.py OAuth（loopback + PKCE），只被 auth 命令和令牌刷新使用
  targets/
    base.py        目标适配器接口
    dav.py         最小 WebDAV/CalDAV 协议层：PROPFIND、REPORT、PUT、DELETE、XML 解析
    caldav.py      CalDAV 目标适配器：发现、变化检测阶梯、托管资源识别
  render.py        统一模型 → iCalendar
  mirror.py        源镜像：应用增量、按系列分组
  planner.py       纯函数：(应有集合, 现有集合, 窗口, 策略) → 计划
  engine.py        一个同步周期的编排：拉取、检测、规划、安全检查、推送
  store.py         SQLite：镜像、目标缓存、游标、运行记录、告警状态
  notify.py        Bark / 通用 webhook
  runtime.py       常驻进程：调度、信号、锁、优雅退出
  cli.py           命令行入口（按命令延迟导入模块）
```

依赖保持最少：

- 运行时：`httpx[http2]`、`icalendar`、`keyring`（只在 keychain 后端导入）。
- 标准库：`sqlite3`、`tomllib`、`xml.etree`、`html.parser`、`hashlib`。
- 不使用 `google-api-python-client` 和 `caldav` 库，理由：
  - 前者导入慢，并且依赖运行时下载的接口描述文档。
  - 后者无法精确控制 `If-Match` / `If-None-Match`、`sync-collection`、`calendar-multiget` 和请求数。
  - 需要的协议面很小：Google 只用三个接口，CalDAV 只用五种请求。

技术栈：Python 3.12+，uv 管理依赖，asyncio 并发。

## 4. 配置

默认路径 `~/.config/calsync/config.toml`，可由环境变量 `CALSYNC_CONFIG` 覆盖。

```toml
[google]
client_secret_file = "~/.config/calsync/google-client.json"

[notify]
bark_url = "https://api.day.app/<key>"   # 可选；或 webhook_url
failure_threshold = 3                    # 连续失败多少轮后告警

[runtime]
interval = "60s"                          # 同步周期
jitter = 0.1                              # 周期随机抖动比例，避免整点扎堆

[accounts.me-google]
provider = "google"
credential = { backend = "keychain", ref = "calsync/me-google" }

[accounts.family-icloud]
provider = "caldav"
server_url = "https://caldav.icloud.com/"   # 中国区：https://caldav.icloud.com.cn/
username = "family@example.com"
credential = { backend = "keychain", ref = "calsync/family-icloud" }
max_concurrency = 2                         # 该账号同时进行的写请求数

[[rules]]
name = "family-schedule"
source = { account = "me-google", calendar = "家庭日程" }
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
- `calendar` 填 ID 或名称。名称匹配到多个或零个时报错，不猜测。推荐使用 `calsync calendars` 列出的 ID。
- `alarms`：
  - `"copy"`：照搬源事件的提醒。
  - `"strip"`：不写任何提醒。
  - 偏移数组，例如 `["-5m", "0m"]`：所有事件统一使用这组提醒。
- `rules[].name` 在配置内唯一。它参与生成资源名和 UID，**修改规则名等于重建该规则的全部托管资源**。
- 配置加载时校验：账号引用存在、源账号类型为 `google`、目标账号类型为 `caldav`、规则名唯一、时长格式合法。校验失败时不运行任何规则。
- "同一个共享日历被不同账号的规则写入"无法在配置层静态识别（不同账号下的同名日历未必是同一个），只在文档中警告，见 §6.3。

## 5. 统一事件模型

```python
@dataclass(frozen=True, slots=True)
class Alarm:
    offset: timedelta          # 相对开始时间，负数表示提前

@dataclass(frozen=True, slots=True)
class Occurrence:              # 单次改动的实例
    original_start: When       # 对应 RECURRENCE-ID
    start: When
    end: When
    title: str                 # 字段完整：按 RFC 5545，带 RECURRENCE-ID 的组件整体替换该实例，
    description: str           # 不继承主事件的标题、描述、地点和提醒
    location: str
    alarms: tuple[Alarm, ...]

@dataclass(frozen=True, slots=True)
class Series:
    source_id: str             # 源系统内的事件 ID（重复事件为主事件 ID）
    title: str
    description: str           # 纯文本
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
- **内容哈希**：在规则策略（提醒策略等）应用之后，对规范化后的字段做 `blake2b(digest_size=16)`，输入为稳定的 JSON 序列化（`sort_keys`，紧凑分隔符）。
- 哈希输入额外包含常量 `RENDER_VERSION`。渲染逻辑变化时递增它，所有托管资源会在下一轮被重写一次。
- 哈希在模型层计算，不依赖渲染结果，所以只有需要写入的系列才会被渲染成 `.ics`。

## 6. 适配器

### 6.1 接口

```python
class Source(Protocol):
    async def list_calendars(self) -> list[CalendarInfo]: ...
    async def pull(self, calendar_id: str, cursor: SourceCursor | None) -> SourceDelta: ...
    # SourceDelta = 变化的原始事件 + 是否为全量 + 新游标 + 日历默认提醒（若变化）

class Target(Protocol):
    async def list_calendars(self) -> list[CalendarInfo]: ...
    async def ensure_calendar(self, ref: CalendarRef, create: bool) -> TargetCalendar: ...
    async def changes(self, cal: TargetCalendar, cursor: TargetCursor | None) -> TargetDelta: ...
    # TargetDelta = 变化的托管资源（href、etag）+ 被删除的 href + 是否为全量 + 新游标
    async def fetch(self, cal: TargetCalendar, hrefs: list[str]) -> list[RemoteObject]: ...
    async def create(self, cal: TargetCalendar, href: str, ics: bytes) -> str | None: ...  # 返回 etag
    async def update(self, cal: TargetCalendar, href: str, etag: str, ics: bytes) -> str | None: ...
    async def delete(self, cal: TargetCalendar, href: str, etag: str) -> None: ...
```

`RemoteObject` 包含 `href`、`etag`、`uid`、`rule`、`content_hash`、`span_start`、`span_end`。

**跨度**是资源覆盖的时间范围：从第一次发生的开始，到最后一次发生的结束。它从 `DTSTART`、`DTEND` 和 RRULE 的 `UNTIL` / `COUNT` 计算；没有结束的重复规则，`span_end` 记为无穷大。

适配器只抛出统一的错误类型：

- `AuthError`：凭证失效，不重试。
- `TransientError`：网络错误、429、5xx，可重试。
- `ConflictError`：412 版本冲突。
- `CursorExpired`：游标失效，需要全量。
- `ConfigError`：配置问题。

### 6.2 Google 源

**授权**

- 用户自建 Google Cloud 项目和"桌面应用"类型的 OAuth 客户端，scope 为 `https://www.googleapis.com/auth/calendar.readonly`。
- `calsync auth` 走 loopback + PKCE：打开浏览器，在本机临时端口接收回调，把 refresh token 写入凭证后端。
- **OAuth 同意屏幕必须发布为 "In production"**。Google 官方文档说明：外部用户类型、处于 "Testing" 状态的项目，refresh token 7 天后失效。个人使用无需审核，授权时会出现"未经验证的应用"提示。
- access token 只保存在进程内存中，过期前 5 分钟刷新，不落盘。

**增量同步**

所有 `events.list` 调用使用同一组固定参数，初始全量和后续增量完全一致。Google 文档要求增量请求的其他参数与初始同步保持一致。

```python
EVENTS_LIST_PARAMS = {
    "singleEvents": "false",
    "showDeleted": "true",
    "maxResults": "2500",
    "fields": "nextPageToken,nextSyncToken,items(<见下>)",
}
```

- **`fields` 必须包含 `nextSyncToken,nextPageToken`**，否则响应里没有游标，增量同步永远无法开始。
- `items(...)` 只请求统一模型需要的字段：
  - `id,status,updated,summary,description,location`
  - `start,end,originalStartTime,recurringEventId,recurrence`
  - `reminders,transparency`
- 不使用 `timeMin` / `timeMax`。它们不能与 `syncToken` 同时使用，时间窗口在本地规划阶段应用。代价是首次同步会拉取整个日历的历史事件，只发生一次。
- 首次同步：不带 `syncToken`，翻页直到最后一页取得 `nextSyncToken`。
- 后续同步：只带 `syncToken`，返回自上次以来变化的事件，包括已删除的（`status = "cancelled"`）。没有变化时只返回一个新游标。
- 返回 410 时抛出 `CursorExpired`：清空该日历的镜像，重新全量同步。

**日历默认提醒**

- 每个 Google 账号每轮发一次 `calendarList.list`，带它自己的 `syncToken`，`fields` 只取 `items(id,defaultReminders),nextSyncToken,nextPageToken`。
- 默认提醒变化时，标记该日历中"使用默认提醒"的系列需要重新计算哈希。

**映射到统一模型**

- 提醒：
  - `reminders.useDefault = true` 时使用该日历的默认提醒。
  - 只映射 `method = "popup"`，`minutes = N` 映射为 `offset = -N 分钟`。
- 重复事件：
  - 主事件的 `recurrence` 原样保留（其中 `EXDATE` 解析到 `exdates`）。
  - 带 `recurringEventId` 的事件按 `originalStartTime` 归入主事件的 `overrides`。
  - `status = "cancelled"` 的实例转为 `exdates`。
- 镜像以事件 ID 为键存储原始字段子集，在规划阶段按 `recurringEventId` 分组成系列。
  - 增量里可能只出现某个实例的改动，分组保证它与镜像中已有的主事件合并。
  - 若某个实例的主事件不在镜像中（异常数据），记录警告并跳过该实例。
- 全天事件：`start.date` / `end.date` 映射为 date 类型的 `When`。
- 描述：Google 的 `description` 可能是 HTML。转为纯文本：
  - `<br>`、`</p>` 转为换行。
  - 去除其余标签，解码 HTML 实体，保留原有换行。
- 不同步的字段：`attendees`、`organizer`、会议链接。写入目标的事件不带 `ATTENDEE` / `ORGANIZER`，避免目标服务器向参与者发送邀请邮件。

**典型数据形态**：由 AI 助手生成的日历里，常见"一个重复系列，几乎每个实例都单独改过标题（例如带进度计数）"，事件级提醒为 `popup, 0 分钟`，描述为 HTML。渲染、哈希和比对必须高效覆盖"一个系列 + 大量单次改动"的情况。

### 6.3 CalDAV 目标

**协议层（`dav.py`）**

只实现需要的请求，全部基于共享 httpx 客户端：

- `PROPFIND`
- `REPORT`（`calendar-query`、`calendar-multiget`、`sync-collection`）
- `MKCALENDAR`
- `PUT`、`DELETE`

XML 用标准库 `xml.etree` 解析，命名空间常量集中定义。

**发现**

- 从 `server_url` 解析 principal → calendar-home-set → 日历列表。
- 发现结果写入 store，后续周期直接复用。只有遇到 404、301 或 401 时才重新发现。
- 发现时同时读取每个日历的 `supported-report-set`，确定变化检测用哪一级（见下）。

**资源命名与托管标记**

- 资源名：`calsync-<rule8>-<id24>.ics`。
  - `rule8 = blake2b(规则名)[:8]`。
  - `id24 = blake2b(规则名 + "\0" + 源日历 ID + "\0" + 源事件 ID)[:24]`。
  - 资源名本身即可识别"是否 calsync 托管、属于哪条规则"，判断归属不需要读取正文。
- 每个 `VEVENT` 带：
  - `UID:<id24>@calsync`
  - `X-CALSYNC-RULE:<规则名>`
  - `X-CALSYNC-HASH:<内容哈希>`
- 若实验证明服务器不保留客户端指定的资源名（§13），退回到读取正文中的 `UID` 和 `X-CALSYNC-RULE` 判断归属。
- CalendarServer 默认把 `X-` 属性视为每个用户自己的数据：托管标记只对写入它的账号可见。因此一条规则的写入和读取必须使用同一个目标账号；不要让不同账号的规则写入同一个共享日历。

**资源内容**

一个系列对应一个 `.ics` 资源，包含：

- `VTIMEZONE`（由 `icalendar` 生成）。
- 主 `VEVENT`，以及每个单次改动对应一个带 `RECURRENCE-ID` 的 `VEVENT`。
- 每个提醒对应一个 `VALARM`（`ACTION:DISPLAY`，`TRIGGER:-PT5M` 形式）。

**变化检测降级阶梯**

每个目标日历每轮只发一次检测请求，按服务器能力取最高一级：

1. **`sync-collection`（RFC 6578）**：带上次的 sync-token，只返回变化和删除的 href 与 etag。没有变化时响应为空。
2. **`getctag`**：PROPFIND 读取日历的 ctag。未变化则跳过；变化则执行第 3 级。
3. **全量列表**：`calendar-query` REPORT，只请求 `getetag`，不请求正文。

检测结果与目标缓存比对：

- 只对"资源名属于 calsync、且 etag 与缓存不同或缓存中没有"的 href 发起 `calendar-multiget`，每批最多 100 个，读取正文以取得哈希和跨度。
- 第 1 级的 sync-token 失效（服务器返回 `valid-sync-token` 前置条件错误）时，执行一次第 3 级，并重新取得 sync-token。

**写入**

- 新建：PUT 并带 `If-None-Match: *`。若返回 412（资源已存在），把该 href 加入下一次 multiget，再按更新处理。
- 更新：PUT 并带 `If-Match: <etag>`。若返回 412，multiget 取最新 etag 后重试一次。
- 删除：DELETE 并带 `If-Match: <etag>`。404 视为已删除。
- 写入成功后立即用新 etag 和哈希更新目标缓存。
  - 若响应没有返回 ETag，缓存记录"已写入、etag 待定"。
  - 下一次变化检测返回该 href 时，直接采信新 etag，不读取正文。
  - 这样自己的写入在下一轮不会触发任何正文读取。

**限流与并发**

- 每个账号用一个信号量限制并发写请求，默认 `max_concurrency = 2`。
- 遇到 429 / 503 时，该账号本轮并发降为 1，按 `Retry-After` 或指数退避（1s、2s、4s，最多 3 次）重试；仍失败则抛出 `TransientError`。

## 7. 同步周期

一个周期内：

1. **拉取**：所有源日历并发执行 `Source.pull`，同一个源日历即使被多条规则引用也只拉一次。增量应用到源镜像，游标随镜像在同一个 SQLite 事务中写入。
2. **检测**：所有目标日历并发执行 `Target.changes`，必要时 multiget，然后更新目标缓存。
3. **规划**：每条规则执行纯函数 `plan(desired, actual, window, policy)`：
   - `desired`：镜像中属于该规则源日历、跨度与窗口有交集、未被取消的系列，按 `id24` 建索引，值为内容哈希。
   - `actual`：目标缓存中属于该规则的托管资源。
   - `desired` 有、`actual` 无 → 新建。
   - 两边都有、哈希不同 → 更新。
   - `actual` 有、`desired` 无，且 `actual` 的跨度与窗口有交集 → 删除。
   - `actual` 有、`desired` 无，且跨度与窗口无交集 → 不处理（例如早已结束的旧事件）。
   - 用跨度而不是开始时间判断，是为了覆盖"系列开始于窗口之前、但仍在持续"的情况：这类系列在源头被删除后，目标副本必须被清理。
   - 时间窗口在本地计算，所以随着时间推移进入窗口的事件，即使源头没有任何变化，也会被新建。
4. **安全检查**：计划删除数 `> max_count`，或 `> max_ratio × len(actual)`（`len(actual) > 0` 时），则该规则本轮不执行，立即告警，并在告警中列出计划删除的事件标题。
5. **推送**：只渲染需要新建和更新的系列。按目标账号分组：不同账号并发执行，同一账号受信号量限制；先新建，再更新，最后删除。单个资源失败时记录错误，继续处理其余资源，本轮结果为"部分失败"。
6. **记录**：写入运行记录，包括各类数量、请求数、传输字节数、耗时。

规则之间相互隔离：一条规则失败，只影响它自己的结果。某个源或目标拉取失败，只跳过依赖它的规则。

`--dry-run` 执行第 1–4 步并打印计划，不推送，也不更新失败计数。拉取和检测得到的缓存照常写入，因为它们是对外部状态的如实记录。

## 8. 性能设计

这个工作负载完全受网络限制：CPU 和编程语言对耗时的影响可以忽略。性能目标因此定义为两件事：

1. **每个周期的请求数和传输量最少**。
2. **从源头修改到目标设备可见的延迟最短**。

### 8.1 请求预算（可测试的不变量）

| 场景 | Google 请求 | CalDAV 请求 | 读取事件正文 |
| --- | --- | --- | --- |
| 空闲周期（无变化） | 每个 Google 账号 1 次 `calendarList` + 每个源日历 1 次 `events.list` | 每个目标日历 1 次变化检测 | 0 |
| 源头有 k 个系列变化 | 同上（变化随增量返回） | 同上 + k 次写入 | 0 |
| 上一轮刚写入后的周期 | 同空闲周期 | 同空闲周期 | 0（采信自己写入的 etag） |
| 目标端被他人改了 m 个托管资源 | 同空闲周期 | 同空闲周期 + ⌈m/100⌉ 次 multiget + 修复写入 | m 个 |
| 冷启动（本地状态为空） | 每个源日历 ⌈N/2500⌉ 次全量翻页 | 发现（约 3 次）+ 每个目标日历 1 次全量列表 + ⌈托管数/100⌉ 次 multiget | 全部托管资源 |

另有每个 Google 账号约每小时 1 次 access token 刷新。

以一条规则为例：空闲周期共 3 次请求，每次响应都只有几百字节。按 60 秒周期，每天约 4,300 次 Google 请求，远低于 Google 的默认配额（每用户每分钟 600 次，每项目每天 1,000,000 次）。

### 8.2 手段

- **增量优先**：Google 用 syncToken；CalDAV 优先 `sync-collection`，其次 ctag。
- **只取需要的字段**：Google 用 `fields` 部分响应；CalDAV 检测只请求 `getetag`。
- **压缩**：请求带 `Accept-Encoding: gzip`，User-Agent 含 `gzip`，这是 Google 文档要求的。
- **连接复用**：常驻进程为每个主机保持一个 httpx 连接池，优先 HTTP/2（ALPN 协商，不支持时回落 HTTP/1.1）。
- **并发**：拉取和检测在所有源和目标之间并发；写入按账号限流。
- **按需计算**：哈希在模型层计算；只渲染需要写入的系列。镜像常驻内存，SQLite 只在启动时加载一次，之后增量写入。
- **SQLite**：WAL 模式；每个周期的镜像和缓存更新放进一个事务。
- **快速启动**：CLI 按子命令延迟导入模块；`keyring`、OAuth 流程等只在需要时导入。

### 8.3 延迟

- 默认周期 60 秒，加 ±10% 抖动。源头修改到目标服务器的延迟不超过一个周期加上本轮耗时；目标服务器推送到设备通常只需几秒。
- 进一步降低延迟要靠 Google 推送通知：Google 回调一个公网 HTTPS 地址（例如通过 Tailscale Funnel 暴露），常驻进程收到后立即开始一个周期。第一版不做，但 §10.3 的触发入口就是为它预留的。

### 8.4 度量

- 共享 httpx 客户端通过事件钩子统计每个周期、每个主机的请求数、状态码和传输字节数。
- 每个周期结束时写入运行记录，并输出一行结构化日志。
- `calsync status` 显示每条规则最近一轮的请求数、耗时和结果。
- §14 的测试断言空闲周期的请求数严格等于 §8.1 的预算，并且没有读取任何正文。任何让请求数变多的改动都会让测试失败。

## 9. 本地状态

SQLite 文件默认在 `~/.local/state/calsync/state.db`（Docker 中为数据卷）。所有表都可以从两端重建。

| 表 | 内容 |
| --- | --- |
| `source_cursor` | (账号, 日历) → events syncToken；账号 → calendarList syncToken；日历默认提醒 |
| `source_event` | (账号, 日历, 事件 ID) → 主事件 ID、原始开始时间、状态、`updated`、规范化字段 JSON |
| `target_calendar` | (账号, 日历 href) → 变化检测级别、sync-token 或 ctag、发现信息 |
| `target_object` | (账号, 日历 href, 资源 href) → etag、规则、内容哈希、跨度 |
| `run` | 规则、开始和结束时间、结果、各类数量、请求数、字节数、错误 |
| `alert` | 告警键 → 状态、最后发送时间（用于去重） |

- 镜像和游标在同一个事务中提交：游标前进时，对应的事件一定已经写入。
- 目标缓存的更新紧跟每次成功写入。进程中途退出后，下一轮的变化检测会自动纠正缓存。
- `store.py` 带模式版本号。版本不匹配时直接清空重建，不写迁移脚本，这正是"一切皆缓存"带来的好处。

## 10. 运行时与部署

### 10.1 常驻进程（推荐）

`calsync daemon` 是一个 asyncio 进程：

- 启动：获取 `state.db` 同目录下 `run.lock` 的排他文件锁，写入 PID；加载镜像和目标缓存到内存。
- 调度：上一个周期结束后等待 `interval × (1 ± jitter)` 再开始下一个，周期之间不会重叠。
- 失败退避：规则连续失败时，该规则的下一次尝试按 1、2、4…分钟退避，上限 15 分钟；其他规则不受影响。
- 信号：
  - `SIGTERM` / `SIGINT`：完成正在进行的单个写请求后退出，不开始新的写入。
  - `SIGHUP`：重新加载配置。新配置校验失败时保留旧配置并告警。
  - `SIGUSR1`：立即开始一个周期（见 §10.3）。

### 10.2 单次运行

`calsync run` 执行一个周期后退出，适合调试、cron 和 CI。如果常驻进程正持有锁，`run` 不执行同步，并提示改用 `calsync trigger`。

### 10.3 触发入口

`calsync trigger` 读取 PID 文件，向常驻进程发送 `SIGUSR1`。以后接入 Google 推送通知时，webhook 接收端只需调用同一个入口。

### 10.4 部署方式

- **macOS**：
  - 安装：`uv tool install`。
  - 常驻：`~/Library/LaunchAgents/<label>.plist` 以 `KeepAlive = true` 运行 `calsync daemon`，并设置 `HTTPS_PROXY` 等代理变量。
  - 日志：写入 `~/Library/Logs/calsync/`，按天轮转，保留 14 天。
  - 钥匙串：`keychain` 后端要求用户已登录且登录钥匙串已解锁。无人值守的机器需开启自动登录，或改用 `file` 后端。
  - `calsync service install` 会生成并加载 plist。
- **Docker**：
  - 基础镜像 `python:3.12-slim`，入口 `calsync daemon`。
  - 配置、凭证文件、状态目录通过挂载传入；凭证后端使用 `env` 或 `file`。
  - `HEALTHCHECK` 调用 `calsync status --health`：最近一个周期在 3 个周期时长内完成即视为健康。

## 11. 告警

| 情况 | 行为 |
| --- | --- |
| `AuthError` | 立即告警："账号 X 授权失效，请运行 `calsync auth X`"。同一账号在恢复前只告警一次 |
| 防误删拦截 | 立即告警，附计划删除的事件列表 |
| 其他失败 | 同一规则连续失败达到 `failure_threshold` 轮时告警一次 |
| 恢复 | 曾告警过的规则首次成功时推送"已恢复" |

告警发送失败只写日志，不影响同步结果。

## 12. 命令行

```
calsync auth <account>                 Google：浏览器 OAuth；CalDAV：从标准输入读取密码。写入凭证后端并验证可用
calsync calendars <account>            列出日历的 ID 和名称
calsync run [--rule NAME] [--dry-run]  执行一个周期
calsync daemon                         常驻运行
calsync trigger                        让常驻进程立即执行一个周期
calsync status [--json] [--health]     每条规则最近一轮的结果、请求数、耗时
calsync config check                   只校验配置
calsync service install|uninstall      macOS：生成并加载/卸载 launchd plist
```

退出码：0 成功；1 至少一条规则失败；2 配置错误；3 授权错误；4 常驻进程已在运行。

## 13. 待验证事项（实现第一步）

以下事项会影响默认写法或降级路径。必须在主体实现前用真实服务验证，结论回写本节。iCloud 实验使用专门的测试日历。

1. **共享日历的提醒**：
   - 背景：Apple 开源的 CalendarServer 把 `VALARM` 视为每个用户自己的数据（`twistedcaldav/datafilters/peruserdata.py`）。iCloud 线上是否同样处理尚未确认。
   - 实验 A：账号 1 写入带 `VALARM` 的事件到日历 C，C 共享给账号 2，观察账号 2 的设备是否提醒。
   - 实验 B：用账号 2 的凭证直接写入账号 2 自己的日历，观察提醒。
   - 结论决定"给家人同步"的推荐配置：写在自己账号再共享，还是直接写在家人账号。
2. **自定义属性**：iCloud 是否保留 `X-CALSYNC-RULE` / `X-CALSYNC-HASH`。
   - 若不保留：归属改由资源名判断，哈希只存在目标缓存中。
   - 冷启动时缓存为空，所有托管资源会被重写一次（代价有上限，且只在冷启动时发生）。
3. **资源名**：iCloud 是否保留客户端在 PUT 时指定的资源名。若不保留，归属判断退回到读取正文（§6.3）。
4. **变化检测**：iCloud 是否支持 `sync-collection` REPORT，或至少支持 `getctag`。
5. **建日历**：iCloud 是否接受 `MKCALENDAR`。若不接受，`create_if_missing` 改为报错，并提示"先在日历 App 中创建该日历"。
6. **ETag 返回**：iCloud 的 PUT 响应是否返回 `ETag`。这决定 §6.3 的"etag 待定"路径是不是常态。
7. **HTTP/2**：`caldav.icloud.com` 和 `www.googleapis.com` 是否协商出 HTTP/2。不影响正确性，只影响连接数。
8. **iCloud 中国区**：`caldav.icloud.com.cn` 的发现和写入流程是否与国际区一致（有中国区账号时验证）。

## 14. 测试

1. **单元测试**（pytest，无网络）：
   - 模型规范化与哈希稳定性，包括 `RENDER_VERSION` 变化时哈希随之变化。
   - Google 原始事件 → 统一模型：HTML 描述转换、提醒（默认 / 覆盖 / 仅弹窗）、重复系列与大量单次改动、取消的实例、全天事件、增量中只有实例没有主事件。
   - 镜像：全量后接增量、删除、410 后重建。
   - 渲染：时区、RRULE、EXDATE、RECURRENCE-ID、全天事件、三种提醒策略；不包含 ATTENDEE / ORGANIZER。
   - 规划：新建、更新、删除、窗口外不处理、跨度覆盖长期系列、事件随时间进入窗口、防误删阈值。
   - DAV 协议层：multistatus 解析、`sync-collection` 增量与令牌失效、412 处理。
   - 配置校验与凭证后端（`file` 后端的权限检查）。
2. **端到端测试**：
   - CalDAV 用本地 Radicale（确认所用版本支持 `sync-collection`）；Google 用基于 httpx MockTransport 的假服务，实现 syncToken 语义。
   - 覆盖流程：首次同步 → 源修改 → 源删除 → 手动建的非托管事件不受影响 → 防误删拦截 → 删除本地状态后冷启动结果一致。
   - **请求预算测试**：连续两个空闲周期，断言每个周期的请求数严格等于 §8.1 的预算，且没有任何 multiget 或 GET 正文。
3. **真实验证**：
   - §13 的实验。
   - 在 macOS 上用一条测试规则连续运行 24 小时，确认没有重复写入、没有误删、空闲周期请求数符合预算、告警正常。
4. **测试数据**：`tests/fixtures/` 只放合成数据。从真实 API 取得的样本必须先替换掉所有个人信息（标题、描述、邮箱、ID）再提交。

## 15. 开源准备

- 仓库目前为私有，计划在功能稳定后开源。
- 开源前完成：
  - 选择许可证（MIT 或 Apache-2.0）。
  - 编写 README 和配置示例。
  - 检查 git 历史中没有个人信息或凭证。必要时在公开前重写历史。
- 代码、文档、测试数据中只使用 `example.com` 邮箱和虚构的日历内容。
