# calsync 设计规格

- 日期：2026-10-23（v2 修订：2026-10-24；v3 与计划 1 的实现同步：2026-10-24）
- 状态：v3（实现同步）, 2026-10-24
- 作者：Aryous 与 Claude

> 2026-10-24：第二阶段设计见 `2026-10-24-calsync-v2-product-design.md`。两者冲突之处以第二阶段设计为准，受影响的章节列在该文件 §10。

> v2 相对 v1 的变化：
> - 引入源日历本地镜像和 Google 增量同步。
> - 目标侧采用"变化检测降级阶梯"。
> - Google 和 CalDAV 客户端改为基于 httpx 自写。
> - 常驻进程成为推荐运行方式，并新增"性能设计"一节，把每轮请求数写成可测试的预算。
> - 去除了背景中的个人信息，为开源做准备。
>
> v3 相对 v2 的变化（与计划 1 的实现对齐，不改变目标）：
> - §6.1 接口改为实际签名：`Source.pull` 一次拉取一个账号的多个日历，单个日历的失败彼此隔离；目标写入接口以 href 为参数。
> - 403 不再一律视为授权失败（§6.1、§6.2、§6.3、§12）；配置错误单独归类并以退出码 2 结束。
> - 写明了无法解析的源系列、412 冲突、重定向、重新发现等边界行为（§6.3、§7）。
> - §9 表结构与实现一致；§2 写明目标端手动修改的现有局限，§13 增加对应实验。

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

同步方向始终是单向：源 → 目标。（块①已实现目标端改动的改回，本段关于修改不被发现和 xfail 的描述已过时：见第二阶段设计 §3.3。）在目标日历中手动修改托管事件，目前只有当修改去掉或改变了 `X-CALSYNC-HASH` 时，才会在下一轮被源的内容覆盖；保留该标记的修改（例如在 iPhone 上改时间，客户端通常原样保留未知的 `X-` 属性）不会被发现，直到源头再次改动该系列。完整的修复（比对正文或 etag，而不只是哈希）要等计划 2 在 iCloud 上验证"手机编辑是否保留标记、我们自己 PUT 之后 etag 是否变化"之后再做（§13 第 10 项），测试 `test_manual_target_edit_is_repaired` 以 `xfail(strict=True)` 固定了这一现状。若需要 A、B 互通，配置 A→B 和 B→A 两条规则；引擎会跳过带 calsync 标记的源事件，避免循环复制。第一版没有 CalDAV 源，所以这个跳过逻辑随 CalDAV 源适配器一起实现。

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
  errors.py        统一错误类型
  durations.py     时长字符串解析（30s、5m、7d、-15m）
  config.py        读取并校验 TOML 配置
  credentials.py   凭证后端：keychain / env / file（不叫 secrets.py，避免与标准库同名）
  model.py         统一事件模型、规范化、内容哈希、跨度、时间窗口
  icalutil.py      iCalendar 小工具（EXDATE/RDATE 解析、时区还原）
  naming.py        确定性的资源名、UID、规则标签
  http.py          共享 httpx 客户端工厂：HTTP/2、gzip、代理、超时、请求计数、重试退避
  sources/
    base.py        源适配器接口
    google_map.py  Google 事件 → 统一模型（纯函数）
    google.py      Google Calendar REST 客户端（增量同步、日历默认提醒）
    google_auth.py access token 刷新；浏览器授权（loopback + PKCE）在计划 3 加入
  targets/
    base.py        目标适配器接口、目标缓存数据结构
    dav.py         最小 WebDAV/CalDAV 协议层：PROPFIND、REPORT、PUT、DELETE、MKCALENDAR、重定向
    caldav.py      CalDAV 目标适配器：发现、变化检测阶梯、托管资源识别、写入限流
  render.py        统一模型 → iCalendar；读取已写入资源的标记与跨度
  mirror.py        把一次源拉取应用到内存镜像
  planner.py       纯函数：(应有集合, 现有集合, 窗口) → 计划；防误删检查
  engine.py        一个同步周期的编排：拉取、检测、规划、安全检查、推送、记录
  store.py         SQLite：镜像、目标缓存、游标、运行记录、告警状态
  lock.py          单实例文件锁（记录 PID）
  wiring.py        按配置构建适配器和引擎（唯一把账号变成客户端的地方）
  notify.py        Bark / 通用 webhook（计划 3）
  runtime.py       常驻进程：调度、信号、优雅退出（计划 3）
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
- `rules[].name` 在配置内唯一。它参与生成资源名和 UID，**修改规则名等于以新名字重建该规则的全部托管资源**。
  - 改名或删除一条规则后，旧名字写入的副本留在目标日历中：calsync 不再更新它们，也不会删除它们（它们不属于任何现有规则）。需要手动删除。
  - 两处 calsync 安装若在同一个目标日历上使用同名规则，会把对方的副本当作自己的，互相覆盖和删除。不要这样配置。
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
    async def pull(self, calendar_refs: Sequence[str], state: Mapping[str, Any]) -> SourcePull: ...
    # SourcePull = calendars（按传入的日历引用索引的 CalendarPull）+ state + errors
    #   CalendarPull = 解析后的日历 ID + 是否为全量 + 新增/修改的原始事件 + 删除的事件 ID + 日历元数据
    #   state = 适配器自己的游标（可 JSON 序列化，引擎原样保存、下次原样交回）
    #   errors = 单个日历自己的失败（名称不存在、404、403 等）；它们的游标不前进。账号级失败直接抛出
    def to_series(self, raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Series]: ...
    def series_ids(self, raw_events: Iterable[RawEvent]) -> set[str]: ...
    # 镜像中所有存活系列的源 ID，无论 to_series 能否解析（用于保护无法解析的系列，见 §7）

