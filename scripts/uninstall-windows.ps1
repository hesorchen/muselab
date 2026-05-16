# muselab — Windows uninstaller. Removes the scheduled task.
# Leaves .env, sessions\, your archive, and logs untouched.
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1

$ErrorActionPreference = "Stop"
$TaskName = "Muselab"

function Ok($s)   { Write-Host "  [+] $s" -ForegroundColor Green }
function Warn($s) { Write-Host "  [!] $s" -ForegroundColor Yellow }

Write-Host "muselab — uninstall (Windows)"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
  if ($task.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Ok "task stopped"
  }
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Ok "task removed: $TaskName"
} else {
  Warn "no scheduled task '$TaskName' — nothing to remove"
}

Write-Host
Write-Host "Note: .env, sessions\, your MUSELAB_ROOT, and $env:LOCALAPPDATA\muselab\logs"
Write-Host "are NOT touched. Delete the repo to fully remove muselab."
