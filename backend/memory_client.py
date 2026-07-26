"""Thin async client for the self-hosted mem0 daemon (~/mem0-native).

Design contract — FAIL-SOFT ALWAYS:
  Every public coroutine swallows all exceptions and degrades gracefully
  (search → "", store → no-op). mem0 is an *enhancement*: a daemon that is
  down, slow, or erroring must NEVER break or delay a chat turn beyond the
  short timeout. There is no retry, no queue — best effort, then move on.

The daemon (127.0.0.1:8800 by default) exposes:
  POST /search  {query, run_id?, user_id?, agent_id?, limit}  -> {results:[...]}
  POST /add     {messages, run_id?, user_id?, agent_id?, infer}

Scope mapping for single-user muselab:
  run_id   = session_id   (per-conversation memory isolation)
  user_id  = fixed constant (all turns share one logical owner)
  agent_id = model         (optional; lets memories note which model wrote them)
"""
import logging

from .settings import MEM0_DAEMON_URL

log = logging.getLogger("muselab.mem0")

# Single logical owner for this single-user deployment.
_USER_ID = "muselab"
# Keep chat latency bounded: a slow daemon must not stall the turn.
_SEARCH_TIMEOUT = 3.0
_STORE_TIMEOUT = 10.0
_SEARCH_LIMIT = 5


def enabled() -> bool:
    """True when a daemon URL is configured. When False, all calls are no-ops."""
    return bool(MEM0_DAEMON_URL)


def _extract_text(results) -> list[str]:
    """Pull the human-readable memory strings out of a /search response.

    mem0 returns either {"results": [{"memory": "..."}, ...]} or a bare list;
    tolerate both plus the odd key name so a daemon version bump doesn't
    silently drop context."""
    if isinstance(results, dict):
        results = results.get("results", results.get("memories", []))
    out: list[str] = []
    for r in results or []:
        if isinstance(r, dict):
            txt = r.get("memory") or r.get("text") or r.get("content")
            if txt:
                out.append(str(txt).strip())
        elif isinstance(r, str):
            out.append(r.strip())
    return out


async def search_context(query: str, session_id: str) -> str:
    """Return a prompt-ready memory block for `query`, or "" if none / disabled.

    The returned string, when non-empty, is a self-contained section safe to
    prepend to the user prompt. Never raises."""
    if not enabled() or not query.strip():
        return ""
    try:
        import httpx
        payload = {
            "query": query,
            "run_id": session_id,
            "user_id": _USER_ID,
            "limit": _SEARCH_LIMIT,
        }
        async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT) as hc:
            resp = await hc.post(f"{MEM0_DAEMON_URL}/search", json=payload)
            resp.raise_for_status()
            mems = _extract_text(resp.json())
    except Exception as e:
        log.debug("mem0 search skipped: %s", e)
        return ""
    if not mems:
        return ""
    bullets = "\n".join(f"- {m}" for m in mems)
    return (
        "--- Relevant memory from earlier conversations ---\n"
        "The following facts were recalled from prior sessions. Use them if "
        "relevant; ignore if not.\n"
        f"{bullets}\n"
        "--- end memory ---\n\n"
    )


async def store_turn(session_id: str, model: str, user_text: str,
                     assistant_text: str) -> None:
    """Persist one completed (user, assistant) exchange to mem0. Never raises.

    mem0's LLM fact-extractor (infer=True) distills durable facts from the
    exchange; we hand it the raw two-message conversation."""
    if not enabled():
        return
    if not user_text.strip() and not assistant_text.strip():
        return
    try:
        import httpx
        messages = [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
        payload = {
            "messages": messages,
            "run_id": session_id,
            "user_id": _USER_ID,
            "agent_id": model or None,
            "infer": True,
        }
        async with httpx.AsyncClient(timeout=_STORE_TIMEOUT) as hc:
            resp = await hc.post(f"{MEM0_DAEMON_URL}/add", json=payload)
            resp.raise_for_status()
    except Exception as e:
        log.debug("mem0 store skipped: %s", e)
