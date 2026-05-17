# muselab upgrade (Windows) — bump claude-agent-sdk (Python) + claude CLI (npm)
# to latest, run tests, restart the Scheduled Task.
#   powershell -ExecutionPolicy Bypass -File scripts\upgrade.ps1
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $Repo

function Bold($s) { Write-Host $s -ForegroundColor White }
function Ok($s)   { Write-Host "  [+] $s" -ForegroundColor Green }
function Warn($s) { Write-Host "  [!] $s" -ForegroundColor Yellow }
function Err($s)  { Write-Host "  [x] $s" -ForegroundColor Red }

Bold "muselab upgrade — Python SDK + claude CLI"

# ----- Current versions -----
Bold "Current versions"
$curSdk = (& uv pip show claude-agent-sdk 2>$null | Select-String "^Version:" | ForEach-Object { ($_ -split ":")[1].Trim() })
Ok "claude-agent-sdk:  $($curSdk -join '')"

$claudeCmd = Get-Command claude -ErrorAction SilentlyContinue
if ($claudeCmd) {
  $curCli = (& claude --version 2>$null | Select-Object -First 1)
  Ok "claude CLI:        $curCli"
} else {
  Warn "claude CLI not installed — skip CLI upgrade. Install: npm install -g @anthropic-ai/claude-code"
}

# ----- Bump SDK -----
Bold "Bumping claude-agent-sdk (uv lock --upgrade-package)"
& uv lock --upgrade-package claude-agent-sdk
if ($LASTEXITCODE -ne 0) { Err "uv lock failed — aborting"; exit 1 }
& uv sync --frozen
$newSdk = (& uv pip show claude-agent-sdk 2>$null | Select-String "^Version:" | ForEach-Object { ($_ -split ":")[1].Trim() })
if ($curSdk -eq $newSdk) {
  Ok "claude-agent-sdk already at latest ($newSdk)"
} else {
  Ok "claude-agent-sdk: $curSdk → $newSdk"
}

# ----- Bump CLI -----
if (Get-Command npm -ErrorAction SilentlyContinue) {
  Bold "Bumping claude CLI"
  npm install -g "@anthropic-ai/claude-code@latest" 2>&1 | Select-Object -Last 3
  $newCli = (& claude --version 2>$null | Select-Object -First 1)
  Ok "claude CLI:        $curCli → $newCli"
} else {
  Warn "npm not installed — CLI upgrade skipped"
}

# ----- Smoke test -----
Bold "Running tests to catch SDK API breaks"
& uv run pytest tests/ -q 2>&1 | Select-Object -Last 3
if ($LASTEXITCODE -ne 0) {
  Err "tests FAILED — rollback recommended:"
  Err "  git checkout uv.lock pyproject.toml; uv sync"
  exit 1
}
Ok "tests pass against new SDK"

# ----- Restart service -----
Bold "Restarting muselab service"
Stop-ScheduledTask -TaskName Muselab -ErrorAction SilentlyContinue
Get-Process -Name python, uv -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName Muselab
Start-Sleep -Seconds 3
$state = (Get-ScheduledTask -TaskName Muselab).State
Ok "Scheduled Task state: $state"

Write-Host
Bold "✓ upgrade complete"
Write-Host "  Review the lock diff:    git diff uv.lock"
Write-Host "  Commit if happy:         git add uv.lock pyproject.toml; git commit -m 'deps: bump SDK'"
