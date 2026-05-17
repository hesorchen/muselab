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

Bold "muselab intake / 入门问答 — archive at $Archive"
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

# Explicit -Encoding UTF8 — PS 5.1 on zh-CN Windows defaults Get-Content to
# the system codepage (GBK), which corrupts the Chinese template into 乱码.
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

# Write UTF-8 WITHOUT BOM — PS 5.1's `-Encoding utf8` writes WITH BOM, which
# some readers render as a leading 'ï»¿' artifact. SDK + most editors handle
# either, but BOM-less is cleaner.
[System.IO.File]::WriteAllText($ClaudeMd, $tpl, (New-Object System.Text.UTF8Encoding $false))
Ok "CLAUDE.md updated / 已更新"
Write-Host
Write-Host "  Next / 下一步: open $ClaudeMd and fill any blanks."
Write-Host "  打开上面那个文件把空字段填完。Muse 下一次 chat 会自动加载（不用重启服务）。"
