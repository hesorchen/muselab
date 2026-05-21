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

# Port pick + conflict check now happen at the .env step, after the user can
# choose a non-default port. Keep the earlier prereq block focused on tools.

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
  # Token: random 64-hex (256-bit) by default, but let the user pick a memorable
  # password if they prefer (>= 16 chars enforced — backend rejects shorter).
  Write-Host
  Write-Host "  Login token = your password for the web UI. Stored in .env + browser localStorage."
  Write-Host "  登录口令 = 浏览器登录用的密码。会写进 .env，浏览器记住后不用反复输入。"
  Write-Host "  Press Enter to auto-generate a 64-char random token (recommended)."
  Write-Host "  直接回车 = 随机生成 64 位（推荐）；想自己设密码就直接输入（≥16 字符）。"
  while ($true) {
    $TokenInput = Read-Host "  Token (Enter for random / 回车随机)"
    if ([string]::IsNullOrWhiteSpace($TokenInput)) {
      $bytes = New-Object 'System.Byte[]' 32
      [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
      $Token = ($bytes | ForEach-Object { "{0:x2}" -f $_ }) -join ""
      Ok "auto-generated 64-char random token / 已生成 64 位随机口令"
      break
    } elseif ($TokenInput.Length -lt 16) {
      Warn "token must be >= 16 chars / 口令至少 16 字符（输入了 $($TokenInput.Length) 个）"
      continue
    } else {
      $Token = $TokenInput
      Ok "using your token / 使用你提供的口令 ($($Token.Length) chars)"
      break
    }
  }

  Write-Host
  Write-Host "  HTTP port = where the web UI listens. Default 8765 is usually free."
  Write-Host "  HTTP 端口 = Web UI 监听端口。默认 8765 通常没被占用。"
  while ($true) {
    $PortInput = Read-Host "  Port / 端口 [8765]"
    if ([string]::IsNullOrWhiteSpace($PortInput)) { $Port = 8765; break }
    $parsed = 0
    if ([int]::TryParse($PortInput, [ref]$parsed) -and $parsed -ge 1024 -and $parsed -le 65535) {
      $Port = $parsed; break
    }
    Warn "port must be 1024-65535 / 端口范围 1024-65535"
  }
  # Check the chosen port is free. If a previous muselab install is holding
  # the port (python child of an existing Scheduled Task), offer to clean it
  # up in place rather than make the user run kill commands by hand.
  try {
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
    if ($listeners) {
      $owningPid = ($listeners | Select-Object -First 1).OwningProcess
      $proc = Get-Process -Id $owningPid -ErrorAction SilentlyContinue
      $existingTask = Get-ScheduledTask -TaskName "Muselab" -ErrorAction SilentlyContinue
      $looksLikeMuselab = ($proc -and ($proc.ProcessName -match "^(python|uv)$")) -and $existingTask

      if ($looksLikeMuselab) {
        Warn "Port $Port is held by an existing muselab install (PID $owningPid, $($proc.ProcessName))"
        Warn "  端口被已有的 muselab 占着 — 可以一键清理后继续"
        $clean = Ask "Clean it up and continue / 清理后继续? [Y/n]:" "Y"
        if ($clean -match "^[Yy]") {
          Stop-ScheduledTask -TaskName "Muselab" -ErrorAction SilentlyContinue
          Get-Process -Name python, uv -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
          Start-Sleep -Seconds 2
          # The new install registers a fresh task; remove the old one.
          # Unregister requires admin if the prior was S4U — try, fall back to letting the
          # admin UAC at the Scheduled Task step take care of it.
          try { Unregister-ScheduledTask -TaskName "Muselab" -Confirm:$false -ErrorAction Stop } catch {}
          # Re-check
          $still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
          if ($still) {
            Err "Cleanup didn't free the port. PID still holding: $(($still | Select-Object -First 1).OwningProcess)"
            Err "  Open Admin PowerShell and run: Stop-Process -Id <PID> -Force"
            exit 1
          }
          Ok "cleaned up — port $Port now free"
        } else {
          Err "Aborted by user. Re-run when the port is free."
          exit 1
        }
      } else {
        Err "Port $Port is already in use (PID $owningPid, $($proc.ProcessName))"
        Err "  端口被别的进程占着，不是 muselab — 请先停掉它或重跑选别的端口"
        $listeners | Format-Table LocalAddress, LocalPort, OwningProcess
        exit 1
      }
    }
  } catch { } # cmdlet missing on older PS — skip soft
  Ok "port $Port available / 端口 $Port 可用"

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
MUSELAB_PORT=$Port
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
    # Locale detection — Chinese template if Windows culture is zh-*, else English.
    $cultureName = (Get-Culture).Name
    if ($cultureName -like "zh*") {
      $MuseLocale = "zh"
      $MuseClaudeTpl = "scripts\templates\default-CLAUDE.md"
      $MuseReadmeSrc = "README.md"
    } else {
      $MuseLocale = "en"
      $MuseClaudeTpl = "scripts\templates\default-CLAUDE.en.md"
      $MuseReadmeSrc = "README.en.md"
    }
    Write-Host
    if ($MuseLocale -eq "zh") {
      Write-Host "  Muse 是一个同时管你健康 / 职业 / 投资 / 家庭 / 生活的助手。"
      Write-Host "  它需要先认识你（基本档案）+ 知道去哪里查你的真实材料。"
      Write-Host "  下面是 2 分钟的入门问题，任意一题可以直接回车跳过。"
      $IntakePrompt = "现在生成档案目录骨架 + CLAUDE.md？ [Y/n]:"
    } else {
      Write-Host "  Muse is one assistant that helps you across health / work /"
      Write-Host "  money / people / life simultaneously. It needs your basic"
      Write-Host "  profile and somewhere to find your real documents."
      Write-Host "  This is a 2-minute intake; you can skip any question (press Enter)."
      $IntakePrompt = "Set up archive skeleton + CLAUDE.md now? [Y/n]:"
    }
    $DoSetup = Ask $IntakePrompt "Y"
    if ($DoSetup -match "^[Yy]") {
      $skel = Join-Path $Repo "scripts\templates\archive-skeleton"
      foreach ($sub in @("health", "work", "money", "people", "notes", "archives")) {
        $sd = Join-Path $Archive $sub
        if (-not (Test-Path $sd)) {
          New-Item -ItemType Directory -Path $sd | Out-Null
          Copy-Item (Join-Path $skel "$sub\$MuseReadmeSrc") (Join-Path $sd "README.md")
        }
      }
      # Drop in concrete "_example-" template files so users see the shape
      # of a typical entry. Suffix (.en.md / .zh.md) stripped on destination.
      foreach ($exSrc in @("health\_example-checkup", "work\_example-project-log", "money\_example-budget")) {
        $src  = Join-Path $skel "$exSrc.$MuseLocale.md"
        $dest = Join-Path $Archive "$exSrc.md"
        if ((Test-Path $src) -and -not (Test-Path $dest)) {
          Copy-Item $src $dest
        }
      }
      Ok "archive skeleton created under $Archive"

      Write-Host
      if ($MuseLocale -eq "zh") {
        Write-Host "  --- 入门问答（任意题回车跳过）---"
        $iName   = Ask "Muse 该怎么称呼你？" ""
        $iBirth  = Ask "出生年份（或大致年龄段）:" ""
        $iCity   = Ask "你现在住在哪？" ""
        Write-Host "  这一周你的主要时间花在哪？（学业 / 工作 / 自由职业 / 照护家人 / 退休 / 其他）"
        $iDoing  = Ask "" ""
        Write-Host "  用一句话描述你当下的人生阶段"
        $iStage  = Ask "" ""
        $iGoal   = Ask "这一年最想做成的一件事:" ""
        $iHealth = Ask "当前最关心的健康问题（无则填 none）:" ""
      } else {
        Write-Host "  --- Quick intake (press Enter to skip any) ---"
        $iName   = Ask "How should Muse address you?" ""
        $iBirth  = Ask "Birth year (or age range):" ""
        $iCity   = Ask "Where do you live?" ""
        Write-Host "  What occupies most of your week? (study / job / freelance / care / retirement / ...)"
        $iDoing  = Ask "" ""
        Write-Host "  One sentence about your life stage right now"
        $iStage  = Ask "" ""
        $iGoal   = Ask "One main goal for this year:" ""
        $iHealth = Ask "Top health concern right now (or 'none'):" ""
      }

      # Explicit -Encoding UTF8 — PS 5.1 on zh-CN Windows defaults Get-Content
      # to the system codepage (GBK), which corrupts the Chinese template.
      $tpl = Get-Content (Join-Path $Repo $MuseClaudeTpl) -Raw -Encoding UTF8
      $tpl = $tpl -replace "%DATE%", (Get-Date -Format "yyyy-MM-dd")

      # Patch by full-line match — robust against label content (slashes,
      # parens, full-width punctuation, apostrophes in English labels).
      function Patch-Field($content, $label, $value) {
        if ([string]::IsNullOrWhiteSpace($value)) { return $content }
        $idx = $content.IndexOf($label)
        if ($idx -lt 0) { return $content }
        return $content.Substring(0, $idx + $label.Length) + " $value" +
               $content.Substring($idx + $label.Length)
      }
      if ($MuseLocale -eq "zh") {
        $tpl = Patch-Field $tpl "- 称呼 / 名字（你希望 Muse 叫你什么）：" $iName
        $tpl = Patch-Field $tpl "- 出生年份（年龄段就行，不必精确）："     $iBirth
        $tpl = Patch-Field $tpl "- 现在住在："                              $iCity
        $tpl = Patch-Field $tpl "- 我现在主要在做："                        $iDoing
        $tpl = Patch-Field $tpl "- 这一年最想做成的一件事："                $iGoal
        $tpl = Patch-Field $tpl "- 当前最关心的健康问题（如有）："         $iHealth
        $stageNeedle = "（如：「大三在准备保研」"
      } else {
        $tpl = Patch-Field $tpl "- Name / how you'd like Muse to address you:" $iName
        $tpl = Patch-Field $tpl "- Birth year (an age range is fine, no need for exact):" $iBirth
        $tpl = Patch-Field $tpl "- Where you currently live:" $iCity
        $tpl = Patch-Field $tpl "- What I'm mainly doing:" $iDoing
        $tpl = Patch-Field $tpl "- One thing I most want to make happen this year:" $iGoal
        $tpl = Patch-Field $tpl "- Top health concern right now (if any):" $iHealth
        $stageNeedle = '(e.g. "junior in college prepping for grad school"'
      }
      if (-not [string]::IsNullOrWhiteSpace($iStage)) {
        $tpl = $tpl -replace [regex]::Escape($stageNeedle),
                              ($iStage + "`n`n" + $stageNeedle)
      }

      # UTF-8 WITHOUT BOM (PS 5.1 `-Encoding utf8` writes WITH BOM; some readers
      # render BOM as a leading 'ï»¿' artifact).
      [System.IO.File]::WriteAllText($ClaudeMd, $tpl, (New-Object System.Text.UTF8Encoding $false))
      Ok "CLAUDE.md -> $ClaudeMd (intake answers prefilled)"
      Write-Host
      if ($MuseLocale -eq "zh") {
        Write-Host "  接下来放点真实材料（按你的人生阶段选）:"
        Write-Host "    * 健康:  体检 / 补剂 / 训练记录       -> $Archive\health\"
        Write-Host "    * 工作:  简历 / 作品集 / 学业材料     -> $Archive\work\"
        Write-Host "    * 财务:  预算 / 持仓 / 学贷 / 保单    -> $Archive\money\"
        Write-Host "    * 人:    关心的人的资料               -> $Archive\people\"
        Write-Host "    * 编辑 $ClaudeMd 把剩下的空字段填完"
        Write-Host "  每个子目录里都有 README.md 说明放什么。"
        Write-Host "  下次 chat 时 Muse 会自动看到这些 - 不用重启服务。"
      } else {
        Write-Host "  Next steps (what fits depends on your life stage):"
        Write-Host "    * Health:  checkups / supplements / training logs -> $Archive\health\"
        Write-Host "    * Work:    resume / portfolio / study material    -> $Archive\work\"
        Write-Host "    * Money:   budget / holdings / loans / insurance  -> $Archive\money\"
        Write-Host "    * People:  profiles of people you care about      -> $Archive\people\"
        Write-Host "    * Open $ClaudeMd and fill in any blank fields"
        Write-Host "  Each subdir has a README.md explaining what to put there."
        Write-Host "  Muse picks all of this up on your next chat - no restart needed."
      }
    }
  }
}

