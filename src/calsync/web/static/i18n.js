// English strings are the keys; Chinese lives in the table below.
// Chinese is the default when navigator.language starts with "zh"; the choice is kept in
// localStorage ("calsync.lang"). Call t() only with a literal string so the key test can see it.

const LANG_KEY = "calsync.lang";

const zh = {
  "just now": "刚刚",
  "{n} min ago": "{n} 分钟前",
  "{n} h ago": "{n} 小时前",
  "{n} d ago": "{n} 天前",
  "{n} s": "{n} 秒",
  "{m} min {s} s": "{m} 分 {s} 秒",
  "at start": "开始时",
  "{n} min after start": "开始后 {n} 分钟",
  "{n} d before": "提前 {n} 天",
  "{n} h before": "提前 {n} 小时",
  "{n} min before": "提前 {n} 分钟",
  "No alerts": "无提醒",
  "Alerts: {list}": "提醒：{list}",
  ", ": "、",
  "Google": "Google 日历",
  "OK": "正常",
  "Sign-in expired": "授权失效",
  "Blocked": "已拦截",
  "Failed": "失败",
  "Not run yet": "未运行",
  "Added": "新增",
  "Changed": "修改",
  "Reverted": "改回",
  "Deleted": "删除",
  "Protected": "受保护",
  "Partial": "部分完成",
  "Skipped": "已跳过",
  "Read-only demo": "只读演示",
  "Needs you": "需要处理",
  "Error": "出错",
  "Not synced yet": "还没有同步过",
  "Menu": "菜单",
  "Settings": "设置",
  "Language": "语言",
  "Appearance": "外观",
  "System": "跟随系统",
  "Light": "浅色",
  "Dark": "深色",
  "About calsync": "关于 calsync",
  "One-way sync from Google Calendar to iCloud. This page only reads; it never edits your events.":
    "从 Google 日历单向同步到 iCloud。这个页面只读，不会修改你的日程。",
  "This calsync runs in demo mode and does not sync.": "这个 calsync 以演示模式运行，不会同步。",
  "Demo mode": "演示模式",
  "Syncing…": "同步中…",
  "Sync now": "立即同步",
  "Pages": "页面",
  "Overview": "总览",
  "Sync history": "同步记录",
  "1 thing needs you": "有 1 件事需要你处理",
  "{n} things need you": "有 {n} 件事需要你处理",
  "All good": "一切正常",
  "Last good sync {ago}": "上次成功同步在 {ago}",
  "Synced {ago}": "{ago}同步",
  "The first sync starts soon": "第一次同步马上开始",
  "{n} changes synced in 24 h": "24 小时内同步了 {n} 处改动",
  "No changes in 24 h": "24 小时内没有改动",
  "The {account} account needs to be connected again.": "{account} 账号需要重新连接。",
  "Sync paused: {n} deletions are waiting for your confirmation.": "同步已暂停：{n} 个删除在等你确认。",
  "Sync paused: deletions are waiting for your confirmation.": "同步已暂停：有删除在等你确认。",
  "Sync has failed {n} times in a row.": "同步已连续失败 {n} 次。",
  "Sync line {rule}": "同步线路 {rule}",
  "(untitled)": "（无标题）",
  "Run this on the computer that runs calsync:": "在运行 calsync 的电脑上执行：",
  "Copied": "已复制",
  "Selected, copy it": "已选中，请手动复制",
  "Copy": "复制",
  "All events": "全部日程",
  "Past {p} days – next {f} days": "过去 {p} 天 – 未来 {f} 天",
  "From": "出发 From",
  "To": "到达 To",
  "{n} series": "{n} 个系列",
  "{n} protected": "{n} 个受保护",
  "{n} outside the range": "{n} 个在范围外",
  "One-way": "单向",
  "State comes from an older calsync; delivery is unknown": "状态来自旧版本 calsync，送达状态未知",
  "On iCloud and identical to Google.": "已在 iCloud 上，内容与 Google 一致。",
  "Changed in Google; the next sync delivers it.": "Google 里有改动，下次同步时送达。",
  "Delivered": "已送达 · 验讫",
  "Pending": "待送达",
  "Unknown": "未知",
  "Tomorrow": "明天",
  "All day": "全天",
  "next sync": "下次同步",
  "Sync line": "同步线路",
  "Target calendar": "目标日历",
  "Repeats": "重复",
  "Recurring event": "重复日程",
  "One-off event": "单次日程",
  "Time": "时间",
  "Delivery": "送达",
  "Next 24 hours": "接下来 24 小时",
  "Coupons": "Coupons",
  "No events in the next 24 hours": "接下来 24 小时没有日程",
  "Void": "作废",
  "Recent changes": "最近改动",
  "Changes": "Changes",
  "No changes yet": "还没有改动",
  "All sync history": "全部同步记录",
  "{n} added": "新增 {n}",
  "{n} changed": "修改 {n}",
  "{n} reverted": "改回 {n}",
  "{n} deleted": "删除 {n}",
  "1 idle sync": "1 次空闲同步",
  "{n} idle syncs": "{n} 次空闲同步",
  "{n} requests": "{n} 次请求",
  "Could not load the changes of this sync.": "这次同步的改动没能载入。",
  "Nothing was written in this sync.": "这次同步没有写入任何改动。",
  "The details of this sync are no longer kept.": "这次同步的改动明细已不在记录中。",
  "Latest {n} syncs, newest first": "最近 {n} 次同步，最新的在前",
  "No syncs recorded yet": "还没有同步记录",
  "Can't reach calsync. Check the connection and try again.": "连不上 calsync。检查网络后再试一次。",
  "Sync could not start: {message}": "没能开始同步：{message}",
  "The sync is taking longer than usual. This page keeps checking.": "这次同步比平时慢，页面会继续检查。",
  "No sync data yet": "还没有同步数据",
  "No sync data yet. Once sync starts, its status shows up here.": "还没有同步数据，启动同步后这里会显示状态",
  "Can't reach calsync": "连不上 calsync",
  "Retry": "重试",
  "calsync answered with an error": "calsync 返回了错误",
};

export const tables = { zh };

function readStored() {
  try {
    return localStorage.getItem(LANG_KEY);
  } catch (e) {
    return null;
  }
}

function detect() {
  const q = new URLSearchParams(window.location.search).get("lang");
  if (q === "zh" || q === "en") return q;
  const s = readStored();
  if (s === "zh" || s === "en") return s;
  return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
}

let lang = detect();

export function getLang() {
  return lang;
}

export function setLang(value) {
  lang = value === "zh" ? "zh" : "en";
  try {
    localStorage.setItem(LANG_KEY, lang);
  } catch (e) {
    /* storage unavailable: the choice lasts for this page only */
  }
}

export function locale() {
  return lang === "zh" ? "zh-CN" : "en-GB";
}

export function t(key, vars) {
  let s = lang === "zh" && Object.prototype.hasOwnProperty.call(zh, key) ? zh[key] : key;
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, name) => (name in vars ? String(vars[name]) : m));
  return s;
}
