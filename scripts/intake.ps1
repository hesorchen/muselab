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
$envText = Get-Content $envPath -Raw -Encoding UTF8
if ($envText -notmatch "MUSELAB_ROOT=(\S+)") { Err "MUSELAB_ROOT missing in .env"; exit 1 }
$Archive = $matches[1]
if (-not (Test-Path $Archive)) { Err "MUSELAB_ROOT=$Archive but dir doesn't exist"; exit 1 }

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

if ($MuseLocale -eq "zh") { Bold "muselab 入门问答 — archive 在 $Archive" }
else { Bold "muselab intake — archive at $Archive" }
Write-Host

$ClaudeMd = Join-Path $Archive "CLAUDE.md"
if (Test-Path $ClaudeMd) {
  Warn "$ClaudeMd already exists"
  if ($MuseLocale -eq "zh") {
    $promptOverwrite = "覆盖为新模板？（旧内容会备份到 CLAUDE.md.bak） [y/N]:"
  } else {
    $promptOverwrite = "Overwrite with a fresh template? (existing -> CLAUDE.md.bak) [y/N]:"
  }
  $r = Ask $promptOverwrite "N"
  if ($r -notmatch "^[Yy]") {
    if ($MuseLocale -eq "zh") {
      Write-Host "  已取消。如果只是想小改，直接编辑 $ClaudeMd。"
    } else {
      Write-Host "  Aborted. Edit $ClaudeMd manually if you just want to tweak it."
    }
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
    Copy-Item (Join-Path $skel "$sub\$MuseReadmeSrc") (Join-Path $sd "README.md")
    Ok "created $sub\"
  }
}
# Drop in concrete "_example-" template files so users see the shape of
# a typical entry. Suffix (.en.md / .zh.md) stripped on destination.
foreach ($exSrc in @("health\_example-checkup", "work\_example-project-log", "money\_example-budget")) {
  $src  = Join-Path $skel "$exSrc.$MuseLocale.md"
  $dest = Join-Path $Archive "$exSrc.md"
  if ((Test-Path $src) -and -not (Test-Path $dest)) {
    Copy-Item $src $dest
    Ok "added $exSrc.md (example template)"
  }
}

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

# Explicit -Encoding UTF8 — PS 5.1 on zh-CN Windows defaults Get-Content to
# the system codepage (GBK), which corrupts the Chinese template into 乱码.
$tpl = Get-Content (Join-Path $Repo $MuseClaudeTpl) -Raw -Encoding UTF8
$tpl = $tpl -replace "%DATE%", (Get-Date -Format "yyyy-MM-dd")

# Full-line label match — robust against any content in the label.
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

# Write UTF-8 WITHOUT BOM — PS 5.1's `-Encoding utf8` writes WITH BOM, which
# some readers render as a leading 'ï»¿' artifact. SDK + most editors handle
# either, but BOM-less is cleaner.
[System.IO.File]::WriteAllText($ClaudeMd, $tpl, (New-Object System.Text.UTF8Encoding $false))
Ok "CLAUDE.md updated"
Write-Host
if ($MuseLocale -eq "zh") {
  Write-Host "  下一步: 打开 $ClaudeMd 把空字段填完。"
  Write-Host "  Muse 下一次 chat 会自动加载（不用重启服务）。"
} else {
  Write-Host "  Next: open $ClaudeMd and fill in the blanks."
  Write-Host "  Muse picks it up on the next chat — no restart needed."
}
