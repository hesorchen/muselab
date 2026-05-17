# muselab — one-shot Windows installer (Task Scheduler, at logon)
# Run from the repo root in PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
# (Default ExecutionPolicy 'Restricted' blocks unsigned scripts; -Bypass is per-invocation.)

$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $Repo

function Bold($s) { Write-Host $s -ForegroundColor White }
function Ok($s)   { Write-Host "  [+] $s" -ForegroundColor Green }
function Warn($s) { Write-Host "  [!] $s" -ForegroundColor Yellow }
function Err($s)  { Write-Host "  [x] $s" -ForegroundColor Red }
function Ask($q, $def="") {
  $prompt = if ($def) { "  $q [$def]" } else { "  $q" }
  $a = Read-Host $prompt
  if ([string]::IsNullOrWhiteSpace($a)) { return $def } else { return $a }
}

Bold "muselab — Windows installer"
Write-Host "  Repo: $Repo"
Write-Host

# ----- 1. Prerequisites ---------------------------------------------------
Bold "1/5  Checking prerequisites"

# PowerShell version check — Windows 10/11 ships 5.1, that's fine
if ($PSVersionTable.PSVersion.Major -lt 5) {
  Err "PowerShell $($PSVersionTable.PSVersion) is too old. Need >= 5.1 (ships with Win10+)."
  exit 1
}
Ok "PowerShell: $($PSVersionTable.PSVersion)"

# ExecutionPolicy — install-windows.ps1 itself runs because the user passed
# -ExecutionPolicy Bypass on the invocation. But the *Scheduled Task* we
# register will spawn cmd.exe → launcher.cmd → uv.exe, and uv itself refuses
# to run under Restricted policy ("PowerShell requires an execution policy in
# [Unrestricted, RemoteSigned, Bypass] to run uv"). So check the persistent
# CurrentUser policy and tell the user to fix it now, not after first reboot.
$policy = Get-ExecutionPolicy -Scope CurrentUser
if ($policy -eq "Restricted" -or $policy -eq "AllSigned" -or $policy -eq "Undefined") {
  Warn "Your CurrentUser ExecutionPolicy is '$policy'."
  Write-Host "      uv refuses to run under this policy. Run once:"
  Write-Host "        Set-ExecutionPolicy RemoteSigned -Scope CurrentUser"
  Write-Host "      Then re-open PowerShell and re-run this installer."
  $cont = Ask "Continue anyway (the service will fail at first boot)? [y/N]:" "N"
  if ($cont -notmatch "^[Yy]") { exit 1 }
} else {
  Ok "ExecutionPolicy: $policy (uv will work)"
}

# git — needed if user got here via `git clone` already; warn if missing so
# they know how to pull future updates
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) { Ok "git: $($git.Source)" }
else {
  Warn "git not found — you got here via zip download. For future updates use:"
  Write-Host "      winget install --id Git.Git -e   (then reopen PowerShell)"
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
  Err "uv not found. Install it first:"
  Write-Host "      powershell -c `"irm https://astral.sh/uv/install.ps1 | iex`""
  Write-Host "      Then open a new PowerShell window so PATH refreshes."
  exit 1
}
$UvPath = $uv.Source
Ok "uv: $UvPath"

# Port 8765 conflict check
$portInUse = $false
try {
  $listeners = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction Stop
  if ($listeners) { $portInUse = $true }
} catch { } # cmdlet not available on older PS, skip
if ($portInUse) {
  Err "Port 8765 is already in use:"
  $listeners | Format-Table LocalAddress, LocalPort, OwningProcess
  Write-Host "      Stop that process or set MUSELAB_PORT=<other> in .env."
  exit 1
}

$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
  Warn "claude CLI not found. Anthropic models won't work until you install it"
  Warn "  and run 'claude login'. Non-Claude providers will still work via API keys."
} else {
  Ok "claude CLI: $($claude.Source)"
}

# MCP runtimes — non-fatal warnings.
if (Get-Command uvx -ErrorAction SilentlyContinue) {
  Ok "uvx present — uv-based MCP servers available"
} else {
  Warn "uvx not found — uv-based MCP presets (fetch, git, time) won't run"
}
if (Get-Command npx -ErrorAction SilentlyContinue) {
  Ok "npx present — npm-based MCP servers available"
} else {
  Warn "npx not found — npm-based MCP presets (memory, sequential-thinking, filesystem) won't run"
  Warn "  install Node.js from https://nodejs.org"
}

