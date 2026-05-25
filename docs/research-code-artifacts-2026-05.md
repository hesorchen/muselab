# Code Artifacts inline 渲染调研

> 调研日期：2026-05-25
> 触发：[COMPARISON_REPORT_2026-05.md](../COMPARISON_REPORT_2026-05.md) 第 6 节列出"无 Code Artifacts inline 渲染"为高优短板
> 目的：在不破坏 muselab "零构建 / Alpine.js / 单作者维护" 哲学的前提下，给出可执行的升级路径

---

## 1. 什么是 Code Artifacts inline 渲染

LLM 把生成的"可执行 / 可视化产物"在对话**旁边或下方**直接渲染出来，而不是仅返回代码块文本让用户复制走。最早由 Claude.ai 2024-06 推出，2025-2026 各开源项目（LibreChat、Open WebUI、Lobe Chat）相继跟进。

常见的「Artifact 类型」分三档：

| 档次 | 类型 | 难度 | 库 |
|---|---|---|---|
| **轻** | Mermaid 流程图 / 时序图 / 甘特图 | 低 | `mermaid.js`（CDN） |
| **轻** | SVG / 数学公式（KaTeX）| 低 | KaTeX（muselab 已有） |
| **轻** | 图表（柱状/折线/饼）| 低 | Chart.js（CDN） |
| **中** | 静态 HTML / CSS / vanilla JS | 中 | `<iframe sandbox srcdoc>` |
| **中** | 单文件可运行的 HTML demo | 中 | 同上 |
| **重** | React / JSX 组件 | 高 | Sandpack / Sucrase / esbuild-wasm |
| **重** | Vue / Svelte 组件 | 高 | 各自 runtime（很少做） |

---

## 2. 其他项目的实现方案对照

### 2.1 LibreChat（最成熟的开源参考）

- **库**：Sandpack（CodeSandbox 的嵌入式 runtime）
- **格式**：自定义 markdown directive
  ```
  :::artifact{identifier="..." type="text/html" title="..."}
  <html>...</html>
  :::
  ```
- **类型**：`text/html` / `application/vnd.mermaid` / `application/vnd.react`
- **CSP 要求**：`frame-src 'self' https://*.codesandbox.io`（默认走 CodeSandbox 公共 CDN，有遥测）
- **自托管**：可设 `SANDPACK_BUNDLER_URL` 指向自部署 bundler
- **缺点**：依赖 React 全栈、强依赖第三方 CDN、bundler 自托管运维成本高