class Target(Protocol):
    def reset(self) -> None: ...  # 每个周期开始时调用：恢复本轮被限流降低的并发
    async def list_calendars(self) -> list[TargetCalendar]: ...
    async def ensure_calendar(self, ref: str, *, create: bool) -> TargetCalendar: ...
    async def changes(self, cal: TargetCalendar, cursor: str | None) -> TargetDelta: ...
    # TargetDelta = 变化的托管资源（href → etag，服务器未给出时为 None）+ 被删除的 href + 是否为全量 + 新游标
    async def fetch(self, cal: TargetCalendar, hrefs: Sequence[str]) -> list[FetchedObject]: ...
    def href_for(self, cal: TargetCalendar, name: str) -> str: ...  # 资源名 → 绝对 href
    async def create(self, href: str, data: bytes) -> str | None: ...  # 返回 etag
    async def update(self, href: str, etag: str | None, data: bytes) -> str | None: ...
    async def delete(self, href: str, etag: str | None) -> None: ...
    # etag 为 None 表示未知，此时以 If-Match: * 写入（§6.3）
```

`FetchedObject` 是读到的资源：`href`、`etag`、正文。目标缓存中每个托管资源记为 `CachedObject`：`etag`（自己写入且服务器未返回 ETag 时为 None）、规则标签、对象 ID、内容哈希、跨度。

**跨度**是资源覆盖的时间范围：从第一次发生的开始，到最后一次发生的结束。它从 `DTSTART`、`DTEND` 和 RRULE 的 `UNTIL` / `COUNT` 计算；没有结束的重复规则，`span_end` 记为无穷大。

适配器只抛出统一的错误类型：

- `AuthError`：凭证缺失或失效（HTTP 401、OAuth `invalid_grant` 等），不重试，提示运行 `calsync auth`。
- `ForbiddenError`：凭证有效，但服务器拒绝了这个请求（不属于限流的 403：权限不足、`valid-calendar-data`、`no-uid-conflict` 等前置条件）。按普通失败处理，不提示重新授权。
- `TransientError`：网络错误、429、5xx、Google 的限流 403、无法使用的令牌响应（例如强制门户页面），可重试。
- `ConflictError`：412 版本冲突。
- `NotFoundError`：日历或资源不存在（404）。
- `CursorExpired`：游标失效，需要全量。
- `ConfigError`：配置问题，包括运行时才发现的（源日历名称不存在、凭证文件权限不是 `0600`、Google 客户端密钥文件不存在）。

### 6.2 Google 源

**授权**

- 用户自建 Google Cloud 项目和"桌面应用"类型的 OAuth 客户端，scope 为 `https://www.googleapis.com/auth/calendar.readonly`。
- `calsync auth` 走 loopback + PKCE：打开浏览器，在本机临时端口接收回调，把 refresh token 写入凭证后端。
- **OAuth 同意屏幕必须发布为 "In production"**。Google 官方文档说明：外部用户类型、处于 "Testing" 状态的项目，refresh token 7 天后失效。个人使用无需审核，授权时会出现"未经验证的应用"提示。
- access token 只保存在进程内存中，过期前 5 分钟刷新，不落盘。
- API 返回 401 时丢弃缓存的 access token，刷新一次并重试一次；仍为 401 则抛出 `AuthError`。
- 令牌接口：只有 400/401 且 JSON 正文带 `error`（如 `invalid_grant`）才是 `AuthError`；非 JSON 正文，或 200 却没有可用的 `access_token`（强制门户、代理页面）是 `TransientError`。
- 403：原因是限流（`rateLimitExceeded`、`userRateLimitExceeded`、`quotaExceeded`、`dailyLimitExceeded`、`RESOURCE_EXHAUSTED`）时为 `TransientError`；其他 403 为 `ForbiddenError`，消息带 Google 给出的原因。它通常只影响一个日历，经 `SourcePull.errors` 只让依赖该日历的规则失败。

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