# ----- 2. Python deps -----------------------------------------------------
Bold "2/5  Installing Python dependencies / 安装 Python 依赖 (uv sync, may take a few minutes first time)"
& $UvPath sync                       # no --quiet: user wants to see progress
if ($LASTEXITCODE -ne 0) { Err "uv sync failed"; exit 1 }
Ok "deps installed"

# ----- 3. .env ------------------------------------------------------------
Bold "3/5  Configuring .env / 写入 .env 配置"
$EnvPath = Join-Path $Repo ".env"
if (Test-Path $EnvPath) {
  Ok ".env already exists — keeping it as is"
} else {
  # 64 hex chars (256-bit token) — uses crypto-grade RNG
  $bytes = New-Object 'System.Byte[]' 32
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $Token = ($bytes | ForEach-Object { "{0:x2}" -f $_ }) -join ""

  Write-Host
  Write-Host "  Archive dir = where Muse can read/write (NEVER point at your user dir or C:\)"
  Write-Host "  档案目录 = Muse 能读写的地方（不要指向你的用户目录或 C:\ 整个盘）"
  $defArchive = Join-Path $env:USERPROFILE "muselab-archive"
  $Archive = Ask "Archive dir / 档案目录 (absolute path / 绝对路径):" $defArchive
  if (-not (Test-Path $Archive)) {
    try {
      New-Item -ItemType Directory -Path $Archive -Force | Out-Null
    } catch {
      Err "Cannot create $Archive — pick a path you have permissions for (e.g. under $env:USERPROFILE)"
      exit 1
    }
  }
  # Probe writability
  $probe = Join-Path $Archive ".muselab-write-test"
  try {
    Set-Content -Path $probe -Value "x" -ErrorAction Stop
    Remove-Item $probe -ErrorAction Stop
  } catch {
    Err "$Archive exists but isn't writable"
    exit 1
  }

  $now = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  @"
# Generated by install-windows.ps1 on $now
MUSELAB_TOKEN=$Token
MUSELAB_ROOT=$($Archive -replace '\\','/')
MUSELAB_HOST=127.0.0.1
MUSELAB_PORT=8765
MUSELAB_MODEL=claude-sonnet-4-6
"@ | Set-Content -Path $EnvPath -Encoding utf8     # utf8 — supports Chinese / CJK paths

  # Restrict ACL to current user (rough equivalent of `chmod 600`)
  $acl = Get-Acl $EnvPath
  $acl.SetAccessRuleProtection($true, $false)
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    "$env:USERNAME","FullControl","Allow")
  $acl.SetAccessRule($rule)
  Set-Acl -Path $EnvPath -AclObject $acl

  Ok ".env created (ACL restricted to $env:USERNAME)"
  Ok "  MUSELAB_ROOT = $Archive"
  Ok "  MUSELAB_TOKEN = $($Token.Substring(0,6))...$($Token.Substring($Token.Length - 4))  (full token in .env)"

  # First-time setup: drop a CLAUDE.md template + subdirectory skeleton, and
  # walk the user through a short intake to populate the holistic profile.
  $ClaudeMd = Join-Path $Archive "CLAUDE.md"
  if (-not (Test-Path $ClaudeMd)) {
    Write-Host
    Write-Host "  Muse is one assistant that helps you across health / career /"
    Write-Host "  investment / family / life simultaneously. It needs your basic"
    Write-Host "  profile and somewhere to find your real documents."
    Write-Host "  This is a 2-minute intake; you can skip any question (press Enter)."
    Write-Host
    Write-Host "  Muse 是一个同时管你健康 / 职业 / 投资 / 家庭 / 生活的助手。"
    Write-Host "  它需要先认识你（基本档案）+ 知道去哪里查你的真实材料。"
    Write-Host "  下面是 2 分钟的入门问题，任意一题可以直接回车跳过。"
    $DoSetup = Ask "Set up archive skeleton + CLAUDE.md now / 现在生成档案目录骨架 + CLAUDE.md？ [Y/n]:" "Y"
    if ($DoSetup -match "^[Yy]") {
      $skel = Join-Path $Repo "scripts\templates\archive-skeleton"
      foreach ($sub in @("health", "work", "money", "people", "notes", "archives")) {
        $sd = Join-Path $Archive $sub
        if (-not (Test-Path $sd)) {
          New-Item -ItemType Directory -Path $sd | Out-Null
          Copy-Item (Join-Path $skel "$sub\README.md") (Join-Path $sd "README.md")
        }
      }
      Ok "archive skeleton created under $Archive"

      Write-Host
      Write-Host "  --- Quick intake / 入门问答 (press Enter to skip any / 任意题回车跳过) ---"
      $iName   = Ask "How should Muse address you? / Muse 该怎么称呼你？" ""
      $iBirth  = Ask "Birth year (or age range) / 出生年份（或大致年龄段）:" ""
      $iCity   = Ask "Where do you live? / 你现在住在哪？" ""
      Write-Host "  What occupies most of your week? (study / job / freelance / care / retirement / ...)"
      Write-Host "  这一周你的主要时间花在哪？（学业 / 工作 / 自由职业 / 照护家人 / 退休 / 其他）"
      $iDoing  = Ask "" ""
      Write-Host "  One sentence about your life stage right now"
      Write-Host "  用一句话描述你当下的人生阶段"
      $iStage  = Ask "" ""
      $iGoal   = Ask "One main goal for this year / 这一年最想做成的一件事:" ""
      $iHealth = Ask "Top health concern right now (or 'none') / 当前最关心的健康问题（无则填 none）:" ""

      # Explicit -Encoding UTF8 — PS 5.1 on zh-CN Windows defaults Get-Content
      # to the system codepage (GBK), which corrupts the Chinese template.
      $tpl = Get-Content (Join-Path $Repo "scripts\templates\default-CLAUDE.md") -Raw -Encoding UTF8
      $tpl = $tpl -replace "%DATE%", (Get-Date -Format "yyyy-MM-dd")

      function Patch-Field($content, $label, $value) {
        if ([string]::IsNullOrWhiteSpace($value)) { return $content }
        $needle = "- ${label}："
        $idx = $content.IndexOf($needle)
        if ($idx -lt 0) { return $content }
        return $content.Substring(0, $idx + $needle.Length) + " $value" +
               $content.Substring($idx + $needle.Length)
      }
      $tpl = Patch-Field $tpl "称呼 / 名字（你希望 Muse 叫你什么）" $iName
      $tpl = Patch-Field $tpl "出生年份（年龄段就行，不必精确）"     $iBirth
      $tpl = Patch-Field $tpl "现在住在"                              $iCity
      $tpl = Patch-Field $tpl "我现在主要在做"                        $iDoing
      $tpl = Patch-Field $tpl "这一年最想做成的一件事"               $iGoal
      $tpl = Patch-Field $tpl "当前最关心的健康问题（如有）"         $iHealth
      if (-not [string]::IsNullOrWhiteSpace($iStage)) {
        $tpl = $tpl -replace [regex]::Escape("（如：「大三在准备保研」"),
                              ($iStage + "`n`n（如：「大三在准备保研」")
      }

      # UTF-8 WITHOUT BOM (PS 5.1 `-Encoding utf8` writes WITH BOM; some readers
      # render BOM as a leading 'ï»¿' artifact).
      [System.IO.File]::WriteAllText($ClaudeMd, $tpl, (New-Object System.Text.UTF8Encoding $false))
      Ok "CLAUDE.md -> $ClaudeMd (with your intake answers prefilled / 你回答的字段已预填)"
      Write-Host
      Write-Host "  Next steps / 接下来放点你的真实材料 (what fits depends on your life stage):"
      Write-Host "    * Health / 健康:  checkups / supplements / training logs -> $Archive\health\"
      Write-Host "                      体检 / 补剂 / 训练记录"
      Write-Host "    * Work   / 工作:  resume / portfolio / study material    -> $Archive\work\"
      Write-Host "                      简历 / 作品集 / 学业材料"
      Write-Host "    * Money  / 财务:  budget / holdings / loans / insurance  -> $Archive\money\"
      Write-Host "                      预算 / 持仓 / 学贷 / 保单"
      Write-Host "    * People / 人:    profiles of people you care about      -> $Archive\people\"
      Write-Host "                      你关心的人的资料"
      Write-Host "    * Open / 编辑 $ClaudeMd  to fill in any blank fields / 把剩下的空字段填完"
      Write-Host "  Each subdir has a README.md / 每个子目录里都有 README.md 说明放什么。"
      Write-Host "  Muse picks all of this up on your next chat - no restart needed."
      Write-Host "  下次 chat 时 Muse 会自动看到这些 - 不用重启服务。"
    }
  }
}

