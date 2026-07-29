# Long-term memory

> [中文](memory_zh.md)

MuseLab's optional long-term memory stores durable business context,
preferences, decisions, and agent experience. It is disabled by default and is
not a repository-wide file RAG feature.

The canonical SQLite Registry retains evidence, Episodes, provenance,
versions, conflicts, review state, jobs, and audit events. Qdrant or pgvector
is only a rebuildable dense index. Retrieval fuses SQLite FTS, dense similarity,
metadata, authority, and confidence, with an optional reranker.

Configure Memory under Settings with:

1. a configured chat model for Dreamer and Verifier;
2. an OpenAI-compatible embedding endpoint;
3. Qdrant or PostgreSQL + pgvector;
4. an optional reranker.

MuseLab probes every required capability before an enabled configuration is
saved. `off` has no chat overhead, `shadow` forms reviewable candidates without
recall, and `active` enables bounded hybrid recall. The default 250 ms soft
deadline and fail-soft behavior keep provider failures out of the chat path.

The Memory Center exposes memories, sources, Episodes, reflections, conflicts,
jobs, recall traces, and Skill candidates. The brain button beside a chat
message is a deterministic confirmed-memory action; natural-language
"remember/correct/forget" classification is not required. Corrections retain a
`supersedes` edge and forgetting also removes the vector entry.

Workers can create Skill drafts only. A draft remains in SQLite and cannot be
discovered by the SDK. An authenticated, explicit approval installs it under
`~/.claude/skills/muselab-generated-<name>/SKILL.md`; disabling moves it out of
the discoverable directory while preserving its audit trail.

Cross-Episode reflection requires the configured number of independent
Episodes. Independence is computed from normalized evidence content, so forked
or copied transcripts do not count as separate support. Every candidate names
its source Episodes. The white-box value score combines Verifier prediction,
independent-Episode count, historical recall-query fit, and novelty. Unsupported
or conflicting candidates are quarantined; low-value and shadow-mode candidates
remain pending review. Failed turns form separate failure Episodes, while
cancelled turns remain evidence-only and cannot reach Dreamer or Skill Learner.
Orphaned running jobs are requeued when the worker next starts.

Data lives in `$MUSELAB_ROOT/.muselab/memory/` by default and can be relocated
with `MUSELAB_MEMORY_DIR`. Back up the whole directory after stopping the
service, or include SQLite WAL/SHM files. The API exposes a neutral JSON export,
a rebuild operation, and a legacy Mem0 import whose provenance-poor facts enter
as low-confidence `pending_review` items.

Third-party generation uses its configured API key. For Claude authenticated
through `claude login`, background generation creates a fresh one-turn SDK
query with `tools=[]`, no MCP, and no Skills. It never borrows a live chat
client or receives Agent tool authority.

On a deployment/test machine, run:

```bash
uv sync
.venv/bin/pytest -q \
  tests/test_memory_store.py tests/test_memory_api.py \
  tests/test_memory_engine.py tests/test_memory_providers.py \
  tests/test_memory_client.py tests/test_frontend_lint.py
node --check frontend/app.js
```

Probe and begin in `shadow` mode, verify independent Episode provenance and
review state, then switch to `active`. Confirm that recall traces are visible,
provider outages remain fail-soft, and generated Skills remain inert until an
explicit authenticated approval.
