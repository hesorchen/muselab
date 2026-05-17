# muselab intake (Windows) — (re)run the 7-question profile setup.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\intake.ps1
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

$envPath = Join-Path $Repo ".env"
if (-not (Test-Path $envPath)) { Err ".env not found — run scripts\install-windows.ps1 first"; exit 1 }
$envText = Get-Content $envPath -Raw
if ($envText -notmatch "MUSELAB_ROOT=(\S+)") { Err "MUSELAB_ROOT missing in .env"; exit 1 }
$Archive = $matches[1]
if (-not (Test-Path $Archive)) { Err "MUSELAB_ROOT=$Archive but dir doesn't exist"; exit 1 }

Bold "muselab intake — archive at $Archive"
Write-Host

$ClaudeMd = Join-Path $Archive "CLAUDE.md"
if (Test-Path $ClaudeMd) {
  Warn "$ClaudeMd already exists"
  $r = Ask "Overwrite with a fresh template? (existing -> CLAUDE.md.bak) [y/N]:" "N"
  if ($r -notmatch "^[Yy]") {
    Write-Host "  Aborted. Edit $ClaudeMd manually if you just want to tweak it."
    exit 0
  }
  Copy-Item $ClaudeMd "$ClaudeMd.bak"
  Ok "backed up to CLAUDE.md.bak"
}

$skel = Join-Path $Repo "scripts\templates\archive-skeleton"
foreach ($sub in @("health","work","money","people","notes","archives")) {
  $sd = Join-Path $Archive $sub
  if (-not (Test-Path $sd)) {
    New-Item -ItemType Directory -Path $sd -Force | Out-Null
    Copy-Item (Join-Path $skel "$sub\README.md") (Join-Path $sd "README.md")
    Ok "created $sub\"
  }
}

Write-Host
Write-Host "  --- Quick intake (press Enter to skip any) ---"
$iName   = Ask "How should Muse address you?" ""
$iBirth  = Ask "Birth year (or just an age range):" ""
$iCity   = Ask "Where do you live?" ""
$iDoing  = Ask "What occupies most of your week? (study / job / freelance / care / retirement / ...)" ""
$iStage  = Ask "One sentence about your life stage right now:" ""
$iGoal   = Ask "One main goal for this year:" ""
$iHealth = Ask "Top health concern right now (or 'none'):" ""

$tpl = Get-Content (Join-Path $Repo "scripts\templates\default-CLAUDE.md") -Raw
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

Set-Content -Path $ClaudeMd -Value $tpl -Encoding utf8
Ok "CLAUDE.md updated"
Write-Host
Write-Host "  Next: open $ClaudeMd and fill any blanks. Muse picks it up on next chat (no restart)."
