// Every visible string goes through t(). The English text is the key; Chinese lives in the table
// below and is the default. theme.js picks the language before first paint (?lang=, then the
// stored choice "calsync.lang", then Chinese) and sets <html lang>, which this module reads.
// Call t() only with a literal string, so the key test (tests/test_web_static.py) can see it.

const zh = {
  // shell
  "Sync now": "立即同步",
  "Syncing…": "同步中…",
  "Settings": "设置",
  "Sync map": "同步地图",
  "Calendar": "日历",
  "Activity": "活动",
  "Close": "关闭",
  "Inspector": "检查器",
  "Language": "语言",
  "One-way sync from Google Calendar to iCloud.": "从 Google 日历单向同步到 iCloud。",
  "calsync hub: open the settings": "calsync 中枢：打开全局设置",
  "1 line": "1 条线路",
  "{n} lines": "{n} 条线路",
  // services
  "Google Calendar": "Google 日历",
  "iCloud Calendar": "iCloud 日历",
  "CalDAV calendar": "CalDAV 日历",
  // the result sentence
  "Reading the sync state…": "正在读取同步状态…",
  "Every line is working": "每条线路都在正常工作",
  "The only line is working": "这条线路在正常工作",
  "No line yet": "还没有线路",
  "Not synced yet": "还没有同步过",
  "A line needs you": "有线路需要你处理",
  "Some lines are paused": "有线路暂停中",
  "Synced just now": "刚刚同步",
  "Synced {when}": "{when}同步",
  "next check {time}": "下次检查 {time}",
  "Done: {n} changes delivered": "同步完成，送达了 {n} 处改动",
  "Done: 1 change delivered": "同步完成，送达了 1 处改动",
  "Done: nothing new to deliver": "同步完成，没有新的改动",
  "Done, but some lines did not finish": "同步结束，但有线路没有完成",
  "The sync is still running; look again in a moment": "同步还在进行，稍后再看",
  "This sync did not finish": "这一轮同步没有完成",
  "Every line is paused: nothing to sync": "所有线路都暂停了，没有可同步的",
  "This calsync does not run syncs (--no-sync)": "这个 calsync 不运行同步（--no-sync）",
  "Can't reach calsync. Is it running?": "连不上 calsync，它还在运行吗？",
  "Something went wrong ({kind})": "出了点问题（{kind}）",
  // relative time
  "just now": "刚刚",
  "{n} min ago": "{n} 分钟前",
  "{n} h ago": "{n} 小时前",
  "{n} d ago": "{n} 天前",
  "{n} s": "{n} 秒",
  "{m} min {s} s": "{m} 分 {s} 秒",
  // inspector titles (each inspector fills its body in a later task)
  "Shared by every line": "所有线路共用",
  "Line": "线路",
  "Account": "账号",
  "New line": "新线路",
  "Choose a source, then a target": "选一个来源，再选一个目标",
  "Event": "日程",
  "Blocked deletes": "被拦下的删除",
  // weekdays
  "Sun": "周日",
  "Mon": "周一",
  "Tue": "周二",
  "Wed": "周三",
  "Thu": "周四",
  "Fri": "周五",
  "Sat": "周六",
};

export function getLang() {
  return document.documentElement.lang === "en" ? "en" : "zh";
}

export function locale() {
  return getLang() === "zh" ? "zh-CN" : "en-GB";
}

export function t(key, vars) {
  let s = getLang() === "zh" && Object.prototype.hasOwnProperty.call(zh, key) ? zh[key] : key;
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, name) => (name in vars ? String(vars[name]) : m));
  return s;
}