- 每个 Google 账号每轮发一次 `calendarList.list`，带它自己的 `syncToken`，参数 `showDeleted=true`、`showHidden=true`，`fields` 只取 `nextPageToken,nextSyncToken,items(id,summary,summaryOverride,timeZone,defaultReminders,deleted)`。
- 同一份结果也用于把配置里的日历名称解析为 ID：先匹配 `summaryOverride`（用户自己看到的名字），再匹配 `summary`；匹配到多个时报错。不在列表中、但包含 `@` 的引用按 ID 原样使用（用户有权访问但没有加入列表的日历）。
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
  - `<br>` 和块级标签（`p`、`div`、`li`、`ul`、`ol`、`h1`–`h6`、`tr`）的结束转为换行。
  - 链接保留地址，写成"文字 (地址)"；文字与地址相同时只写一次。
  - 去除其余标签，解码 HTML 实体（不换行空格转为普通空格），保留原有换行。
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
- 发现结果写入 store，后续周期直接复用。只有变化检测返回 404 时才清除发现结果，下一轮重新发现。
- 重定向（301、302、307、308）在协议层透明跟随，最多 5 跳：每一跳重新带上认证，拒绝从 https 降级到 http；href 按最终响应的 URL 转成绝对地址。重定向本身不触发重新发现。
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

1. **`sync-collection`（RFC 6578）**：带上次的 sync-token，只返回变化和删除的 href 与 etag。没有变化时响应为空。按 RFC 6578 以 `Depth: 0` 发送。集合本身返回 507 表示结果被截断，此时这次结果不当作全量。
2. **`getctag`**：PROPFIND 读取日历的 ctag。未变化则跳过；变化则执行第 3 级。
3. **全量列表**：`calendar-query` REPORT，只请求 `getetag`，不请求正文。

检测结果与目标缓存比对：

- 只对"资源名属于 calsync、且 etag 与缓存不同或缓存中没有"的 href 发起 `calendar-multiget`，每批最多 100 个，读取正文以取得哈希和跨度。
- 某个发生变化的 href 读不到正文（multiget 没有返回它）时，本轮把它当作不存在来规划（新建会 412 并转为更新），并且不推进游标，让下一轮的检测再次报告它。
- 第 1 级的 sync-token 失效（服务器返回 `valid-sync-token` 前置条件错误）时，用空 sync-token 重新做一次初始 `sync-collection`（列出全部资源，相当于全量列表），并取得新的 sync-token。

**写入**

