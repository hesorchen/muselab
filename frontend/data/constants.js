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
  { name: { zh: "石板灰", en: "Slate" },         value: "#94a3b8" },
];

// Editable file extensions (matches backend TEXT_EXT). A Set so Alpine
// doesn't try to wrap it in a reactive Proxy when read from component state.
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

// Inspire prompts — surfaced on the empty chat screen ("试试问 Muse").
// Each entry is bilingual + tagged with which archive subdirs it leans on.
// `general` tag = always usable (no archive content required). Frontend
// filters by whichever subdirs actually exist in the user's archive,
// shuffles, then shows a handful.
window.MUSELAB_INSPIRE_PROMPTS = [
  // health — body / training / family medical
  { tags: ["health"], zh: "看看我的健康档案，最近有什么需要警觉的",
    en: "Look at my health files and tell me what to watch" },
  { tags: ["health"], zh: "对比最近两次体检的关键指标变化，趋势是好是坏",
    en: "Compare key indicators across my last two checkups — better or worse" },
  { tags: ["health"], zh: "我的训练频率够吗？给改进建议",
    en: "Is my training frequency enough? Suggest improvements" },
  { tags: ["health"], zh: "我父母的健康档案里有哪些容易被忽略的细节",
    en: "What's easy to miss in my parents' health files?" },
  { tags: ["health"], zh: "我哪些补剂可能在重复或浪费？哪些可能漏掉",
    en: "Which supplements am I doubling up on or missing?" },
  { tags: ["health"], zh: "按我现在的体检数据，3 年后最可能出问题的是什么",
    en: "Given my current labs, what's most likely to flag in 3 years?" },

  // work / career — resume / interview / target companies
  { tags: ["work"], zh: "我的简历里最容易被面试官质疑的点是什么",
    en: "What's the easiest point on my resume for an interviewer to push back on?" },
  { tags: ["work"], zh: "针对我的目标公司列表，简历应该针对哪几条优化",
    en: "Targeting my list of companies, which lines on my resume should I sharpen?" },
  { tags: ["work"], zh: "如果我现在拿到 offer，应该谈到什么数字才合理",
    en: "If I got an offer today, what numbers should I be negotiating to?" },
  { tags: ["work"], zh: "复盘我最近的项目，哪个最能讲故事 / 哪个最弱",
    en: "Review my recent projects — which one tells the best story, which is weakest?" },
  { tags: ["work"], zh: "我目标公司列表里漏了哪些值得关注的方向",
    en: "What's missing from my target-company list that I should consider?" },
  { tags: ["work"], zh: "如果一年内我没换工作，我会损失什么 / 留下来又赚到什么",
    en: "If I don't switch jobs in a year, what do I lose? What do I gain by staying?" },

  // money — portfolio / FIRE / insurance
  { tags: ["money"], zh: "看我现在的持仓，哪些和我 FIRE 目标对不上",
    en: "Look at my portfolio — which holdings don't fit my FIRE plan?" },
  { tags: ["money"], zh: "如果我每月多投 5000 元，能提前几年到 FIRE",
    en: "If I add ¥5k/month, how many years earlier do I hit FIRE?" },
  { tags: ["money"], zh: "我的资产配置 currency / 区域风险够分散吗",
    en: "Is my asset mix diversified enough across currencies / regions?" },
  { tags: ["money"], zh: "对照保险清单，我哪些保障还差或重复",
    en: "Against my insurance list — what coverage am I missing or doubling up on?" },
  { tags: ["money"], zh: "我现在的现金 / 应急金够撑多少个月",
    en: "How many months of runway does my cash / emergency fund cover?" },
  { tags: ["money"], zh: "假设市场跌 40%，我的 FIRE 时间表会被推迟多久",
    en: "Assume a 40% market drawdown — how many years does that push my FIRE timeline?" },

  // people — relationships / family
  { tags: ["people"], zh: "我关心的人最近有哪些需要跟进的事",
    en: "Who in my people files needs a follow-up from me lately?" },
  { tags: ["people"], zh: "父母的保险 / 健康 / 财务，哪个最紧迫",
    en: "Parents' insurance / health / finances — which is most urgent?" },
  { tags: ["people"], zh: "我和女朋友聊天里我可能没听明白的事",
    en: "From my notes with my partner — anything I might have missed hearing?" },

  // knowledge — notes / papers
  { tags: ["notes", "knowledge"], zh: "整理我笔记里那些写了一半就停下的 idea",
    en: "Surface the half-written ideas in my notes" },
  { tags: ["notes", "knowledge"], zh: "我最近读的论文，哪些跟我工作直接相关",
    en: "Of the papers I've read recently, which apply directly to my work?" },
  { tags: ["notes", "knowledge"], zh: "把我档案里互相矛盾的两份记录找出来",
    en: "Find two records in my archive that contradict each other" },
  { tags: ["notes", "knowledge"], zh: "推荐 3 本可能适合我现状的书，给理由",
    en: "Recommend 3 books that fit where I am now — with reasoning" },

  // reflection / decisions — always-usable
  { tags: ["general"], zh: "如果你是我的朋友，会让我先做什么",
    en: "If you were my friend, what would you tell me to do first?" },
  { tags: ["general"], zh: "我哪些已经定下来的目标可能要重新审视",
    en: "Which of my locked-in goals deserve a second look?" },
  { tags: ["general"], zh: "我最近的决策里有哪个我可能会后悔",
    en: "Of my recent decisions, which one am I most likely to regret?" },
  { tags: ["general"], zh: "我今年最重要的 3 个决定是什么？给反思",
    en: "What were my 3 most important decisions this year? Reflect on each" },
  { tags: ["general"], zh: "你认为我最近最大的盲点是什么",
    en: "What do you think is my biggest blind spot right now?" },
  { tags: ["general"], zh: "用一句话总结我最近 30 天",
    en: "Sum up my last 30 days in one sentence" },

  // archive overview — always-usable
  { tags: ["general"], zh: "我的 archive 里有什么？给一份目录式导览",
    en: "What's in my archive? Give me a tour" },
  { tags: ["general"], zh: "把我档案里能用一句话总结的事都列给我",
    en: "List everything in my archive that fits in one sentence" },
  { tags: ["general"], zh: "推荐我接下来一周该专注的 3 件事",
    en: "Pick the 3 things I should focus on this coming week" },
  { tags: ["general"], zh: "今天找个角度问我一个我没问过自己的问题",
    en: "Ask me one question today that I haven't asked myself" },
];

// Slash command palette. Each entry: { name, desc: { zh, en } }. Used by the
// chat-input slash autocomplete; descriptions show in the bilingual hint row.
window.MUSELAB_SLASH_CMDS = [
  { name: "help",    desc: { zh: "查看所有可用斜杠命令", en: "List all slash commands" } },
  { name: "clear",   desc: { zh: "清空当前会话", en: "Reset / clear current session" } },
  { name: "compact", desc: { zh: "压缩历史 — 把上下文摘要成新会话", en: "Compact: summarize history into a new session" } },
  { name: "model",   desc: { zh: "/model <id> 切换模型，留空看可选项", en: "/model <id> — switch model (no arg = list)" } },
  { name: "resume",  desc: { zh: "/resume <名字> 跳到名字匹配的旧会话", en: "/resume <name> — jump to a session by name" } },
  { name: "cost",    desc: { zh: "显示当前用量 / 预算 / 缓存命中率", en: "Show current usage / budget / cache hit rate" } },
  { name: "config",  desc: { zh: "打开 Settings 面板", en: "Open Settings panel" } },
  { name: "stop",    desc: { zh: "中断当前流式响应", en: "Stop the current streaming reply" } },
];
