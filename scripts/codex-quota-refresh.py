#!/usr/bin/env python3
"""Read Codex account quota and token usage without consuming a model turn.

The preferred path is Codex app-server's versioned JSON-RPC surface:

* ``account/rateLimits/read`` returns the current account quota buckets;
* ``account/usage/read`` returns account-wide token usage.

The script never reads Codex auth files and never sends a prompt.  Older Codex
builds that do not expose the account RPCs fall back to the newest rate-limit
snapshot already present in local session JSONL files.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_INITIALIZE_ID = 1
_RATE_LIMITS_ID = 2
_ACCOUNT_USAGE_ID = 3


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _find_codex() -> str | None:
    explicit = os.environ.get("CODEX_BIN", "").strip()
    if explicit:
        return explicit
    found = shutil.which("codex")
    if found:
        return found
    fallback = Path.home() / ".npm-global" / "bin" / "codex"
    if fallback.exists():
        return str(fallback)
    return None


def _rate_limit_type(key: str, window: dict[str, Any]) -> str:
    minutes = int(window.get("window_minutes") or 0)
    if minutes == 300:
        return "five_hour"
    if minutes == 10080:
        return "seven_day"
    if 28 * 24 * 60 <= minutes <= 31 * 24 * 60:
        return "monthly"
    return key


def _from_payload(payload: dict[str, Any], source: Path, ts: str | None) -> dict[str, Any] | None:
    raw = payload.get("rate_limits")
    if not isinstance(raw, dict):
        return None
    windows: dict[str, dict[str, Any]] = {}
    reached = raw.get("rate_limit_reached_type")
    for key in ("primary", "secondary"):
        w = raw.get(key)
        if not isinstance(w, dict):
            continue
        try:
            used_f = float(w.get("used_percent"))
        except (TypeError, ValueError):
            used_f = None
        kind = _rate_limit_type(key, w)
        status = "allowed"
        if reached and (reached == key or reached == kind):
            status = "rejected"
        elif used_f is not None and used_f >= 90:
            status = "allowed_warning"
        windows[key] = {
            "rate_limit_type": kind,
            "window_minutes": int(w.get("window_minutes") or 0),
            "resets_at": int(w.get("resets_at") or 0) or None,
            "used_percent": used_f,
            "remaining_percent": (
                round(max(0.0, 100.0 - used_f), 1) if used_f is not None else None
            ),
            "utilization": (used_f / 100.0 if used_f is not None else None),
            "status": status,
        }
    if not windows:
        return None
    updated_at = 0.0
    if ts:
        try:
            updated_at = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            updated_at = 0.0
    return {
        "ok": True,
        "source": "codex-cli-exec",
        "source_scope": "codex_cli_exec_rate_limits",
        "provider_authoritative": False,
        "source_file": str(source),
        "updated_at": updated_at,
        "timestamp": ts,
        "limit_id": raw.get("limit_id"),
        "limit_name": raw.get("limit_name"),
        "plan_type": raw.get("plan_type"),
        "rate_limit_reached_type": reached,
        "credits": raw.get("credits"),
        "individual_limit": raw.get("individual_limit"),
        "windows": windows,
    }


def _snake_account_usage(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize the app-server account usage response for the HTTP API/UI."""
    if not isinstance(raw, dict):
        return None
    summary = raw.get("summary")
    if not isinstance(summary, dict):
        return None
    buckets: list[dict[str, Any]] = []
    for item in raw.get("dailyUsageBuckets") or []:
        if not isinstance(item, dict):
            continue
        start_date = item.get("startDate")
        try:
            tokens = int(item.get("tokens") or 0)
        except (TypeError, ValueError):
            continue
        if isinstance(start_date, str) and start_date:
            buckets.append({"start_date": start_date, "tokens": tokens})
    buckets.sort(key=lambda item: item["start_date"])
    return {
        "summary": {
            "lifetime_tokens": summary.get("lifetimeTokens"),
            "peak_daily_tokens": summary.get("peakDailyTokens"),
            "longest_running_turn_seconds": summary.get("longestRunningTurnSec"),
            "current_streak_days": summary.get("currentStreakDays"),
            "longest_streak_days": summary.get("longestStreakDays"),
        },
        "daily_usage_buckets": buckets,
    }


