# Long-term memory

> [中文](memory_zh.md)

MuseLab's optional long-term memory stores durable business context,
preferences, decisions, and agent experience. It is disabled by default and is
not a repository-wide file RAG feature.

## Architecture

The canonical SQLite Registry retains evidence, Episodes, provenance,
versions, conflicts, review state, jobs, and audit events. Qdrant or pgvector
is only a rebuildable dense index. Retrieval fuses SQLite FTS, dense similarity,
metadata, authority, and confidence, with an optional reranker.

Each user has one logical memory pool. Workspaces, domains, topics, and entities are soft metadata, not isolated memory stores. Rebuild the retrieval index from the Registry after changing the embedding model or vector database.

## Setup

Configure Memory under Settings with:

1. a configured chat model for Dreamer and Verifier;
2. an OpenAI-compatible embedding endpoint;
3. Qdrant or PostgreSQL + pgvector;
4. an optional reranker.

Saving an enabled configuration through the UI probes the required capabilities by default; a failed probe prevents activation. `off` has no chat overhead, `shadow` forms reviewable candidates without recall, and `active` enables bounded hybrid recall. New configurations use a 2000 ms soft recall deadline; existing configurations retain their saved value. Retrieval or reranking failures and timeouts preserve available results where possible, or continue the reply without injecting memory. Dreamer, Verifier, indexing, and Skill learning run in the background.

## Dreaming and hybrid recall

Dreaming consolidates task experience. When a session reaches the configured turn or idle threshold, its Episode enters the background consolidation queue. “Dream now” also queues a job. Dreamer extracts candidate facts, decisions, and reflections; Verifier checks evidence, conflicts, and future value. Automatic consolidation requires memory to be enabled and the Worker to be running. Generated memories are inferred records, not user-confirmed facts.

In active mode, hybrid recall combines keyword and semantic-vector retrieval, then ranks results using authority, confidence, and usage feedback, with an optional reranker. It searches available memories in the Registry, not the entire workspace. Memory files inside a project are ordinary project materials, separate from this cross-session memory system.

## Memory management

The Memory Center supports search, sorting, pagination, confirmation, correction, and deletion. It exposes memories, sources, Episodes, reflections, conflicts,
jobs, recall traces, and Skill candidates. The brain button beside a chat
message is a deterministic confirmed-memory action; natural-language
"remember/correct/forget" classification is not required. Corrections retain a
`supersedes` edge and forgetting also removes the vector entry.

Workers can create Skill drafts only. A draft remains in SQLite and cannot be
discovered by the SDK. An authenticated, explicit approval installs it under
`~/.claude/skills/muselab-generated-<name>/SKILL.md`; disabling moves it out of
the discoverable directory while preserving its audit trail.

## Source evidence tracebacks

Memory sources can link to Episodes, conversation messages, and tool records. Open a source to return to its session. An explicit message source with a valid message ID can also locate the message; Episode and tool-evidence sources usually locate the session. Missing records or entries without sources may not support a traceback.

“Copy evidence” copies session evidence locators, including session and workspace information and original record paths. It does not copy the full conversation or recreate the execution environment. Locators may contain private paths and identifiers; inspect and redact them before sharing publicly.

## Reflection and value checks

Cross-Episode reflection requires the configured number of independent
Episodes. Independence is computed from normalized evidence content, so forked
or copied transcripts do not count as separate support. Every candidate names
its source Episodes. The white-box value score combines Verifier prediction,
independent-Episode count, historical recall-query fit, and novelty. Verifier rejects unsupported or conflicting candidates, which may leave no reviewable memory entry. Candidates that pass verification but have low value, or are produced in shadow mode, remain `pending_review`. Failed turns form separate failure Episodes, while
cancelled turns remain evidence-only and cannot reach Dreamer or Skill Learner.
Orphaned running jobs are requeued when the worker next starts.

## Data, migration, and recovery

Data lives in `$MUSELAB_ROOT/.muselab/memory/` by default and can be relocated
with `MUSELAB_MEMORY_DIR`. Back up the whole directory after stopping the
service, or include SQLite WAL/SHM files. The API exposes a neutral JSON export,
a rebuild operation, and a legacy Mem0 import whose provenance-poor facts enter
as low-confidence `pending_review` items.

Third-party generation uses its configured API key. For Claude authenticated
through `claude login`, background generation creates a fresh one-turn SDK
query with `tools=[]`, no MCP, and no Skills. It never borrows a live chat
client or receives Agent tool authority.

## Deployment verification

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

### Recall latency and diagnostics

Recall uses its own SQLite actor and read-only connections to the WAL registry.
Background consolidation, job bookkeeping, and recall telemetry use the write
actor. A recall read does not initialize or migrate the schema.

The soft budget starts at recall entry and covers recent evidence, dense and
lexical retrieval, hydration, and optional reranking. The configurable maximum
is 5 seconds; the facade stops at 8 seconds, before the SDK's 10-second hook
watchdog. Retrieval reserves a small part of its budget for hydration so that a
stalled channel does not discard the other channel's completed hits. Reranking
failure preserves hydrated results and reports partial success.

Safe performance events `memory.recall_hook_start`, `memory.recall_finish`,
and `memory.recall_hook_finish` report stage outcomes, elapsed time, counts,
and whether context was injected, without query or memory text. The recall ID
continues into `done.memory_recall`; the footer counts facts actually injected
after sanitization and context limits. Persistent receipts keep IDs and stage
diagnostics without duplicating memory contents. These diagnostics concern
recall; generation-provider failures during consolidation remain separate.
