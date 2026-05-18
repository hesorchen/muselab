# muselab e2e 测试

Playwright + pytest 的浏览器级测试，专门覆盖多 tab UI 交互这类**只有真浏览器能抓**的回归（DOM 事件、x-effect 反应、Alpine x-if/x-show 渲染竞态、SSE 后台流式）。

## 为什么不放进默认 pytest 套件

- Playwright + Chromium 二进制约 200 MB，CI / 本地装一次门槛较高
- 跑一次 e2e 要起后端 + 浏览器，比单元测试慢 30 倍
- 多 tab 交互回归很重要但低频；按需手动跑即可

所以 e2e 默认 **skip**，需要环境变量 `RUN_E2E=1` 才执行。

## 首次启用

```bash
# 装依赖（dev group 已声明 pytest-playwright，但 chromium 要单独下）
uv add --group dev pytest-playwright
uv run playwright install chromium

# 系统库（Ubuntu / Debian 缺 libnss3 等会启动失败）
sudo apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64
```

## 跑测试

```bash
# 起后端（独立终端）
MUSELAB_TOKEN=test-token-1234567890abcdef-secure-min-32 \
MUSELAB_ROOT=$HOME/claude_space/projects/muselab \
MUSELAB_PORT=9999 \
.venv/bin/python -m backend.main

# 跑 e2e（另一个终端）
RUN_E2E=1 .venv/bin/python -m pytest tests/e2e/ -v
```

## 覆盖范围（草稿）

| 测试 | 抓什么 |
|------|--------|
| 新建 / 切换 / 关闭 tab | tab 操作核心三件套 |
| 重命名 tab（双击） | inline-rename 模板与 blur 提交逻辑 |
| 右键菜单 | tabCtxMenu 显示 / 点击各项 / Esc 关闭 |
| 后台流式保留 | A 起 stream → 切到 B → 切回 A 看消息完整 |
| 关闭 tab 撤销 toast | undo 复位到原 index |
| 拖动 tab 重排序 | HTML5 drag & drop 完成顺序变更 |
| 刷新后预览 tab 持久化 | localStorage previewPath 恢复 |
| 浏览器 tab 标题 | document.title 反映当前 session + 流式 ● 前缀 |

具体实现见 `test_multi_tab.py`，新增场景沿用同一模式。

## 备注

- 跑 e2e 前后请确认 `MUSELAB_ROOT` 指向**测试隔离目录**或自己愿意被读到的目录，e2e 会真实创建 sessions
- 若想 headed 调试，spec 里把 `headless=True` 改 `False`
- token 必须 ≥ 32 字符，否则后端在启动期就拒绝