def _app_server_rate_limit_type(key: str, window: dict[str, Any]) -> str:
    return _rate_limit_type(
        key,
        {"window_minutes": window.get("windowDurationMins")},
    )


def _normalize_account_snapshot(
    rate_limits: dict[str, Any] | None,
    account_usage: dict[str, Any] | None,
) -> dict[str, Any]:
    """Convert current app-server camelCase account responses to UI shape."""
    now = time.time()
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    normalized_usage = _snake_account_usage(account_usage)
    rate_limits = rate_limits if isinstance(rate_limits, dict) else {}
    default_snapshot = rate_limits.get("rateLimits")
    if not isinstance(default_snapshot, dict):
        default_snapshot = {}
    raw_by_id = rate_limits.get("rateLimitsByLimitId")
    by_id = raw_by_id if isinstance(raw_by_id, dict) else {}
    if not by_id and default_snapshot:
        default_id = str(default_snapshot.get("limitId") or "codex")
        by_id = {default_id: default_snapshot}

    default_id = str(default_snapshot.get("limitId") or "codex")
    windows: dict[str, dict[str, Any]] = {}
    normalized_buckets: dict[str, dict[str, Any]] = {}
    for limit_id, snapshot in by_id.items():
        if not isinstance(snapshot, dict):
            continue
        bucket_id = str(limit_id or snapshot.get("limitId") or "codex")
        limit_name = snapshot.get("limitName")
        plan_type = snapshot.get("planType")
        reached = snapshot.get("rateLimitReachedType")
        bucket_windows: dict[str, dict[str, Any]] = {}
        for key in ("primary", "secondary"):
            window = snapshot.get(key)
            if not isinstance(window, dict):
                continue
            try:
                used = float(window.get("usedPercent"))
            except (TypeError, ValueError):
                used = None
            window_key = key if bucket_id == default_id else f"{bucket_id}:{key}"
            status = "rejected" if reached else (
                "allowed_warning" if used is not None and used >= 90 else "allowed"
            )
            normalized = {
                "rate_limit_type": _app_server_rate_limit_type(key, window),
                "window_minutes": int(window.get("windowDurationMins") or 0),
                "resets_at": int(window.get("resetsAt") or 0) or None,
                "used_percent": used,
                "remaining_percent": (
                    round(max(0.0, 100.0 - used), 1) if used is not None else None
                ),
                "utilization": (used / 100.0 if used is not None else None),
                "status": status,
                "limit_id": bucket_id,
                "limit_name": limit_name,
                "plan_type": plan_type,
            }
            windows[window_key] = normalized
            bucket_windows[key] = normalized
        normalized_buckets[bucket_id] = {
            "limit_id": bucket_id,
            "limit_name": limit_name,
            "plan_type": plan_type,
            "rate_limit_reached_type": reached,
            "windows": bucket_windows,
        }

    reset_credits = rate_limits.get("rateLimitResetCredits")
    available_resets = None
    if isinstance(reset_credits, dict):
        try:
            available_resets = int(reset_credits.get("availableCount") or 0)
        except (TypeError, ValueError):
            available_resets = None

    return {
        "ok": bool(windows) or normalized_usage is not None,
        "source": "codex-app-server",
        "source_scope": "local_codex_account",
        # The RPC is authoritative for the local Codex account, but muselab
        # cannot prove that a separately configured gateway uses that account.
        "provider_authoritative": False,
        "account_authoritative": True,
        "gateway_account_verified": False,
        "updated_at": now,
        "timestamp": timestamp,
        "limit_id": default_snapshot.get("limitId"),
        "limit_name": default_snapshot.get("limitName"),
        "plan_type": default_snapshot.get("planType"),
        "rate_limit_reached_type": default_snapshot.get("rateLimitReachedType"),
        "credits": default_snapshot.get("credits"),
        "individual_limit": default_snapshot.get("individualLimit"),
        "rate_limit_reset_credits": {"available_count": available_resets},
        "rate_limits_by_limit_id": normalized_buckets,
        "windows": windows,
        "account_usage": normalized_usage,
    }


