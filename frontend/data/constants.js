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
//
// IMPORTANT — write these to be UNIVERSAL. muselab is open-source and
// these prompts ship to all users regardless of gender / age / family
// structure / profession / income / health condition / nationality.
// Do NOT mention partners ("my girlfriend"), specific companies
// ("ByteDance"), specific currencies / amounts ("¥5000"), specific
// health conditions ("骨密度"), or any other identity-specific framing.
// Use abstract framing — "the people I care about" instead of "parents",
// "my work" instead of "my T4 role", "my finances" instead of "my FIRE
// plan". Reflection / structure / pattern questions travel better than
// concrete-amount ones.
window.MUSELAB_INSPIRE_PROMPTS = [
  // health — body / habits / records, no specific conditions
  { tags: ["health"], zh: "看我的健康记录，最近有什么值得关注的趋势",
    en: "Look at my health records — what trends are worth my attention?" },
  { tags: ["health"], zh: "对比我最近两次体检数据，变化方向如何",
    en: "Compare my last two checkups — which direction is the trend?" },
  { tags: ["health"], zh: "我的运动、饮食、睡眠记录里，最弱的是哪一环",
    en: "Across my exercise / diet / sleep records — what's the weakest link?" },
  { tags: ["health"], zh: "档案里我可能漏掉的健康风险点",
    en: "What health risks am I likely overlooking from my files?" },
  { tags: ["health"], zh: "按我目前的健康习惯，3 年后会带来什么",
    en: "Given my current health habits, what state will I be in 3 years from now?" },

  // work — career / skills / projects, no specific company or role
  { tags: ["work"], zh: "我的工作 / 学习材料里，哪些经验可以提炼成方法论",
    en: "Across my work / study materials, which experiences can become methodology?" },
  { tags: ["work"], zh: "看我的工作记录，最近 3 个月最有价值的产出是什么",
    en: "From my work logs — what was my most valuable output in the last 3 months?" },
  { tags: ["work"], zh: "我目前的角色定位，长期看是机会还是瓶颈",
    en: "My current role — is it an opportunity or a ceiling in the long run?" },
  { tags: ["work"], zh: "针对我现在的方向，简历最关键应该突出什么",
    en: "For my current direction, what's the single thing my resume must highlight?" },
  { tags: ["work"], zh: "如果我下一阶段要换方向，从档案里看应该往哪走",
    en: "If I want to pivot next, what does my archive suggest I should pivot toward?" },

  // money — finances / planning / risk, no specific amounts or instruments
  { tags: ["money"], zh: "看我的财务档案，目标进展和实际差距多少",
    en: "From my financial records — how far am I from my own targets?" },
  { tags: ["money"], zh: "我的支出记录里，哪部分最容易被忽略",
    en: "In my spending records, which category is easiest to overlook?" },
  { tags: ["money"], zh: "我的储蓄 / 投资策略，跟当前人生阶段是否匹配",
    en: "Do my saving / investing choices match the life stage I'm actually in?" },
  { tags: ["money"], zh: "我的财务计划假设了哪些隐性条件？哪个最不稳",
    en: "What hidden assumptions does my financial plan rest on? Which is shakiest?" },
  { tags: ["money"], zh: "对照我的资产配置，风险分散度够不够",
    en: "Looking at my portfolio — is the risk spread enough?" },

  // people — relationships / connections, no specific role assumptions
  { tags: ["people"], zh: "我关心的人，最近有谁的状态值得我跟进",
    en: "Among the people I care about, whose situation needs a follow-up?" },
  { tags: ["people"], zh: "回顾我的人际记录，我最常忽略哪类人",
    en: "Reviewing my people notes — which kind of person do I most often overlook?" },
  { tags: ["people"], zh: "我和谁的对话最少？可能在疏远",
    en: "Whom do I have the fewest recent conversations with? May be drifting apart" },

  // knowledge — notes / reading / ideas
  { tags: ["notes", "knowledge"], zh: "我笔记里那些写到一半就停下的 idea",
    en: "Surface the half-written ideas in my notes" },
  { tags: ["notes", "knowledge"], zh: "我最近读的内容里，哪些跟我目标直接相关",
    en: "Of what I've read recently, which pieces directly relate to my goals?" },
  { tags: ["notes", "knowledge"], zh: "我档案里互相矛盾的两份记录，对照一下",
    en: "Find two records in my archive that contradict each other, side by side" },
  { tags: ["notes", "knowledge"], zh: "我对某个概念的多份笔记，整合成一份",
    en: "Where I have multiple notes on one concept, merge them into one" },
  { tags: ["notes", "knowledge"], zh: "推荐 3 本可能适合我现在状态的书，给理由",
    en: "Recommend 3 books that fit where I am now — with reasoning" },

  // reflection / decisions — always-usable (no identity assumptions)
  { tags: ["general"], zh: "如果你是我的朋友，会让我先做什么",
    en: "If you were my friend, what would you tell me to do first?" },
  { tags: ["general"], zh: "我哪些已经定下来的目标可能需要重新审视",
    en: "Which of my locked-in goals deserve a second look?" },
  { tags: ["general"], zh: "我最近的决策里有哪个我可能会后悔",
    en: "Of my recent decisions, which one am I most likely to regret?" },
  { tags: ["general"], zh: "我今年最重要的 3 个决定是什么？逐个反思",
    en: "What were my 3 most important decisions this year? Reflect on each" },
  { tags: ["general"], zh: "你认为我最近最大的盲点是什么",
    en: "What do you think is my biggest blind spot right now?" },
  { tags: ["general"], zh: "用一句话总结我最近 30 天",
    en: "Sum up my last 30 days in one sentence" },
  { tags: ["general"], zh: "我最近一个『感觉对』的判断，可能哪里错了",
    en: "A recent gut-call I made — where might it be wrong?" },
  { tags: ["general"], zh: "回头看 6 个月前的我，会羡慕现在的哪一面",
    en: "Looking back at me 6 months ago — what would they envy about me now?" },

  // archive overview — always-usable
  { tags: ["general"], zh: "我的 archive 里有什么？给一份导览",
    en: "What's in my archive? Give me a tour" },
  { tags: ["general"], zh: "把我档案里能用一句话总结的事都列给我",
    en: "List everything in my archive that fits in one sentence" },
  { tags: ["general"], zh: "推荐我接下来一周该专注的 3 件事",
    en: "Pick the 3 things I should focus on this coming week" },
  { tags: ["general"], zh: "今天找个角度问我一个我没问过自己的问题",
    en: "Ask me one question today that I haven't asked myself" },
  { tags: ["general"], zh: "整理我最近 30 天 archive 的改动，看趋势",
    en: "Round up the last 30 days of changes in my archive — what's the trend?" },
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
