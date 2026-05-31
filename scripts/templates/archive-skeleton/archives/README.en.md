# archives/

Original files — long-term references that don't need regular updates.

## What goes here

- Diplomas / transcripts / degree verifications
- Birth certificates / ID scans (**encrypted**)
- Historical checkup PDFs (originals already cited from `health/`)
- Old resumes / offer letters
- Important contracts, agreements, separation letters
- Family archives, photo metadata

## Relationship to other directories

`archives/` is the **filing cabinet** (read-only, rarely opened). The other
directories (`health/` / `work/` / ...) are the **working surface** (updated
often, Muse reads often). Workspace files reference originals via markdown link:

```markdown
See [2024-09 checkup original](archives/2024-09-checkup-xiehe.pdf) for details.
```

## Notes

- This directory likely contains highly sensitive info (national IDs,
  student IDs, contract amounts)
- Strongly recommend filesystem-level encryption for the whole muselab
  archive (macOS FileVault / Linux LUKS)
- Do NOT sync to public clouds (OneDrive / Google Drive / Dropbox)
- For remote backup: use [restic](https://restic.net) or
  [borg](https://borgbackup.org) with end-to-end encryption