# Reload port from .env (may be different from default 8765 if user picked,
# or from a pre-existing .env in the "keeping as is" branch).
$envText = Get-Content $EnvPath -Raw -Encoding utf8
$Port = if ($envText -match "MUSELAB_PORT=(\d+)") { [int]$matches[1] } else { 8765 }

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
    Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop | Out-Null
    $ok = $true
    Ok "muselab responding at http://localhost:$Port (took $($i+1)s)"
    break
  } catch {
    Start-Sleep -Seconds 1
  }
}
if (-not $ok) {
  Warn "didn't respond at http://localhost:$Port in 30s — give it more time or tail logs:"
  Warn "  Get-Content -Wait `"$LogDir\stderr.log`""
}

Write-Host
Bold "[OK] muselab installed / 安装完成"
Write-Host "  Open / 打开:   http://localhost:$Port"
Write-Host
# Read token back from .env so the user can copy without opening files.
$tokenNow = (Select-String "^MUSELAB_TOKEN=(.+)" $EnvPath -ErrorAction SilentlyContinue).Matches.Groups[1].Value
if ($tokenNow) {
  Write-Host "  Login token / 登录口令（复制贴进浏览器登录框）:"
  Write-Host "    $tokenNow" -ForegroundColor Cyan
  Write-Host "  Saved at / 也存在: $EnvPath"
  Write-Host "    再查: Select-String MUSELAB_TOKEN .env"
}
Write-Host
Write-Host "  Useful commands / 常用命令:"
Write-Host "    Get-ScheduledTask -TaskName Muselab    # check status / 查状态"
Write-Host "    Start-ScheduledTask -TaskName Muselab  # start / 启动"
Write-Host "    # Reliable stop+restart (Stop-ScheduledTask alone often leaves the python child alive):"
Write-Host "    # 可靠重启（Stop-ScheduledTask 经常杀不掉 python 子进程）："
Write-Host "    Stop-ScheduledTask -TaskName Muselab; Get-Process python,uv -EA SilentlyContinue | Stop-Process -Force; Start-ScheduledTask -TaskName Muselab"
Write-Host "    Get-Content -Wait `"$LogDir\stderr.log`"   # tail logs / 看日志"
Write-Host "    powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1  # uninstall / 卸载"

# Auto-open the URL in the user's default browser. Skip via MUSELAB_NO_BROWSER=1.
# Token NOT in URL — it would land in browser history; user pastes from the
# highlighted line above.
if (-not $env:MUSELAB_NO_BROWSER) {
  Write-Host
  Write-Host "  Opening browser… (set MUSELAB_NO_BROWSER=1 to skip)"
  Start-Process "http://localhost:$Port" -ErrorAction SilentlyContinue
}
