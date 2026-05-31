---
name: citation-formatter
description: "USE WHEN the user asks to format references for an academic paper or research note. Converts messy DOIs, URLs, or pasted text into APA / IEEE / GB/T 7714 / BibTeX consistently. Verifies the cite key matches the work."
---

# citation-formatter — clean academic citations

## Supported formats

| Format | When |
|--------|------|
| **APA 7** | Social sciences, psychology, health |
| **IEEE** | Engineering, computer science |
| **GB/T 7714-2015** | Chinese academic papers |
| **BibTeX** | LaTeX manuscripts |
| **Vancouver** | Medicine |
| **Chicago** | Humanities |

Default to whichever the surrounding document uses. Ask only if the doc has no
existing citations.

## Source inputs you accept

- DOI: `10.1038/nature12345`
- arXiv ID: `arXiv:2304.12345`
- PubMed ID: `PMID: 12345678`
- ISBN
- Raw URL (publisher page)
- Loosely formatted text (author / year / title)

For DOI / arXiv / PubMed, prefer to fetch authoritative metadata (`WebFetch`
or MCP fetch) rather than guess. Never invent author lists.

## Format templates

### APA 7 (journal article)
> Lastname, F. M., & Lastname, F. M. (Year). Title. *Journal Name*,
> *Volume*(Issue), pp–pp. https://doi.org/...

### IEEE (journal article)
> [N] F. M. Lastname and F. M. Lastname, "Title," *Journal Name*, vol. N,
> no. N, pp. pp–pp, Mon. Year, doi: 10.xxxx/xxxxx.

### GB/T 7714-2015 (期刊文章)
> [N] 作者. 题名 [J]. 刊名, 年, 卷(期): 页码.

### BibTeX (key generated from first author + year + short title)
```bibtex
@article{lastnameYYYYshorttitle,
  author  = {Lastname, Firstname and Lastname, Firstname},
  title   = {Title of paper},
  journal = {Journal Name},
  volume  = {N},
  number  = {N},
  pages   = {pp--pp},
  year    = {YYYY},
  doi     = {10.xxxx/xxxxx},
}
```

## Quality checks (run before returning)

- [ ] All author names in the same name order (Last, First. or First Last)
- [ ] Year is 4 digits and plausible (1900–current)
- [ ] DOI present where one exists
- [ ] Journal name is the official full name (not the colloquial short form),
      unless the format calls for abbreviation
- [ ] Issue/volume/pages present when journal article
- [ ] No bracket-soup mixing styles within one list

## Bibkey rules (BibTeX)

`{firstAuthorLastnameLowercase}{Year}{firstSignificantWordLowercase}` — e.g.
`vaswani2017attention`. No accents, no punctuation. Make unique by appending
`a`, `b` if the same author has multiple in same year.

## Output

If asked for many cites, output a single block in the requested format. If
the user pasted a mixed bag, group by type (journal / book / web).

## Anti-patterns

- Faking author lists when the metadata is incomplete — say "(author list to
  confirm)" instead
- Mixing two formats in one bibliography
- Auto-shortening journal names without checking the venue's preferred form
- Writing access dates for journal articles (only needed for unstable web
  resources)
