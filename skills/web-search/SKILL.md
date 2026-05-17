---
name: web-search
description: "USE WHEN the user asks for current facts, recent news, prices/rates, or anything that may have changed since the training cutoff. Plans the query, runs WebSearch / WebFetch (or an MCP fetch tool), and synthesizes a cited answer with date + source URL."
---

# web-search — fresh, cited answers

## When to use

Trigger when the question depends on **current state**:

- prices, rates, exchange rates, stock quotes, ETF NAV
- recent news (last 6 months)
- newly released product specs, library versions
- regulations / policies that may have changed
- comparison ("X vs Y") where one side could have shipped updates

Do NOT use for stable facts (math, history, well-settled science) — answer
directly. Web searches add latency and cost.

## How to run

1. **Plan the query**. Convert vague Chinese questions into 2–4 targeted English
   keywords. Strip filler words. Example: "美元兑人民币今天什么价" →
   `USD CNY exchange rate today`.

2. **Search**. Prefer in this order:
   - `WebSearch` tool (if available)
   - `mcp__fetch__fetch` (MCP fetch server) for known URLs
   - `WebFetch` for known docs / official sources

3. **Verify recency**. Open at least one source, confirm the publication date is
   within the relevant window. If only stale results, say so explicitly — do NOT
   pretend the answer is current.

4. **Synthesize**. Output:
   - One-sentence answer with the **number / fact**
   - Bullet of 2–3 source citations: `[Title](URL) — YYYY-MM-DD`
   - If sources disagree, surface the disagreement

## Failure modes to avoid

- Quoting yesterday's number as "today's"
- Citing a self-media blog when an official source exists (always prefer
  central-bank / exchange / vendor official pages)
- Doing zero searches but writing a confident-sounding paragraph from memory
- Not stating the date — the user can't tell if it's fresh

## Output template

```
{one-sentence answer with the number}

Sources:
- [{title}]({url}) — {YYYY-MM-DD}
- [{title}]({url}) — {YYYY-MM-DD}
```
