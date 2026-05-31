# 排错

> [English](troubleshooting.md)

常见故障及修法。系统相关的安装问题见 [install-linux_zh.md](install-linux_zh.md) / [install-macos_zh.md](install-macos_zh.md)。环境快速自检：`bash scripts/doctor.sh`。

## 访问与鉴权

**每个请求都返回 401 / "bad token"。**
Web UI 用 `.env` 里的 `MUSELAB_TOKEN` 鉴权，以 `X-Auth-Token` header 发送。用 `grep MUSELAB_TOKEN .env` 找到它，粘贴到登录页面。（脚本调 API 时，发 `-H "X-Auth-Token: <token>"`，*不是* `Authorization: Bearer`。）

**丢了 / 想轮换 token。**
改 `.env` 里的 `MUSELAB_TOKEN`（≥16 字符）后重启，或在设置面板里改（无需重启）。然后重新登录 —— 浏览器 `localStorage` 里缓存的仍是旧 token，需重新粘贴。

## 模型与 provider

**Claude 模型 401，但我已登录 Pro/Max。**
后端需要 `~/.claude/.credentials.json`（来自 `claude login`）或 `ANTHROPIC_API_KEY` 二者之一。若安装器提示"claude CLI 已装但未登录"，跑一次 `claude login`。

**第三方 provider（DeepSeek/GLM 等）报"invalid api key"，但我确定 key 没错。**
确认 key 配在了*正确*的环境变量下（见 [配置 → Provider 密钥](configuration_zh.md#provider-密钥)）。muselab 把厂商流量经隔离 CLI 配置转发，绝不会把你的 Anthropic OAuth 发给它们 —— 所以这里的 401 是真的厂商 key 问题。

**MiniMax 用有效 key 仍 401。** 国内与国际是不同的账户/密钥：`minimaxi.com` 用 `MINIMAX_API_KEY`，`minimax.io` 用 `MINIMAX_INTL_API_KEY`。配与账户匹配的那个。

**全新安装后每次发送都 401。**
在配置任何 provider 之前建的会话，会锁定到一个不可用的 Claude 兜底模型。在设置里配一个 provider；之后新建的会话就会用上它，且在至少有一个可用模型之前输入框是禁用的。若某个旧会话卡住，新建一个即可。

## 服务与端口

**8765 端口被占用。**
通常是上一个 muselab 实例。用 `lsof -iTCP:8765 -sTCP:LISTEN` 找到它，停掉那个服务，或改 `.env` 里的 `MUSELAB_PORT`。安装器也会主动提议帮你停掉/禁用冲突的服务单元。

**服务起不来。**
查日志：

```bash
# Linux
journalctl --user -u muselab -n 50
# macOS
log show --predicate 'process == "muselab"' --last 5m
```

多数是 `.env` 缺值（如 `MUSELAB_TOKEN` 太短）或端口冲突。

**注销后服务就停了（Linux）。**
开启 lingering，让用户服务持续运行：`sudo loginctl enable-linger $USER`。

## 定时任务

**任务没卡点准时触发。** 调度循环每约 60 秒 tick 一次，所以可能晚至一分钟。这是正常的。

**机器关机期间任务没跑。** 启动时，错过的任务会补跑一次 —— 但仅在 24 小时窗口内。错过超过一天的任务会被跳过，因为那时它的 prompt 多半已经过期失去意义。

见 [定时任务 → 安全提示](scheduler_zh.md)：定时任务以完整权限无人值守运行。

## 手机端 / 推送通知

**iOS 无法注册 PWA 或开启通知。** iOS 要求安全上下文（HTTPS）。裸 `http://192.168.x.x:端口` 不行。用 Tailscale 的 `*.ts.net` 地址（自动 HTTPS）或跑 `scripts/setup-https.sh`。*先*把应用加到主屏，再开通知。完整步骤：[手机端 PWA](mobile_zh.md)。

**所有设备的推送同时失效。** `<archive>/.muselab/vapid.json` 的 VAPID 密钥对读不出来了。muselab 不会静默重建（那会作废所有订阅）。从备份恢复它，或主动删掉以生成新密钥对 —— 之后每台设备会重新订阅。

## 还是没解决？

跑 `bash scripts/doctor.sh`，带上它的输出提一个 [GitHub issue](https://github.com/hesorchen/muselab/issues)。