# ----- 4. Scheduled Task --------------------------------------------------
Bold "4/5  Registering Scheduled Task / 注册开机自启计划任务 (runs at logon, S4U)"
$TaskName  = "Muselab"
$LogDir    = Join-Path $env:LOCALAPPDATA "muselab\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

# Wrap the uv command in a small launcher .cmd file so the Scheduled Task can
# `Execute` it as a plain string — avoids the nested-quote escape hell that
# breaks when REPO_PATH or LOG_DIR contain spaces (Documents and Settings, etc).
$LauncherPath = Join-Path $Repo "scripts\muselab-launcher.cmd"
$launcherBody = @"
@echo off
cd /d "$Repo"
"$UvPath" run python -m backend.main >> "$LogDir\stdout.log" 2>> "$LogDir\stderr.log"
"@
Set-Content -Path $LauncherPath -Value $launcherBody -Encoding ascii

# LogonType=S4U — task has no interactive console (a conhost.exe window getting
# closed by the user used to kill uvicorn) and survives logout. Trade-off:
# registering S4U needs admin, so if we're not elevated, spawn one UAC prompt
# for just the Register-ScheduledTask step.
$User = $env:USERNAME
$RegisterScript = @"
`$ErrorActionPreference = 'Stop'
`$Action = New-ScheduledTaskAction -Execute '$LauncherPath' -WorkingDirectory '$Repo'
`$Trigger = New-ScheduledTaskTrigger -AtLogOn -User '$User'
`$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 365)
`$Principal = New-ScheduledTaskPrincipal -UserId '$User' -LogonType S4U
if (Get-ScheduledTask -TaskName '$TaskName' -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false }
Register-ScheduledTask -TaskName '$TaskName' -Action `$Action -Trigger `$Trigger -Settings `$Settings -Principal `$Principal | Out-Null
Start-ScheduledTask -TaskName '$TaskName'
"@

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($isAdmin) {
  Invoke-Expression $RegisterScript
} else {
  Write-Host "  Registering a system service needs admin rights — UAC will pop up once."
  Write-Host "  注册后台服务需要管理员权限，接下来会弹一次 UAC，请点 [是]。"
  $TmpReg = Join-Path $env:TEMP "muselab-register-$(Get-Random).ps1"
  # Write with UTF-8 BOM so PowerShell on Chinese-locale Windows reads it as UTF-8
  $bom  = [System.Text.Encoding]::UTF8.GetPreamble()
  $body = [System.Text.Encoding]::UTF8.GetBytes($RegisterScript)
  [System.IO.File]::WriteAllBytes($TmpReg, $bom + $body)
  try {
    $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList @(
      "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$TmpReg`""
    )
    if ($p.ExitCode -ne 0) {
      Err "elevated registration failed (exit $($p.ExitCode))"
      Err "  re-run this installer from an Admin PowerShell to register the task"
      Remove-Item $TmpReg -ErrorAction SilentlyContinue
      exit 1
    }
  } catch {
    Err "UAC denied or elevation cancelled — service NOT registered"
    Err "  UAC 被拒或取消 — 服务未注册。请以管理员身份重跑此脚本。"
    Remove-Item $TmpReg -ErrorAction SilentlyContinue
    exit 1
  }
  Remove-Item $TmpReg -ErrorAction SilentlyContinue
}
Ok "Scheduled Task '$TaskName' registered (S4U, hidden, restart-on-crash)"

