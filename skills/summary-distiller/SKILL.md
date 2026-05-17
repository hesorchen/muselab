---
name: summary-distiller
description: "USE WHEN the user pastes long text (article, paper, transcript, doc) and wants a summary. Picks the right summary shape (TL;DR / key points / structured / actionable) for the source type and length. Preserves numbers, names, dates verbatim."
---

# summary-distiller — useful summaries, not lossy ones

## Pick the shape based on source

| Source | Shape |
|--------|-------|
| News article | TL;DR + 3-5 key facts + "why it matters" |
| Academic paper | Background / Method / Results / Limitations |
| Meeting transcript | Decisions / Action items (with owner+due) / Open questions |
| Tutorial / how-to | Numbered steps, preserving every command verbatim |
| Long thread / comments | Stance map: "X argued A, Y countered B" |
| Strategy doc | Goals / Constraints / Decisions / Open issues |

Don't impose one shape on all sources. If unsure, ask: "What are you using
this for?" — that tells you the shape.

## Rules

### Preserve verbatim

These must be quoted exactly, never paraphrased:

- **Numbers** — "$1.2M", "37%", "3,400 users"
- **Dates** — preserve the original date format
- **Names** of people, products, places, papers
- **Quotes** explicitly attributed to a speaker
- **Commands / code** — copy character-for-character

### Compress aggressively elsewhere

- Cut adjectives and qualifiers ("very", "really", "essentially")
- Merge redundant statements
- Drop background the audience already knows

### Length targets

- TL;DR: 1 sentence, ≤25 words
- Key points: 3-5 bullets, ≤20 words each
- Structured summary: ≤25% of source word count
- Quotes section: only direct quotes that matter, max 5

## Common failure modes

- Summarizing the **structure** instead of the **content** ("the article
  discusses three points") — the user already saw the structure
- Hedging language ("the author suggests that perhaps…") that adds nothing
- Dropping the punchline because it's late in the source
- Inventing "key takeaways" that weren't actually in the text
- Misquoting numbers (a single digit error here ruins trust)

## Output template (default: news article)

```markdown
**TL;DR**: {one sentence}

**Key facts**:
- {bullet with verbatim number/name}
- ...

**Why it matters**: {one sentence on consequence or context}

**Source**: {title}, {publication}, {date}, [link]
```

## Anti-patterns

- "In summary…" / "To conclude…" filler
- Making the summary almost as long as the source
- Hallucinating a key point that "sounds important"
- Losing all the proper nouns to make it "smoother"
