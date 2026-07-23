# Personalize CLAUDE.md

> [简体中文](personalize-claude-md_zh.md)

Muse is a single assistant — it understands the user's health, daily
activities, finances, relationships, and life as a whole. It does not
switch personas by topic; every response is informed by the full context.

The Claude Agent SDK discovers `CLAUDE.md` from the active working directory.
It can hold both the user's autobiographical brief and durable instructions for
Muse's identity, response style, memory boundaries, and tool-use rules. muselab
does not inject another global or per-session system prompt.

**Important**: the template is neutral across life stages — students,
employees, freelancers, full-time parents, retirees, founders — all are
accommodated. Delete sections that do not apply; do not force-fill.

---

## How to generate this file

### Chat-driven intake with the `archive-curator` Skill (recommended)

Open **Skills** from the chat header, find `archive-curator`, click
**Try this**, and send the prefilled prompt. You can also tell Muse directly:
"Use the `archive-curator` skill to organize my archive and complete
`CLAUDE.md`."

The Skill scans the archive's current state and walks through the still-empty
sections of `CLAUDE.md` — one question at a time, saving each answer via
`Edit`. Any section can be skipped by saying "skip" or "not now". Sensitive
sections (money / health) are handled more gently: Muse asks for rough orders
of magnitude first, with exact figures only if offered voluntarily.

For an empty archive, a new chat also shows **Open CLAUDE.md and fill it
together**; that action starts the same workflow. If `CLAUDE.md` does not
exist, an orange **no archive** chip appears in the chat header; click it to
see the file location and setup instructions.

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
You can invoke `archive-curator` again from Skills at any time to complete the
remaining sections.

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

## Template structure

CLAUDE.md enters Muse's context on every conversation, so it should remain
short, explicit, and maintainable. The template contains a durable working
agreement followed by six personal-context sections.

| Section | What to put |
|---------|-------------|
| **Muse working agreement** | Identity / response style / memory and sources of truth / tool-use rules |
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

### Durable behavioral commitments belong in CLAUDE.md

Cross-domain, durable instructions belong in `CLAUDE.md`: Muse's identity,
response style, which files are authoritative, when to read source material,
and how to state uncertainty. Domain-specific constraints may remain in each
subdirectory's `README.md`, such as evidence rules for health records or risk
boundaries for financial material. Both are discovered natively by the Claude
Agent SDK; muselab does not maintain a second prompt layer.

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
