---
name: markdown-formatter
description: "USE WHEN the user asks to clean up, reformat, or polish a markdown document. Normalizes headings, lists, tables, code fences, math delimiters, and Chinese punctuation. Returns the rewritten doc, not a description of changes."
---

# markdown-formatter — clean and polish markdown

## When to use

- "format this markdown"
- "clean up this doc"
- "make this look nicer"
- "convert this to standard markdown"

## Rules to apply (in order)

### 1. Heading hierarchy

- Exactly one `#` (H1) at top, or none. If the doc has multiple H1s, demote
  all but the first to H2.
- No skipped levels: H2 → H3 → H4. Never H2 → H4 directly.
- Add one blank line above every heading, none below.

### 2. Lists

- Use `-` for unordered, never `*` or `+`.
- Use `1.` `2.` `3.` for ordered (real numbers, not all `1.`).
- 2-space indent for nested lists.
- One blank line before and after the list block.

### 3. Tables

- Pipe-style with header divider: `| h1 | h2 |\n| --- | --- |\n| a | b |`
- Align with `| :--- |` (left), `| ---: |` (right), `| :---: |` (center) only
  when content benefits.
- One space padding inside cells.

### 4. Code

- Use fenced code blocks with language: ` ```python ` not just ` ``` `.
- For inline code, single backticks. No triple-backtick on the same line as text.
- Strip trailing whitespace inside code blocks.

### 5. Math

- Display math: `$$ … $$` on its own line, with blank line above/below.
- Inline math: `$…$`. Escape literal `$` as `\$`.
- LaTeX commands use lowercase: `\frac` not `\Frac`.

### 6. Chinese punctuation

In Chinese paragraphs, use full-width: `，。：？！""（）；……——`. Never
mix half-width into Chinese sentences. Inside code, parameter names, URLs,
or English clauses, keep half-width.

### 7. Spacing

- One blank line between paragraphs. Never more.
- Strip trailing spaces from every line.
- End the file with exactly one newline.

### 8. Links

- Inline form: `[text](url)`, not reference-style, unless the doc already uses
  reference style heavily.
- URLs naked in text → wrap them: `https://x.com` → `<https://x.com>` or
  `[x.com](https://x.com)`.

## What NOT to change

- Don't rewrite content for style or grammar — only structural / typographic
  cleanup.
- Don't add comments saying what you changed.
- Don't introduce new sections, TOCs, or footnotes.
- If the source has intentional formatting choices (e.g. all caps for
  emphasis, ASCII art), preserve them.

## Output

Return the rewritten markdown directly, nothing else. No diff, no
"here's the cleaned version" preamble.
