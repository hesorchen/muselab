# Personalize CLAUDE.md

> [简体中文](personalize-claude-md_zh.md)

Muse is a single assistant — it understands the user's health, daily
activities, finances, relationships, and life as a whole. It does not
switch personas by topic; every response is informed by the full context.

To support this, Muse reads the `CLAUDE.md` at the root of the archive
on every startup. This file is the user's autobiographical brief to Muse.

**Important**: the template is neutral across life stages — students,
employees, freelancers, full-time parents, retirees, founders — all are
accommodated. Delete sections that do not apply; do not force-fill.

---

## How to generate this file

### Chat-driven intake — "Organize archive" button (recommended)

Click the **Organize archive** button in the top bar (curator mode).
Muse opens a session, scans the archive's current state, and walks through
the still-empty sections of `CLAUDE.md` — one question at a time, saving
each answer via `Edit`. Any section can be skipped by saying "skip" or
"not now". Sensitive sections (money / health) are handled more gently —
Muse asks for qualitative orders of magnitude first, with exact figures
only if offered voluntarily.

If `CLAUDE.md` doesn't exist yet, an orange "no archive" chip appears in
the top bar; clicking it lands on the same workflow.

### Use the installer (alternative, less interactive)

The first run of `scripts/install-{linux,macos}` offers to:

1. Create 6 sub-directories under your archive (`health/` `work/` `money/`
   `people/` `notes/` `archives/`), each with its own README
2. Ask 7 open-ended intake questions:
   - How to call you
   - Birth year (or age range)
   - Current city
   - What you do most of the week (study / work / freelance / care / retired / …)
   - One sentence about your current life stage
   - Main goal this year
   - Health concern you're most focused on right now
3. Patch the answers into the corresponding fields in CLAUDE.md
4. Tell you which originals to drop into which directory next

Skipping intake is acceptable — press Enter on every question to continue.
The remaining sections can be completed any time via the top-bar
"Organize archive" button.

### Manual

```bash
cp scripts/templates/default-CLAUDE.en.md ~/muselab-archive/CLAUDE.md
# portable in-place edit (GNU and BSD/macOS sed differ on -i)
sed -e "s/%DATE%/$(date +%Y-%m-%d)/" ~/muselab-archive/CLAUDE.md > ~/muselab-archive/CLAUDE.md.tmp \
  && mv ~/muselab-archive/CLAUDE.md.tmp ~/muselab-archive/CLAUDE.md
# subdirectory skeleton — copy each subdir, keep only the English README
# (each skeleton subdir ships both README.md (zh) and README.en.md (en))
for sub in scripts/templates/archive-skeleton/*/; do
  name=$(basename "$sub")
  mkdir -p ~/muselab-archive/"$name"
  cp "$sub/README.en.md" ~/muselab-archive/"$name"/README.md
done
```

---

## The 6 sections of the template

CLAUDE.md is a deliberately bare skeleton — it's loaded into Muse's
context on every conversation, so it carries no filling instructions or
option lists, only fields.

| Section | What to put |
|---------|-------------|
| **1. Who I am** | Name / birth year / lives in / languages / household |
| **2. What I'm mainly doing** | Life stage (one line) / main activity / how long / goal this year / big decision this year |
| **3. Money** | Income source / asset-liability scale / current focus / risk tolerance |
| **4. Body** | General / last checkup / medications / exercise / top concern / sleep |
| **5. People I care about** | Key relationships / who needs attention now / recent events |
| **6. What's on my mind** | Biggest worry / active projects / things to start |

The archive subdirectories (`health/` / `work/` / `money/` / `people/` /
`notes/` / `archives/`) are reached via Muse's Read tool on demand — no
index needed in CLAUDE.md. Each subdir's `README.md` describes what
belongs there and the constraints Muse follows in that domain (no
diagnosis in health, no price predictions in money, etc.).

---

## Subdirectory skeleton (6, all general-purpose)

| Directory | What it holds | Student | Employed | Freelance | Full-time parent | Retired |
|-----------|---------------|---------|----------|-----------|------------------|---------|
| `health/` | Body-related | School physical | Annual checkup | same | Self + kids | Chronic-disease mgmt |
| `work/` | What you do | Papers / grad-school apps | Resume / projects | Portfolio / clients | Childcare logs | Current activities |
| `money/` | Money | Monthly budget | Income & savings | Tax / emergency fund | Household budget | Cash flow |
| `people/` | People you care about | Parents / friends | Partner / coworkers / parents | same | Spouse / kids / in-laws | Spouse / kids / old friends |
| `notes/` | Miscellaneous | general | general | general | general | general |
| `archives/` | Original files | general | general | general | general | general |

Each directory ships with its own `README.md` containing stage-specific
suggestions.

---

## Key design principles

### Muse is one assistant, not multiple personas

Cross-domain decisions are where Muse is most valuable. Example:
**a parent recently underwent cardiac stent placement + the user's cash
flow this year + the possibility of changing jobs in the coming years →
should the parent's Hong Kong health insurance be upgraded?** A persona
model splits this into three separate experts providing three disconnected
answers; a unified assistant can give a single coherent response that
accounts for all the relevant factors.

### Template is neutral

All phrasing, directory names, and intake questions avoid presupposing
any particular life stage:

- `work/`, not `career/` (applicable to students and retirees as well)
- `money/`, not `investment/` (covers budgets, student loans, pensions, FIRE)
- `people/`, not `family/` (also fits solo / unmarried / friend-only circles)
- "What you do most of the week", not "What's your job?"

### Behavioral commitments live with each subdirectory, not in CLAUDE.md

Domain-specific constraints (health: cite guidelines, no diagnosis; money:
no price predictions; etc.) live in each subdirectory's `README.md`
(e.g. `health/README.md`, `money/README.md`). Muse reads those when it
enters the corresponding directory. CLAUDE.md itself stays a bare-bones
fact sheet about you.

---

## Maintenance cadence

| Trigger | What to update |
|---------|----------------|
| After a checkup | §4; PDF into `health/` |
| Study / job / business change | §2 |
| Major financial change | §3; record into `money/` |
| Major change in someone you care about | §5 |
| Anytime | Half-yearly sweep — delete anything no longer true |

---

## Privacy / security

- Filesystem encryption is strongly recommended for the muselab archive
  (macOS FileVault / Linux LUKS).
- Do not sync the archive to OneDrive / Google Drive / Dropbox or
  similar public cloud services.
- Information about other people can be redacted ("father" / "M" instead
  of real names).
- Passwords / national ID numbers / bank accounts belong in a dedicated
  password manager, not in the archive.
- For remote backup, use [restic](https://restic.net) or
  [borg](https://borgbackup.org) with end-to-end encryption.