- 新建：PUT 并带 `If-None-Match: *`。
- 更新：PUT 并带 `If-Match: <etag>`；etag 未知时带 `If-Match: *`（资源必须仍然存在）。
- 删除：DELETE 并带 `If-Match: <etag>`，etag 未知时同样用 `*`。404 视为已删除。
- 严格的服务器对不存在的资源也会用 412 回应 `If-Match`，所以每次 412 都先重新读取该资源，再决定如何重试一次：
  - 新建 412：资源已存在则按更新处理（若该系列是降级的，见 §7，则保留现有副本不覆盖）；读取时已不存在则重新新建。
  - 更新 412：资源仍在则用读到的 etag 重试；已不存在则重新新建。
  - 删除 412：资源仍在则用读到的 etag 再删一次；已不存在则视为删除完成。
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
   - 源镜像中存在、但无法解析的系列（未解析），以及只有部分实例能解析的系列（降级），它们在目标中已有的副本既不更新也不删除，并且不计入安全检查的分母；降级系列若还没有副本，则按能解析的部分新建。每个系列每个进程只警告一次，日志只写源 ID，不写事件内容。
4. **安全检查**：计划删除数 `> max_count`，或计划删除数 `≥ 3` 且 `> max_ratio × len(actual)`，则该规则本轮不执行：不删除，也不新建、不更新。立即告警，并在告警中列出计划删除的事件标题；读取标题需要 ⌈d/100⌉ 次 multiget（d 为计划删除数），只有被拦截的规则付出这个代价。比例检查要求至少删 3 条，是为了让小日历删掉最后一两条事件时不被拦截。确认是有意删除时，用 `calsync run --allow-deletes` 放行一次。
5. **推送**：只渲染需要新建和更新的系列。按目标账号分组：不同账号并发执行，同一账号受信号量限制；先新建，再更新，最后删除。单个资源失败时记录错误，继续处理其余资源，本轮结果为"部分失败"。
6. **记录**：写入运行记录，包括各类数量、请求数、开始和结束时间、错误。传输字节数留到计划 3。

规则之间相互隔离：一条规则失败，只影响它自己的结果。某个源或目标拉取失败，只跳过依赖它的规则。源的失败按日历隔离：同一账号下一个日历失败（名称不存在、404、403），不影响该账号的其他日历；只有账号级失败（例如 `calendarList` 授权失败）才让该账号的全部规则失败。

`--dry-run` 执行第 1–4 步并打印计划，不推送，也不更新失败计数。拉取和检测得到的缓存照常写入，因为它们是对外部状态的如实记录。目标日历不存在时，`--dry-run` 不会创建它，该规则失败（配置了 `create_if_missing` 时提示"正式运行会创建它"）。

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

上表的 CalDAV 列按 `sync-collection` 模式计算：

- `getctag` 模式下，任何变化之后的周期（包括我们自己的写入改变了 ctag）都要付 PROPFIND + 一次 `calendar-query` 全量列表；之后 ctag 不变的周期才是 1 次请求。
- 全量列表模式每轮都列出全部 etag。
- "上一轮刚写入后的周期"一行假设服务器在检测中报告的 etag 就是 PUT 返回的 etag；iCloud 是否如此待 §13 第 6、10 项验证。

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

- 共享 httpx 客户端通过事件钩子统计每个周期、每个主机的请求数、状态码和传输字节数（计划 1 只统计请求数，字节数在计划 3 加入）。
- 每个周期结束时写入运行记录，并输出一行结构化日志。
- `calsync status` 显示每条规则最近一轮的结果和耗时，以及该周期的总请求数。请求数按周期统计，同一周期内的规则共享同一个数字；按规则拆分留到计划 3 再考虑。
- §14 的测试断言空闲周期的请求数严格等于 §8.1 的预算，并且没有读取任何正文。任何让请求数变多的改动都会让测试失败。

## 9. 本地状态

SQLite 文件默认在 `~/.local/state/calsync/state.db`（Docker 中为数据卷）。所有表都可以从两端重建。

