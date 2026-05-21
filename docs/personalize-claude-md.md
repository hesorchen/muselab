# Personalize CLAUDE.md

> [简体中文](personalize-claude-md_zh.md)

Muse is **one** assistant — it understands your body, what you do, your
money, the people you care about, your life, all at once. It doesn't
switch personas by topic; it always answers with the full background
in view.

To make that work, Muse reads the `CLAUDE.md` at the root of your archive
on every startup. This file is **your autobiographical brief** to Muse.

**Important**: the template is neutral across life stages — students,
employees, freelancers, full-time parents, retirees, founders — all fit.
Delete sections that don't apply to you; don't force-fill.

---

## How to generate this file

### Chat-driven intake — 👤 button (recommended)

The fastest path inside muselab itself. Click the **👤** icon in the
top bar. Muse opens a fresh session, seeds an empty CLAUDE.md from the
template if you don't have one yet, then walks you through the nine
sections one at a time, asking concrete questions and saving each answer
via `Edit`. Skip any section by saying "skip" or "no for now". Sensitive
sections (money / health) get a softer treatment — Muse asks
qualitative orders-of-magnitude first, exact numbers only when you offer
them.

This is the path the welcome card on first chat load points you at —
step 2 of the three-step orientation.

### Use the installer (alternative, less interactive)

The first run of `scripts/install-{linux,macos,windows}` asks whether you
want it to:

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

Skipping intake is fine — just press Enter on every question. You can
always finish the rest later via the 👤 chat flow.

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

Cross-domain decisions are where Muse earns its keep. Example:
**parent just had a cardiac stent + your cash flow this year + whether
you'll change jobs in the next few years → should you upgrade your
parent's Hong Kong health insurance?** A persona model splits this into 3
separate experts giving 3 disconnected pieces of advice; one assistant
can give a single coherent answer that actually fits you.

### Template is neutral

All phrasing, directory names, and intake questions avoid presupposing
your life stage:

- `work/`, not `career/` (works for students and retirees)
- `money/`, not `investment/` (covers budgets, student loans, pensions, FIRE)
- `people/`, not `family/` (also fits solo / unmarried / friend-only circles)
- "What you do most of the week", not "What's your job?"

### Behavioral promises are Muse's, not yours

The "health-related / money-related / study-or-work-related" rules in
§8 are promises about **how Muse answers** (e.g. cite the guideline for
health, don't diagnose), not requirements that you fill in every section.

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

- Strongly recommend filesystem encryption on your muselab archive
  (macOS FileVault / Linux LUKS / Windows BitLocker)
- **Do not** sync the archive to OneDrive / Google Drive / Dropbox or
  similar public clouds
- Information about other people can be redacted ("father" / "M" instead
  of real names)
- Passwords / national-ID / bank accounts belong in a dedicated password
  manager, **not** in the archive
- For remote backup, use [restic](https://restic.net) or
  [borg](https://borgbackup.org) with end-to-end encryption
