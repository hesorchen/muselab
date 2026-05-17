# muselab doctor (Windows) — same checks as scripts/doctor.sh
# Usage: powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1
$ErrorActionPreference = "Continue"   # don't stop on first failure
$Repo = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $Repo

function Bold($s) { Write-Host $s -ForegroundColor White }
function Ok($s)   { Write-Host "  [+] $s" -ForegroundColor Green }
function Warn($s) { Write-Host "  [!] $s" -ForegroundColor Yellow; $script:warn++ }
function Err($s)  { Write-Host "  [x] $s" -ForegroundColor Red;    $script:fail++ }
function Note($s) { Write-Host "      $s" }

$script:fail = 0; $script:warn = 0

Bold "muselab doctor — $(Get-Date)"
Write-Host "  Repo: $Repo"
Write-Host

Bold "1. Prerequisites"
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) { Ok "uv: $(& uv --version)" } else { Err "uv not found — install from https://astral.sh/uv" }
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
  Ok "claude CLI: $(& claude --version | Select-Object -First 1)"
  if (Test-Path "$env:USERPROFILE\.claude\.credentials.json") { Ok "  Pro OAuth present" }
  else { Warn "  no Pro OAuth — run 'claude login' for Anthropic models" }
} else { Warn "claude CLI missing — Anthropic models unavailable" }
foreach ($r in @("uvx","npx")) {
  if (Get-Command $r -ErrorAction SilentlyContinue) { Ok "$r present" }
  else { Warn "$r missing — some MCP presets won't run" }
}

Write-Host
Bold "2. Configuration"
$envPath = Join-Path $Repo ".env"
if (Test-Path $envPath) {
  Ok ".env present"
  $envText = Get-Content $envPath -Raw
  $token = if ($envText -match "MUSELAB_TOKEN=(\S+)") { $matches[1] } else { "" }
  $root  = if ($envText -match "MUSELAB_ROOT=(\S+)")  { $matches[1] } else { "" }
  $port  = if ($envText -match "MUSELAB_PORT=(\S+)")  { $matches[1] } else { "8765" }
  if (-not $token) { Err "MUSELAB_TOKEN missing in .env" }
  elseif ($token.Length -lt 16) { Err "MUSELAB_TOKEN too short ($($token.Length) chars; need >=16)" }
  else { Ok "MUSELAB_TOKEN set ($($token.Substring(0,4))...$($token.Substring($token.Length-4)), $($token.Length) chars)" }
  if (-not $root) { Err "MUSELAB_ROOT missing in .env" }
  elseif (-not (Test-Path $root)) { Err "MUSELAB_ROOT=$root but dir doesn't exist" }
  else {
    Ok "MUSELAB_ROOT = $root"
    if (Test-Path (Join-Path $root "CLAUDE.md")) {
      $lines = (Get-Content (Join-Path $root "CLAUDE.md") | Measure-Object -Line).Lines
      Ok "  CLAUDE.md present ($lines lines)"
    } else {
      Warn "  no CLAUDE.md at $root — run scripts\intake.ps1 to add"
    }
    foreach ($sub in @("health","work","money","people","notes","archives")) {
      $sd = Join-Path $root $sub
      if (Test-Path $sd) { Ok "  $sub\ present" } else { Note "  $sub\ missing" }
    }
  }
} else {
  Err ".env not found — run scripts\install-windows.ps1 first"
}

Write-Host
Bold "3. Python deps"
& uv sync --frozen --no-progress *>$null 2>&1
if ($LASTEXITCODE -eq 0) {
  Ok "uv sync --frozen passes"
} else {
  & uv sync --no-progress *>$null 2>&1
  if ($LASTEXITCODE -eq 0) { Warn "uv.lock out of sync — re-run scripts\install-windows.ps1" }
  else { Err "uv sync failed — see: uv sync" }
}

Write-Host
Bold "4. Service"
$task = Get-ScheduledTask -TaskName "Muselab" -ErrorAction SilentlyContinue
if ($task) {
  Ok "Scheduled Task 'Muselab' registered (state: $($task.State))"
} else {
  Warn "Scheduled Task not installed — run scripts\install-windows.ps1"
}

Write-Host
Bold "5. HTTP probe"
$url = "http://127.0.0.1:$($port)/"
try {
  Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop | Out-Null
  Ok "$url responds 200"
  if ($token) {
    try {
      Invoke-WebRequest -Uri "http://127.0.0.1:$($port)/api/chat/context-info" `
        -Headers @{"X-Auth-Token"=$token} -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop | Out-Null
      Ok "token works — /api/chat/context-info OK"
    } catch { Err "token rejected — TOKEN in .env doesn't match running process" }
  }
} catch {
  Warn "$url not responding — service may not be up"
}

Write-Host
Bold "6. Provider keys"
foreach ($entry in @(@{k="DEEPSEEK_API_KEY";n="DeepSeek"}, @{k="ZHIPUAI_API_KEY";n="GLM"}, @{k="MINIMAX_API_KEY";n="MiniMax"})) {
  $val = if ($envText -match "$($entry.k)=(\S+)") { $matches[1] } else { "" }
  if ($val) { Ok "$($entry.n) key configured ($($val.Substring(0,4))...$($val.Substring($val.Length-4)))" }
  else { Note "$($entry.n) key not set (optional)" }
}

Write-Host
Bold "Summary"
Write-Host "  Failures: $($script:fail)    Warnings: $($script:warn)"
if ($script:fail -gt 0) { Err "doctor found blocking problems"; exit 1 }
elseif ($script:warn -gt 0) { Warn "doctor finished with warnings"; exit 0 }
else { Ok "all checks passed"; exit 0 }
