# 在 Windows 上安装 muselab

> [English](install-windows.md)

个人 Windows 机器一键安装。使用 **Task Scheduler** 在用户登录时自动启动。
完全跑在用户态——不需要服务账号，不需要管理员权限。

## 前置准备

干净 Windows 上需要一次性装好三样东西。**严格按顺序**——每一步都依赖前一步在
PATH 里就绪。

### 1. 设置 PowerShell ExecutionPolicy

Windows 默认是 `Restricted`，会拦截所有 PowerShell 脚本——包括 uv 的安装包装器
**和**装完之后的 uv 本身。

```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
```

回答 `Y`。只影响当前用户。**之后开一个新 PowerShell 窗口。**

跳过这一步会看到：
```
Error: PowerShell requires an execution policy in [Unrestricted, RemoteSigned, Bypass] to run uv.
```

### 2. 安装 `git`

干净 Windows 不带 git。

```powershell
# 推荐：winget（Win10 1809+ / Win11 内置）
winget install --id Git.Git -e
```

或从 https://git-scm.com/download/win 下载。

> 🚨 **必须关掉当前 PowerShell 窗口，重新开一个**——Windows 在进程启动时
> snapshot PATH；当前窗口仍是旧的（无 git 的）PATH，即使 git 已装好。跳过
> 重开会报：`'git' is not recognized as a cmdlet`。
>
> 在新窗口里 `git --version` 验证。

（不想装 git？滚到 "[No-git install](#no-git-install)"。）

### 3. 安装 `uv`

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

[文档](https://docs.astral.sh/uv/getting-started/installation/)。

> 🚨 **再关掉并重开 PowerShell**，让 uv 进入 PATH。`uv --version` 验证。

### 4.（可选）Anthropic 模型用的 `claude` CLI

```powershell
claude login
```

凭据存在 `%USERPROFILE%\.claude\`。非 Claude provider（DeepSeek / GLM / MiniMax）
只需 API key——之后在 Settings UI 内填。

## 安装

```powershell
git clone https://github.com/hesorchen/muselab
cd muselab
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

### No-git install

如果你跳过了上面的第 2 步：

```powershell
Invoke-WebRequest https://github.com/hesorchen/muselab/archive/refs/heads/main.zip -OutFile muselab.zip
Expand-Archive muselab.zip
cd muselab\muselab-main
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

代价：未来升级需要重新下 zip，不能用 `git pull`。

> **为什么用 `-ExecutionPolicy Bypass`？** Windows 默认是 `Restricted`，会
> 拒绝未签名脚本。`Bypass` 仅对本次调用生效；不会改你系统层的 policy。

脚本会：

1. 校验 `uv`（缺 `claude` 会警告）
2. 执行 `uv sync`
3. **询问你**的 archive 目录（Muse 可读写的目录），默认
   `%USERPROFILE%\muselab-archive`
4. 生成 `.env`（含随机 `MUSELAB_TOKEN` 和 `MUSELAB_HOST=127.0.0.1`），并把文件
   ACL 限制到你这个用户
5. 注册一个 Scheduled Task `Muselab`（触发器 At Logon，失败时重启）
6. 立即启动任务并 curl `localhost:8765` 确认

如果 `.env` 已存在，脚本会保留不动（可安全重跑）。

## 验证

```powershell
Get-ScheduledTask -TaskName Muselab    # State 应为 Running
Start-Process http://localhost:8765    # 浏览器
Select-String MUSELAB_TOKEN .env       # 登录时粘贴
```

## 重启后会自动启动吗？

会——任务触发器是 `AtLogOn`。重启后回到你的账号即自启 muselab。如果开了自动
登录，回到桌面时就已经在跑了。

## 常用命令

```powershell
Get-ScheduledTask    -TaskName Muselab     # 查状态
Start-ScheduledTask  -TaskName Muselab     # 启动
Stop-ScheduledTask   -TaskName Muselab     # 停止
Get-Content -Wait "$env:LOCALAPPDATA\muselab\logs\stderr.log"   # tail 日志

powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1   # 重新校验
powershell -ExecutionPolicy Bypass -File scripts\intake.ps1   # 重做 intake
```

## 暴露到 LAN（可选）

默认仅绑定 `127.0.0.1`。让同一 WiFi 的手机 / 平板能连：

1. 编辑 `.env`：
   ```
   MUSELAB_HOST=0.0.0.0
   ```
2. 重启：`Stop-ScheduledTask -TaskName Muselab; Start-ScheduledTask -TaskName Muselab`
3. 查机器的 LAN IP：`ipconfig` → 在 WiFi 适配器下找 IPv4
4. 用管理员 PowerShell 打开 Windows 防火墙的 8765 端口：
   ```powershell
   New-NetFirewallRule -DisplayName "Muselab" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow
   ```
5. 在其他设备上访问：`http://<那个 IP>:8765`

⚠ Token 泄露 ≈ `MUSELAB_ROOT` 的 shell 级访问。不可信网络上不要不加 HTTPS +
认证层直接暴露。

## 卸载

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1
```

停止并删除计划任务。`.env`、`sessions\`、archive 目录和日志**不会**被动。

## WSL 备选方案

如果你更喜欢 Linux 工具链，装 WSL2 + Ubuntu，然后在 WSL 发行版内按
[install-linux_zh.md](install-linux_zh.md) 走。服务在 WSL 里用 `systemd --user`
运行（需要 `/etc/wsl.conf` 内 `systemd.enabled = true`）。

## 排错

| 现象 | 排查 |
|------|------|
| 任务 `Ready` 但从未 `Running` | 在 Task Scheduler GUI 查 `Last Run Result`；多半是路径问题。对照 installer 日志校验 `uv` 在 `$UvPath` |
| 端口被占 | `Get-NetTCPConnection -LocalPort 8765` → 杀进程或改 `MUSELAB_PORT` |
| Anthropic 模型 401 | 新开 PowerShell 重 `claude login` |
| 日志空白 | 任务可能没在跑。`Get-ScheduledTask Muselab; Start-ScheduledTask Muselab` |
| 脚本签名证书警告 | 用 `-ExecutionPolicy Bypass`（一次性），而非改系统 policy |
