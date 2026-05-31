---
name: mermaid-helper
description: "USE WHEN the user wants to draw an architecture diagram, sequence diagram, flowchart, ER diagram, or state machine. Picks the right mermaid type, writes correct syntax, validates before returning."
---

# mermaid-helper — draw the right diagram in mermaid

## Pick the right diagram type

| Want to show | Use |
|--------------|-----|
| Box-and-arrow architecture | `flowchart TD` |
| Who calls whom across time | `sequenceDiagram` |
| Step-by-step process | `flowchart LR` |
| Entity relationships (DB schema) | `erDiagram` |
| State transitions | `stateDiagram-v2` |
| Class structure | `classDiagram` |
| Gantt / project timeline | `gantt` |
| Pie chart of percentages | `pie title …` |
| Git branch flow | `gitGraph` |

If unsure, default to `flowchart TD` (top-down) for static structure and
`sequenceDiagram` for temporal interactions.

## Common gotchas

- **Spaces in node names** must be quoted: `A["Order Service"]`, not
  `A[Order Service]` (the latter works but breaks if the label has
  punctuation).
- **HTML in labels** needs `&` escapes: `A & B` → `A &amp; B`.
- **Edge labels**: `A -->|publishes event| B`, the pipes are required.
- **Subgraphs** need explicit `end`: `subgraph X\n  A\n  B\nend`.
- **classDef** for styling: `classDef warn fill:#fee,stroke:#900;` then
  `class A,B warn;`.
- **Sequence diagrams**: use `participant A as Alice` to give friendly names;
  use `Note over A: text` for inline annotations.

## Validation checklist (before returning)

- [ ] Every opening `[` `(` `{` has a matching close
- [ ] Every `subgraph` has a corresponding `end`
- [ ] Arrow syntax is consistent (`-->`, `---`, `==>`, `-.->`)
- [ ] Quoted labels for anything with spaces, parentheses, or special chars
- [ ] First line declares the diagram type

## Output

Return a fenced mermaid block:

````
```mermaid
flowchart TD
  …
```
````

After the diagram, one short paragraph explaining the key flow. Skip the
explanation if the diagram is trivial.

## Anti-patterns

- Don't make a flowchart with 50 nodes — split into multiple diagrams
- Don't use ASCII art "diagrams" inside the mermaid block
- Don't add color for decoration; only when it carries meaning
- Don't write a sequence diagram for static structure
