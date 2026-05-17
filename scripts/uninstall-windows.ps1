# muselab — Windows uninstaller. Removes the scheduled task.
# Leaves .env, sessions\, your archive, and logs untouched.
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1

$ErrorActionPreference = "Stop"
$TaskName = "Muselab"

function Ok($s)   { Write-Host "  [+] $s" -ForegroundColor Green }
function Warn($s) { Write-Host "  [!] $s" -ForegroundColor Yellow }

Write-Host "muselab — uninstall (Windows)"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
  Warn "no scheduled task '$TaskName' — nothing to remove"
} else {
  # S4U tasks need admin to unregister (mirror install). If not elevated,
  # spawn one UAC prompt to do the stop + unregister.
  # Stop-ScheduledTask sends a signal to the launcher.cmd but on Windows that
  # often doesn't propagate to the uvicorn (python) subprocess — so add an
  # explicit Stop-Process pass to release the port + kill leftover children.
  $RemoveScript = @"
`$ErrorActionPreference = 'Stop'
`$t = Get-ScheduledTask -TaskName '$TaskName' -ErrorAction SilentlyContinue
if (`$t) {
  if (`$t.State -eq 'Running') { Stop-ScheduledTask -TaskName '$TaskName' -ErrorAction SilentlyContinue }
  Get-Process -Name python, uv -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1
  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false
}
"@
  $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if ($isAdmin) {
    Invoke-Expression $RemoveScript
  } else {
    Write-Host "  Removing the service needs admin rights — UAC will pop up once."
    Write-Host "  卸载后台服务需要管理员权限，接下来会弹一次 UAC，请点 [是]。"
    $TmpRm = Join-Path $env:TEMP "muselab-unregister-$(Get-Random).ps1"
    $bom  = [System.Text.Encoding]::UTF8.GetPreamble()
    $body = [System.Text.Encoding]::UTF8.GetBytes($RemoveScript)
    [System.IO.File]::WriteAllBytes($TmpRm, $bom + $body)
    try {
      $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$TmpRm`""
      )
      if ($p.ExitCode -ne 0) {
        Warn "elevated unregister failed (exit $($p.ExitCode)) — task may still exist"
      }
    } catch {
      Warn "UAC denied — task NOT removed; re-run from an Admin PowerShell"
    }
    Remove-Item $TmpRm -ErrorAction SilentlyContinue
  }
  Ok "task removed: $TaskName"
}

Write-Host
Write-Host "Note: .env, sessions\, your MUSELAB_ROOT, and $env:LOCALAPPDATA\muselab\logs"
Write-Host "are NOT touched. Delete the repo to fully remove muselab."