| 表 | 内容 |
| --- | --- |
| `source_state` | 账号 → JSON：`calendars_token`（calendarList syncToken）、`meta`（每个日历的名称、时区、默认提醒）、`events_tokens`（每个日历的 events syncToken） |
| `source_event` | (账号, 日历, 事件 ID) → Google 返回的原始事件 JSON（字段子集），规划时再分组、规范化 |
| `target_calendar` | (账号, 配置中的日历引用) → 日历 href、名称、变化检测级别、游标（sync-token 或 ctag） |
| `target_object` | (账号, 日历 href, 资源 href) → etag、规则标签、对象 ID、内容哈希、跨度 |
| `run` | 规则、开始和结束时间、结果、各类数量、请求数、错误；每条规则保留最近 500 条。字节数留到计划 3 |
| `alert` | 告警键 → 状态、最后发送时间（用于去重） |

- 镜像和游标在同一个事务中提交：游标前进时，对应的事件一定已经写入。
- 目标缓存在变化检测之后落盘一次，在每次成功写入后立即更新内存，在周期结束时再统一落盘。进程在推送中途退出时，正确性不受影响：下一轮的变化检测会发现这些资源的 etag 与缓存不符，每个受影响的 href 多花一次 multiget 读取，然后缓存恢复正确。
- `store.py` 带模式版本号。版本不匹配时直接清空重建，不写迁移脚本，这正是"一切皆缓存"带来的好处。
- 同一个日历可能被多个引用指向（名称和 href）。删除某个 href 的缓存资源时，指向它的所有引用的游标一并清空，下一轮对它做一次冷启动检测。
- 数据库文件损坏或不是 SQLite 文件时，把它连同 `-wal`、`-shm` 改名为 `.corrupt-<时间>` 隔离，然后新建一个空库。
- 数据库文件以 `0600` 权限创建，所在目录为 `0700`。

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
calsync auth <account>                 CalDAV：从标准输入读取密码。Google：浏览器授权（计划 3）；
                                       在此之前用 --refresh-token-stdin 读入已有的 refresh token。
                                       先用新凭证验证（列出日历），成功后才写入凭证后端；空输入直接拒绝；
                                       验证失败时保留原有凭证
calsync calendars <account>            列出日历的 ID 和名称
calsync run [--rule NAME] [--dry-run] [--allow-deletes]
                                       执行一个周期；--allow-deletes 对本次选中的每条规则放行一次
                                       防误删拦截（用 --rule 缩小范围）