def _latest_rate_limits(max_files: int) -> dict[str, Any]:
    sessions_dir = _codex_home() / "sessions"
    if not sessions_dir.exists():
        return {"ok": False, "reason": "codex_sessions_missing", "windows": {}, "updated_at": 0}
    try:
        files = sorted(
            sessions_dir.rglob("*.jsonl"),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError as e:
        return {
            "ok": False,
            "reason": f"codex_sessions_unreadable: {e}",
            "windows": {},
            "updated_at": 0,
        }
    for path in files[:max(1, max_files)]:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            parsed = _from_payload(payload, path, event.get("timestamp"))
            if parsed:
                return parsed
    return {"ok": False, "reason": "codex_rate_limits_not_found", "windows": {}, "updated_at": 0}


def _reader(stream, kind: str, events: queue.Queue) -> None:
    try:
        for line in iter(stream.readline, ""):
            events.put((kind, line))
    finally:
        events.put((kind, None))


def _send(proc: subprocess.Popen, payload: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("codex app-server stdin unavailable")
    proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def _read_app_server(timeout: int) -> dict[str, Any]:
    codex = _find_codex()
    if not codex:
        return {"ok": False, "reason": "codex_not_found"}
    cmd = [codex, "app-server", "--stdio"]
    started = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=tempfile.gettempdir(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return {"ok": False, "reason": f"codex_app_server_start_failed: {exc}"}

    events: queue.Queue = queue.Queue()
    assert proc.stdout is not None and proc.stderr is not None
    threading.Thread(target=_reader, args=(proc.stdout, "stdout", events), daemon=True).start()
    threading.Thread(target=_reader, args=(proc.stderr, "stderr", events), daemon=True).start()
    responses: dict[int, dict[str, Any]] = {}
    stderr_tail = ""
    deadline = started + max(5, timeout)

    def _drain_until(wanted: set[int]) -> None:
        nonlocal stderr_tail
        while not wanted.issubset(responses) and time.time() < deadline:
            try:
                kind, line = events.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if line is None:
                continue
            if kind == "stderr":
                stderr_tail = (stderr_tail + line)[-800:]
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response_id = message.get("id")
            if isinstance(response_id, int):
                responses[response_id] = message

    try:
        _send(proc, {
            "id": _INITIALIZE_ID,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "muselab", "version": "1"},
            },
        })
        _drain_until({_INITIALIZE_ID})
        initialized = responses.get(_INITIALIZE_ID) or {}
        if initialized.get("error") or _INITIALIZE_ID not in responses:
            return {
                "ok": False,
                "reason": "codex_app_server_initialize_failed",
                "error": initialized.get("error"),
                "stderr_tail": stderr_tail,
            }
        _send(proc, {"method": "initialized"})
        _send(proc, {"id": _RATE_LIMITS_ID, "method": "account/rateLimits/read", "params": None})
        _send(proc, {"id": _ACCOUNT_USAGE_ID, "method": "account/usage/read", "params": None})
        _drain_until({_RATE_LIMITS_ID, _ACCOUNT_USAGE_ID})

        rate_response = responses.get(_RATE_LIMITS_ID) or {}
        usage_response = responses.get(_ACCOUNT_USAGE_ID) or {}
        rate_result = rate_response.get("result")
        usage_result = usage_response.get("result")
        if not isinstance(rate_result, dict) and not isinstance(usage_result, dict):
            return {
                "ok": False,
                "reason": "codex_account_rpc_failed",
                "rate_limits_error": rate_response.get("error") or "timeout",
                "account_usage_error": usage_response.get("error") or "timeout",
                "elapsed_s": round(time.time() - started, 1),
                "stderr_tail": stderr_tail,
            }
        snapshot = _normalize_account_snapshot(rate_result, usage_result)
        snapshot["elapsed_s"] = round(time.time() - started, 1)
        if not isinstance(usage_result, dict):
            snapshot["account_usage_error"] = usage_response.get("error") or "timeout"
        if not isinstance(rate_result, dict):
            snapshot["rate_limits_error"] = rate_response.get("error") or "timeout"
        return snapshot
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("MUSELAB_CODEX_QUOTA_TIMEOUT", "25")))
    parser.add_argument("--max-files", type=int, default=int(os.environ.get("MUSELAB_CODEX_RATE_LIMIT_SCAN_FILES", "80")))
    parser.add_argument("--no-refresh", action="store_true")
    args = parser.parse_args()

    if args.no_refresh:
        result = _latest_rate_limits(args.max_files)
    else:
        result = _read_app_server(args.timeout)
        if not result.get("ok"):
            fallback = _latest_rate_limits(args.max_files)
            fallback["app_server"] = result
            result = fallback
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
