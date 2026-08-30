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

// Slash command palette is temporarily disabled. Keep the implementation
// behind one explicit flag so `/...` remains ordinary chat text until the UX
// is redesigned, without deleting the command/control work below.
window.MUSELAB_SLASH_ENABLED = false;

// Slash command registry. The palette, aliases, busy policy and dispatcher all
// read this same table so pointer, keyboard and send-button execution cannot
// quietly drift into different command semantics.
//
// policy:
//   immediate — control action that must remain available while a turn runs
//   readonly  — opens/reads existing UI and never mutates the session runtime
//   stateful  — changes session/runtime state and is rejected while it is busy
// argKind names a second-stage candidate provider implemented by the composer.
window.MUSELAB_SLASH_CMDS = [
  { name: "context", policy: "readonly",
    desc: { zh: "查看当前会话的上下文占用", en: "Inspect this session's context usage" } },
  { name: "compact", policy: "stateful",
    desc: { zh: "用 Agent SDK 原生压缩当前会话", en: "Compact this session through the Agent SDK" } },
  { name: "model", policy: "stateful", argKind: "model",
    desc: { zh: "切换模型（继续输入可搜索）", en: "Switch model (keep typing to search)" } },
  { name: "permission", aliases: ["permissions"], policy: "stateful", argKind: "permission",
    desc: { zh: "切换当前会话权限模式", en: "Change this session's permission mode" } },
  { name: "mcp", policy: "readonly",
    desc: { zh: "打开 MCP 服务面板", en: "Open the MCP servers drawer" } },
  { name: "stop", policy: "immediate",
    desc: { zh: "中断当前流式响应", en: "Stop the current streaming reply" } },
  { name: "usage", aliases: ["cost"], policy: "readonly",
    desc: { zh: "显示当前用量、预算与缓存命中率", en: "Show usage, budget and cache hit rate" } },
  { name: "effort", policy: "stateful", argKind: "effort",
    desc: { zh: "切换当前模型的推理强度", en: "Change reasoning effort for this model" } },

  // Existing MuseLab-local conveniences remain available. They intentionally
  // use the same registry/dispatcher even though they are not part of the
  // first Agent-SDK-oriented command set above.
  { name: "help", policy: "readonly",
    desc: { zh: "查看所有可用斜杠命令", en: "List all slash commands" } },
  { name: "clear", policy: "stateful",
    desc: { zh: "删除当前会话并新建一个（不可恢复）", en: "Delete current session and start fresh (cannot be undone)" } },
  { name: "resume", policy: "stateful", argKind: "session",
    desc: { zh: "跳到名字或 ID 匹配的旧会话", en: "Jump to a session by name or ID" } },
  { name: "config", policy: "readonly",
    desc: { zh: "打开 Settings 面板", en: "Open Settings panel" } },
];
