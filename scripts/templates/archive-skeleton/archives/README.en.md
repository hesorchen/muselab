# archives/ — original-file vault

## What goes here

**Original files** that don't need daily updating but should be kept
long-term:

- Diplomas / credential verifications / transcripts
- Birth certificate / national ID scans (**encrypted**)
- Old checkup PDFs (the originals referenced from `health/`)
- Old resumes / offer letters
- Important contracts / agreements
- Old employment certificates / leaving certificates
- Family archives / metadata for old photos

## How this relates to other directories

`archives/` is the **vault** — read-only, rarely opened.
The other directories (`health/`, `work/`, …) are the **workbench** —
updated often, read by Muse often.

When the workbench references an original, use a markdown link:

```markdown
See the original numbers in [2024-09 checkup PDF](archives/2024-09-checkup-clinic.pdf)
```

## Important notes

- This directory very likely contains high-sensitivity info: ID numbers
  / student numbers / contract amounts
- Strongly recommend filesystem-level encryption for the whole muselab
  archive (macOS FileVault / Linux LUKS)
- **Do not** sync to public clouds (OneDrive / Google Drive / Dropbox)
- For remote backup, use [restic](https://restic.net) or
  [borg](https://borgbackup.org) with end-to-end encryption
