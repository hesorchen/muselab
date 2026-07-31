// ==========================================================================
// Static UI data — extracted from app.js to keep that file focused on logic.
// Loaded as a plain <script> before app.js; values exposed on window.
// Add new constants here, not in app.js.
// ==========================================================================

// Preset accent colors offered in Settings. Bilingual names; UI tooltip
// picks the right side via `lang`.
window.MUSELAB_ACCENT_PRESETS = [
  { name: { zh: "默认蓝", en: "Classic blue" }, value: "#6093ff" },
  { name: { zh: "紫罗兰", en: "Violet" },        value: "#a78bfa" },
  { name: { zh: "翠绿",   en: "Emerald" },       value: "#34d399" },
  { name: { zh: "暖橙",   en: "Warm orange" },   value: "#fb923c" },
  { name: { zh: "玫红",   en: "Rose" },          value: "#f472b6" },
  // Slate (#94a3b8) removed 2026-05-28 — too low-contrast against the
  // neutral bg-1 backgrounds, "accent" effectively invisible. Users can
  // still pick it via the custom color picker if they really want.
];

// Editable file extensions — an intentionally-conservative frontend
// whitelist. NOTE: this does NOT mirror the backend, which has no TEXT_EXT
// whitelist at all: backend/files.py gates reads/writes with a BINARY_EXT
// blacklist + a NUL-byte sniff ("not blacklisted and no NUL → editable"),
// so it will happily edit any non-binary text file (.proto/.dart/.gradle/…).
// This list is the stricter of the two: a file shows an "Edit" button in the
// UI only if its extension is here, even though the backend would accept more.
// Trade-off is deliberate — the FE stays predictable and avoids offering Edit
// on exotic extensions we haven't visually verified render well in the editor.
// A Set so Alpine doesn't wrap it in a reactive Proxy when read from state.
window.MUSELAB_EDITABLE_EXT = new Set([
  "md", "markdown", "txt", "html", "htm", "json", "yaml", "yml",
  "py", "js", "ts", "tsx", "jsx", "mjs", "css", "scss", "less",
  "sh", "bash", "zsh", "toml", "ini", "cfg", "csv", "xml", "log",
  "sql", "rs", "go", "java", "cpp", "c", "h", "hpp", "rb", "php",
  "lua", "kt", "swift", "vue", "svelte", "tex", "rst", "env",
  "dockerfile", "makefile", "conf", "properties", "gitignore",
  "containerfile", "rakefile", "gemfile", "vagrantfile",
  "license", "licence", "readme", "changelog",
]);

// Slash command palette. Each entry: { name, desc: { zh, en } }. Used by the
// chat-input slash autocomplete; descriptions show in the bilingual hint row.
window.MUSELAB_SLASH_CMDS = [
  { name: "help",    desc: { zh: "查看所有可用斜杠命令", en: "List all slash commands" } },
  { name: "clear",   desc: { zh: "删除当前会话并新建一个（不可恢复）", en: "Delete current session and start fresh (cannot be undone)" } },
  { name: "compact", desc: { zh: "压缩历史 — 把上下文摘要成新会话", en: "Compact: summarize history into a new session" } },
  { name: "model",   desc: { zh: "/model <id> 切换模型，留空看可选项", en: "/model <id> — switch model (no arg = list)" } },
  { name: "resume",  desc: { zh: "/resume <名字> 跳到名字匹配的旧会话", en: "/resume <name> — jump to a session by name" } },
  { name: "cost",    desc: { zh: "显示当前用量 / 预算 / 缓存命中率", en: "Show current usage / budget / cache hit rate" } },
  { name: "config",  desc: { zh: "打开 Settings 面板", en: "Open Settings panel" } },
  { name: "stop",    desc: { zh: "中断当前流式响应", en: "Stop the current streaming reply" } },
];