calsync daemon                         常驻运行
calsync trigger                        让常驻进程立即执行一个周期
calsync status [--json] [--health]     每条规则最近一轮的结果、请求数、耗时
calsync config check                   只校验配置
calsync service install|uninstall      macOS：生成并加载/卸载 launchd plist
```

全局选项：`--config`（默认 `$CALSYNC_CONFIG` 或 `~/.config/calsync/config.toml`），`--state-dir`（默认 `$CALSYNC_STATE_DIR` 或 `~/.local/state/calsync`）。

退出码：0 成功；1 至少一条规则失败、部分失败或被防误删拦截（包括 `ForbiddenError`）；2 配置错误（包括账号初始化时的配置错误，例如凭证文件权限不是 `0600`）；3 授权错误；4 常驻进程已在运行。`calsync run` 有多条规则时按优先级取一个：任一授权错误为 3，否则任一配置错误为 2，否则任一失败为 1，否则为 0。网络、状态数据库或文件系统的意外错误只打印一行 `error: <类型>: <消息>`，退出码 1，不输出 traceback。

## 13. 待验证事项（实现第一步）

以下事项会影响默认写法或降级路径。必须在主体实现前用真实服务验证，结论回写本节。iCloud 实验使用专门的测试日历。

1. **共享日历的提醒**（实验 A 已完成，2026-10-24；实验 B 待做）：
   - 背景：Apple 开源的 CalendarServer 把 `VALARM` 视为每个用户自己的数据（`twistedcaldav/datafilters/peruserdata.py`）。共享协议 `doc/Extensions/caldav-sharing.txt` §5.5.4 要求按用户存储 `VALARM` 与 `TRANSP`，§7 规定"共享者设置的提醒默认不应传播给被共享者"，客户端也不应在用户确认前自动启用共享日历上的提醒。
   - 实验 A（iCloud 中国区，真实使用）：账号 1 写入带 `VALARM` 的事件（每个事件一个开始时提醒，预约事件三个提前提醒）到日历 C，C 共享给账号 2。
     - 账号 2 的 iPhone 上，事件显示，但账号 1 写入的提醒不显示，也不触发。
     - 账号 2 的 iPhone 把"默认提醒时间"设为"日程发生时"后，共享事件显示一个开始时提醒，并按时触发。账号 1 写入的其他提醒（例如提前 1 天、1 小时、15 分钟）仍然看不到。
     - 另一个先决条件：iOS 设置里日历 App 的系统通知必须打开；日历信息页的"日程提醒"开关只控制该日历的提醒能否显示，不会带入共享者的提醒。
     - 早先一次观察到预约事件在账号 2 上显示一两个提醒，原因未查明，未能复现。
   - 推断：CalendarServer 不会在共享日历中由服务器添加默认提醒（`txdav/caldav/datastore/sql.py` 的 `processAlarms`："Never add default alarms to calendar data in shared calendars"），所以账号 2 看到的开始时提醒，应是 iOS 在客户端按本机默认提醒生成的。iCloud 线上服务不开源，此点未直接验证。
   - 实验 B：用账号 2 的凭证直接写入账号 2 自己的日历，观察提醒是否原样显示并触发。尚未进行。
   - 结论（给家人同步的推荐配置）：提醒必须准确送达时，直接写入家人自己的账号（每个接收人一个 CalDAV 账号、一条规则）。写在自己账号再共享，只在"对方默认提醒恰好与所需提醒一致"时可用，例如所有事件都只要开始时提醒；提前提醒和多个提醒都会丢失。
2. **自定义属性**：iCloud 是否保留 `X-CALSYNC-RULE` / `X-CALSYNC-HASH`。
   - 若不保留：归属改由资源名判断，哈希只存在目标缓存中。
   - 冷启动时缓存为空，所有托管资源会被重写一次（代价有上限，且只在冷启动时发生）。
3. **资源名**：iCloud 是否保留客户端在 PUT 时指定的资源名。若不保留，归属判断退回到读取正文（§6.3）。
4. **变化检测**：iCloud 是否支持 `sync-collection` REPORT，或至少支持 `getctag`。
5. **建日历**：iCloud 是否接受 `MKCALENDAR`。若不接受，`create_if_missing` 改为报错，并提示"先在日历 App 中创建该日历"。
6. **ETag 返回**：iCloud 的 PUT 响应是否返回 `ETag`。这决定 §6.3 的"etag 待定"路径是不是常态。
   - 观察（2026-10-24 真实运行）：写入后的下一轮没有读取正文，说明 iCloud 的 PUT 响应带 ETag（推断，未抓包确认）。
7. **HTTP/2**：`caldav.icloud.com` 和 `www.googleapis.com` 是否协商出 HTTP/2。不影响正确性，只影响连接数。
8. **跨主机发现**：iCloud 的 `calendar-home-set` 通常是另一个主机上的绝对地址（`pNN-caldav.icloud.com`），并可能伴随重定向。实现已按此设计：手动跟随重定向（每一跳重新带认证，禁止改变协议），所有 href 按返回它的响应的最终 URL 转成绝对地址。需要实测确认。
9. **iCloud 中国区**：`caldav.icloud.com.cn` 的发现和写入流程是否与国际区一致（有中国区账号时验证）。
10. **手机端编辑托管事件**：在 iOS 上编辑一个托管事件，观察正文是否保留 `X-CALSYNC-HASH`，以及我们自己 PUT 之后服务器报告的 etag 是否变化。结论决定 §2 的"目标端修改会被覆盖"如何实现（按 etag 识别他人修改，还是读取正文比对）。
   - 块①改为按可见内容摘要比对（`content_digest`），不依赖 `X-CALSYNC-HASH` 是否保留；真机验证见第二阶段设计 E1。
11. **错误密码的状态码**：用错误或已作废的 App 专用密码访问 iCloud，确认返回 401 而不是 403。calsync 只把 401 当作授权失效（提示 `calsync auth`），403 按"无权限"处理（退出码 1）；若 iCloud 用 403 表示密码错误，需要调整映射。

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