参考：[LibreChat Artifacts 文档](https://www.librechat.ai/docs/features/artifacts)

### 2.2 Claude.ai（一手参考设计）

被逆向分析过的架构：**外层 iframe（sandbox proxy，在固定可信域名）+ 内层 iframe（跑用户 app，CSP 放宽）**——双层隔离防止恶意 HTML 访问 parent。MCP server 可为每个 app 提供自定义 CSP。

参考：[I Reverse Engineered ChatGPT Apps Iframe Sandbox - DEV](https://dev.to/infoxicator/i-reverse-engineered-chatgpt-apps-iframe-sandbox-2ok3)

### 2.3 Open WebUI

有 "Artifact Storage" 概念（持久化产物），但实现细节文档较少。基于其 Pipelines 插件框架，应是 plugin-driven，非内置。

### 2.4 Lobe Chat

通过 plugin 系统支持，主要走 Function Call → renderer plugin 路径。不是内置一等公民。

### 2.5 总结表

| 项目 | Mermaid | HTML 沙箱 | React | 实现复杂度 |
|---|---|---|---|---|
| LibreChat | ✅ | ✅ Sandpack | ✅ | 高（React + Sandpack 全栈） |
| Open WebUI | ⚠️ via plugin | ⚠️ via plugin | ❌ | 中 |
| Lobe Chat | ⚠️ via plugin | ⚠️ via plugin | ❌ | 中 |
| Claude.ai | ✅ | ✅ 双层 iframe | ✅ | 极高（自研架构） |

**结论**：muselab **不需要**对标 LibreChat 的 Sandpack 重栈方案；走"Mermaid + 单层 sandbox iframe"已可吃掉 80% 价值，且能保持零构建。

---

## 3. muselab 当前栈与可复用基础

### 已有依赖（全 vanilla / vendored）

| 库 | 版本/路径 | 用途 |
|---|---|---|
| marked.js | `vendor/marked.min.js` | Markdown → HTML |
| DOMPurify | `vendor/purify.min.js` | XSS sanitize |
| highlight.js | `vendor/highlight.min.js` + langs | 代码块高亮 |
| KaTeX | （已加载，用于 `$...$`） | 数学公式渲染 |

### 关键代码点

- 渲染入口：[frontend/app.js](../frontend/app.js) 中 `renderMarkdown` 函数（line ~5017）和 sanitize 块（line ~2320-2360）
- **现有 sanitize 配置禁掉了 iframe**：
  ```js
  FORBID_TAGS: ["style", "iframe", "form", "object", "embed"]
  ```
  这是 Artifacts 升级要绕的第一关——**不能简单解禁，必须分流**（受控生成的 Artifact iframe 走单独通路，AI 输出的原始 iframe 仍然 ban）。

### muselab 的约束

| 约束 | 影响 |
|---|---|
| **零构建（Alpine.js + 原生 JS）** | 排除 React/JSX 方案；可用任何 vendored .min.js |
| **单作者维护** | 排除自托管 Sandpack bundler 这类高运维方案 |
| **单用户隐私** | 不能走 CodeSandbox 公共 CDN（数据外发） |
| **archive 是文件系统** | Artifact 可考虑落盘到 archive 而非仅内存态 |

---

## 4. 推荐方案：三段式渐进升级

### Phase 1 — Mermaid + HTML 沙箱（高 ROI，1-2 天）

**Mermaid 支持**

- 新增 `frontend/vendor/mermaid.min.js`（CDN: jsDelivr `[email protected]`，~760KB；按需 lazy load）
- 在 `renderMarkdown` 之后扫描 `<pre><code class="language-mermaid">`，调用 `mermaid.render()` 换成 SVG
- **lazy load**：只在检测到 mermaid 代码块时才 inject `<script>`，普通对话不下载 760KB

**HTML / SVG 沙箱预览**

- 检测 `<pre><code class="language-html">` 或新增 `language-htmlpreview` 触发渲染
- **不解禁 DOMPurify 的 iframe**——改成"post-sanitize 注入"：sanitize 完 markdown 后单独走一个 walker 替换 code 节点为受控 iframe
- iframe 配置：
  ```html
  <iframe sandbox="allow-scripts allow-popups-to-escape-sandbox"
          srcdoc="..."
          style="width:100%;height:400px;border:1px solid var(--border)">
  </iframe>
  ```
- **关键**：**不能加 `allow-same-origin`**——一旦加了，sandbox 等于失效，AI 注入的 `<script>` 可以直接访问 parent DOM，等同 XSS

**UI 决策**

- 默认 inline 渲染（代码块下方折叠展开），不开侧栏 panel——更符合 muselab 单栏对话模型
- 头部按钮：`Run` / `Code` 切换源码/预览、`Copy` 复制源码、`Save` 落盘到 archive

**安全 checklist**（来自 [HackTricks iframe CSP 指南](https://hacktricks.wiki/en/pentesting-web/xss-cross-site-scripting/iframes-in-xss-and-csp.html)）：

- ✅ 必须 `sandbox` 属性
- ❌ 绝不加 `allow-same-origin`（与 allow-scripts 联用 = XSS）
- ✅ 不加 `allow-top-navigation`（防止 iframe 把整个 window 重定向）
- ✅ `srcdoc` 而非 `src`，避免外部资源 fetch
- ⚠️ 注意：srcdoc 中的相对 URL 会解析到 parent origin——iframe 内任何 `<script src="/foo">` 都是请求 parent。要么 ban 相对 URL，要么加 `<base>` 标签
- ⚠️ srcdoc 默认继承 parent 的 CSP（部分浏览器）——靠 `sandbox` 切断 origin 是更可靠的隔离

**工作量**：估计 1-2 天单人开发

### Phase 2 — Chart / 表格增强（0.5 天）

- 新增 `language-chart` 块（用户给 JSON config），用 Chart.js（约 200KB）渲染为 canvas
- 把已经支持的 markdown table 加排序/筛选/导出 CSV 按钮
- KaTeX 已有，无需再做

### Phase 3 — React / 复杂 JS（建议**暂不做**）

权衡：

| 收益 | 代价 |
|---|---|
| 跟齐 LibreChat、Claude.ai 视觉效果 | 引 Sucrase（~1MB）或 esbuild-wasm（~3MB）到 vendor/ |
| Claude 模型对 React Artifacts 训练充分，输出质量高 | iframe 内 React 运行时（React + ReactDOM ~140KB）必须随每个 Artifact 加载 |
| | 调试体验差（错误栈在 iframe 内） |
| | 偏离 "零构建" 哲学 |

**建议**：先用 Phase 1 接住「HTML demo + Mermaid」90% 场景。等 Phase 1 上线 3 个月看真实使用数据，如果用户**明确反馈**要 React Artifacts，再做。

---

## 5. 实施清单（Phase 1）

### 文件改动预估

| 文件 | 改动 |
|---|---|
| `frontend/vendor/mermaid.min.js` | 新增（vendor 进来，不走 CDN） |
| `frontend/app.js` `renderMarkdown` | 新增 mermaid 检测 + iframe 注入逻辑 |
| `frontend/app.js` sanitize 块 | 不动 FORBID_TAGS；新增 post-sanitize walker |
| `frontend/styles.css` | 新增 `.artifact-wrap`、`.artifact-toolbar`、`.artifact-iframe` 样式 |
| `frontend/index.html` | 头部 `<script>` 加 mermaid lazy loader（或在 app.js 内 dynamic import） |
| `docs/artifacts.md` | 新文档：用户教程 + 安全说明 |
| `docs/architecture.md` | 增 1 段说明 Artifact 通道独立于 sanitize 主流 |

### 用户侧 UX 决策（开工前定）

1. **触发方式**：
   - A. 自动——AI 输出 `language-html` / `language-mermaid` 块自动渲染
   - B. 显式 directive——AI 必须用 `:::artifact{type=...}` 才渲染
   - **推荐 A**：用户教育成本低；B 需要 prompt 工程把 AI 教会用 directive

2. **渲染位置**：
   - A. inline 在代码块下方
   - B. 右侧抽屉 panel
   - **推荐 A**：单栏对话流更直观；muselab 已经有右侧 chat 区，再开 panel 会和 archive preview 抢空间

3. **持久化**：
   - A. 仅渲染态，刷新失效
   - B. 写入 archive 某个目录（如 `_artifacts/`），可重新打开
   - **推荐 B**，但放 Phase 1.5——按钮加好 hook 即可

4. **默认开关**：默认开 Mermaid，HTML iframe 默认**关**（用户在 Settings 显式开启）——安全保守起步

### 测试 case

- Mermaid 各种图（flowchart / sequence / class / state / gantt）
- HTML iframe 注入尝试：`<script>parent.alert(1)</script>` 应失败（sandbox 切断）
- iframe 内相对路径 `<img src="/etc/passwd">`：应触发 parent origin 请求（建议 ban 相对路径或加 `<base href="about:blank">`）
- 流式输出未完成的 mermaid 块：不应抛错
- DOMPurify mid-stream 局部失败：不应破坏 Artifact 渲染

---

## 6. 风险与边界

| 风险 | 缓解 |
|---|---|
| **AI 输出恶意 HTML 通过 iframe 攻击 parent** | sandbox 不带 allow-same-origin + 不带 allow-top-navigation + CSP frame-src 限制 |
| **iframe 内大量 fetch / DDoS 第三方** | sandbox 不带 `allow-same-origin` 后 fetch 是 null origin，CORS 会阻挡多数请求；可选叠加 `Content-Security-Policy` header 收紧 |
| **Mermaid bundle 760KB 拖累首屏** | lazy load——只在出现 mermaid 块时 inject script |
| **iframe 高度自适应** | 用 `ResizeObserver`，或固定 400px 提供"全屏"按钮兜底 |
| **暗色主题与 Artifact 内容色冲突** | iframe 内 `prefers-color-scheme` 用户自己处理；muselab 不强制注入 |
| **手机端 iframe 触摸 / 缩放** | 加 `<meta name="viewport">` 提示 AI 在 HTML 模板里加 |

---

## 7. 决策点（需要用户拍板）

1. **是否做？**（如果只是「看看」，写个 mermaid 支持就够了，全套 Phase 1 是 1-2 天投入）
2. **Phase 1 全做 vs 只做 Mermaid？** 只 Mermaid 半天搞定，零风险；HTML iframe 多 1 天，需要安全 review
3. **Settings 默认开关：保守（HTML 默认关）vs 激进（默认开）？**
4. **Artifact 持久化（Phase 1.5）是否一起做？** 一起做总工时 2-3 天但用户价值翻倍

---

## 8. 引用

### 项目文档
- [LibreChat Artifacts 用户文档](https://www.librechat.ai/docs/features/artifacts)
- [LibreChat Artifacts 用户指南](https://www.librechat.ai/docs/user_guides/artifacts)
- [LibreChat 自托管 Sandpack bundler issue #6693](https://github.com/danny-avila/LibreChat/issues/6693)

### Mermaid vanilla JS 用法
- [Mermaid getting started](https://mermaid.ai/open-source/intro/getting-started.html)
- [Mermaid usage 文档](https://mermaid.ai/open-source/config/usage.html)
- [Smart client-side rendered Mermaid Charts](https://mfyz.com/smart-client-side-rendered-mermaid-charts-on-astro-blogs/)

### iframe sandbox 安全
- [HTMLIFrameElement: srcdoc property - MDN](https://developer.mozilla.org/en-US/docs/Web/API/HTMLIFrameElement/srcdoc)
- [Content-Security-Policy: sandbox directive - MDN](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/sandbox)
- [Iframes in XSS, CSP and SOP - HackTricks](https://hacktricks.wiki/en/pentesting-web/xss-cross-site-scripting/iframes-in-xss-and-csp.html)
- [Sandbox-iframe XSS challenge solution - Johan Carlsson](https://joaxcar.com/blog/2024/05/16/sandbox-iframe-xss-challenge-solution/)
- [One-Way Sandboxed Iframes: Read-Only Sandbox](https://joshua.hu/rendering-sandboxing-arbitrary-html-content-iframe-interacting)
- [iframe csp attribute, sandbox: allow-same-origin pitfalls](https://csplite.com/csp153/)

### Claude.ai 参考架构
- [I Reverse Engineered ChatGPT Apps Iframe Sandbox - DEV](https://dev.to/infoxicator/i-reverse-engineered-chatgpt-apps-iframe-sandbox-2ok3)

---

## 9. 局限说明

- 未实测各方案在 muselab 实际栈上的兼容性——上述方案是基于代码读取 + 文档调研的设计推断，不是验证后的结论
- 未测 mermaid.js 与现有 highlight.js 冲突可能（理论上不冲突，因为是不同 language class）
- iframe sandbox 在不同浏览器（Safari WebKit vs Chromium）行为有差异，本报告未做矩阵测试
- React Artifacts 的 Sucrase 方案未真正评估打包大小和兼容性——若 Phase 3 启动需补做
- 未调研 muselab 现有 SSE 流式渲染流程对 Artifact lazy mount 的影响，实施前需读 [frontend/app.js](../frontend/app.js) 中 `renderMarkdown` 与 stream chunk 的 nextTick 调度细节
