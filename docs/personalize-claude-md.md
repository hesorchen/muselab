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

### Chat-driven intake — 👤 button (recommended)

The fastest method within muselab. Click the **👤** icon in the top bar.
Muse opens a fresh session, creates an empty `CLAUDE.md` from the template
if one does not yet exist, then guides through each of the nine sections
with concrete questions, saving each answer via `Edit`. Any section can be
skipped by saying "skip" or "not now". Sensitive sections (money / health)
are handled more gently — Muse asks for qualitative orders of magnitude
first, with exact figures only if offered voluntarily.

This is the path indicated by the welcome card on the first chat load —
step 2 of the three-step orientation.

### Use the installer (alternative, less interactive)

The first run of `scripts/install-{linux,macos,windows}` offers to:

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
The remaining sections can be completed at any time via the 👤 chat flow.

### Manual

```bash
cp scripts/templates/default-CLAUDE.md ~/muselab-archive/CLAUDE.md
sed -i "s/%DATE%/$(date +%Y-%m-%d)/" ~/muselab-archive/CLAUDE.md
# subdirectory skeleton
cp -r scripts/templates/archive-skeleton/* ~/muselab-archive/
```

---

## The 9 sections of the template

| Section | What to put |
|---------|-------------|
| **1. Who I am** | Name / age / city / language / household |
| **2. What I do most of the week** | One-line life stage + status by role (student / employed / self-employed / caregiving / retired / …) |
| **3. Money** | Income source / scale / main concerns (no stage gating) |
| **4. Body** | General health / checkups / medications / concerns |
| **5. People I care about** | Not just family — partner / parents / friends / anyone important |
| **6. What's on my mind** | Current worries / things you're doing / things you want to do |
| **7. What's in the archive** | Index of the 6 sub-directories + current key materials |
| **8. How Muse collaborates with me** | Behavioral promises (neutral; apply to everyone) |
| **9. What I maintain myself** | Which section to update when life changes |

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

### Behavioral commitments belong to Muse, not the user

The "health-related / money-related / study-or-work-related" rules in
§8 are commitments about **how Muse responds** (e.g. cite guidelines for
health topics, do not diagnose), not requirements to fill in every section.

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
  (macOS FileVault / Linux LUKS / Windows BitLocker).
- Do not sync the archive to OneDrive / Google Drive / Dropbox or
  similar public cloud services.
- Information about other people can be redacted ("father" / "M" instead
  of real names).
- Passwords / national ID numbers / bank accounts belong in a dedicated
  password manager, not in the archive.
- For remote backup, use [restic](https://restic.net) or
  [borg](https://borgbackup.org) with end-to-end encryption.