# ----- 5. Sanity check ----------------------------------------------------
Bold "5/5  Sanity check / 启动自检 (up to 30s for first-boot SDK init)"
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  try {
    Invoke-WebRequest -Uri "http://127.0.0.1:8765/" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop | Out-Null
    $ok = $true
    Ok "muselab responding at http://localhost:8765 (took $($i+1)s)"
    break
  } catch {
    Start-Sleep -Seconds 1
  }
}
if (-not $ok) {
  Warn "didn't respond at http://localhost:8765 in 30s — give it more time or tail logs:"
  Warn "  Get-Content -Wait `"$LogDir\stderr.log`""
}

Write-Host
Bold "[OK] muselab installed / 安装完成"
Write-Host "  Open  / 打开    -> http://localhost:8765"
Write-Host "  Token / 登录口令 -> Select-String MUSELAB_TOKEN .env"
Write-Host
Write-Host "  Useful commands / 常用命令:"
Write-Host "    Get-ScheduledTask -TaskName Muselab    # check status / 查状态"
Write-Host "    Stop-ScheduledTask  -TaskName Muselab  # stop / 停止"
Write-Host "    Start-ScheduledTask -TaskName Muselab  # start / 启动"
Write-Host "    Get-Content -Wait `"$LogDir\stderr.log`"   # tail logs / 看日志"
Write-Host "    powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1  # uninstall / 卸载"
