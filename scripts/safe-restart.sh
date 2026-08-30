#!/usr/bin/env bash
# Safely restart the local screen-managed MuseLab service.
# The same file is both the preflight coordinator and detached watchdog.
set -u

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO="$(cd "$(dirname "$SELF")/.." && pwd)"

PORT="${MUSELAB_RESTART_PORT:-8766}"
PREFLIGHT_PORT="${MUSELAB_RESTART_PREFLIGHT_PORT:-$((PORT + 1000))}"
ENTRY="${MUSELAB_RESTART_ENTRY:-$REPO/scripts/run-local-gnu.sh}"
ROLLBACK_ENTRY="${MUSELAB_RESTART_ROLLBACK_ENTRY:-}"
ROLLBACK_ENV="${MUSELAB_RESTART_ROLLBACK_ENV:-}"
SESSION="${MUSELAB_RESTART_SESSION:-muselab-$PORT}"
STATUS="${MUSELAB_RESTART_STATUS:-/tmp/muselab-safe-restart.status}"
STARTUP_LOG="${MUSELAB_RESTART_STARTUP_LOG:-$REPO/muselab.log}"
WATCHDOG_LOG="${MUSELAB_RESTART_WATCHDOG_LOG:-/tmp/muselab-safe-restart.watchdog.log}"
HEALTH_TIMEOUT="${MUSELAB_RESTART_HEALTH_TIMEOUT:-90}"
STOP_TIMEOUT="${MUSELAB_RESTART_STOP_TIMEOUT:-15}"
POLL_INTERVAL="${MUSELAB_RESTART_POLL_INTERVAL:-1}"
DETACH_DELAY="${MUSELAB_RESTART_DETACH_DELAY:-2}"

CURL_BIN="${MUSELAB_RESTART_CURL_BIN:-curl}"
SCREEN_BIN="${MUSELAB_RESTART_SCREEN_BIN:-screen}"
SS_BIN="${MUSELAB_RESTART_SS_BIN:-ss}"
FUSER_BIN="${MUSELAB_RESTART_FUSER_BIN:-fuser}"
PYTHON_BIN="${MUSELAB_RESTART_PYTHON_BIN:-python3}"
NOHUP_BIN="${MUSELAB_RESTART_NOHUP_BIN:-nohup}"

MODE=coordinator
OLD_PID=""
NEW_PID=""
PREFLIGHT_PID=""
HTTP_CODE=000
STARTUP_MARKER=""
READY_PID=""
REASON=""

usage() {
  printf '%s\n' \
    "Usage: scripts/safe-restart.sh [options]" \
    "  --port PORT              production port (default: 8766)" \
    "  --preflight-port PORT    spare port for cold-start validation" \
    "  --entry PATH             candidate launch entry" \
    "  --rollback-entry PATH    launch entry used after a failed switch" \
    "  --rollback-env PATH      pre-change .env backup restored on rollback" \
    "  --session NAME           production screen session" \
    "  --status PATH            atomic key=value status file" \
    "  --startup-log PATH       service startup log" \
    "  --watchdog-log PATH      detached watchdog diagnostics"
}

while (($#)); do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --preflight-port) PREFLIGHT_PORT="$2"; shift 2 ;;
    --entry) ENTRY="$2"; shift 2 ;;
    --rollback-entry) ROLLBACK_ENTRY="$2"; shift 2 ;;
    --rollback-env) ROLLBACK_ENV="$2"; shift 2 ;;
    --session) SESSION="$2"; shift 2 ;;
    --status) STATUS="$2"; shift 2 ;;
    --startup-log) STARTUP_LOG="$2"; shift 2 ;;
    --watchdog-log) WATCHDOG_LOG="$2"; shift 2 ;;
    --watchdog) MODE=watchdog; shift ;;
    --preflight-pid) PREFLIGHT_PID="$2"; shift 2 ;;
    --listener-pid) MODE=listener; PORT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
ROLLBACK_ENTRY="${ROLLBACK_ENTRY:-$ENTRY}"

unset LD_LIBRARY_PATH LD_PRELOAD || true
unset http_proxy https_proxy all_proxy no_proxy || true
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY || true

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }

validate_options() {
  is_uint "$PORT" && ((PORT >= 1024 && PORT <= 65535)) || { REASON=invalid_port; return 1; }
  is_uint "$PREFLIGHT_PORT" && ((PREFLIGHT_PORT >= 1024 && PREFLIGHT_PORT <= 65535)) || { REASON=invalid_preflight_port; return 1; }
  [[ "$PORT" != "$PREFLIGHT_PORT" ]] || { REASON=preflight_port_matches_production; return 1; }
  is_uint "$HEALTH_TIMEOUT" && is_uint "$STOP_TIMEOUT" || { REASON=invalid_timeout; return 1; }
  [[ -x "$ENTRY" ]] || { REASON=entry_not_executable; return 1; }
  [[ -x "$ROLLBACK_ENTRY" ]] || { REASON=rollback_entry_not_executable; return 1; }
  [[ -z "$ROLLBACK_ENV" || -f "$ROLLBACK_ENV" ]] || { REASON=rollback_env_missing; return 1; }
  command -v "$CURL_BIN" >/dev/null 2>&1 || { REASON=curl_missing; return 1; }
  command -v "$SCREEN_BIN" >/dev/null 2>&1 || { REASON=screen_missing; return 1; }
  return 0
}

listener_pid_from_proc() {
  [[ "${MUSELAB_RESTART_DISABLE_PROC:-0}" != 1 ]] || return 1
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || return 1
  "$PYTHON_BIN" - "$1" 2>/dev/null <<'PY'
import glob
import os
import sys

port = int(sys.argv[1])
inodes = set()
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        with open(table, encoding="ascii") as rows:
            next(rows, None)
            for row in rows:
                fields = row.split()
                if len(fields) > 9 and fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                    inodes.add(fields[9])
    except OSError:
        pass
if not inodes:
    raise SystemExit(1)
for fd in glob.iglob("/proc/[0-9]*/fd/*"):
    try:
        target = os.readlink(fd)
    except OSError:
        continue
    if target.startswith("socket:[") and target[8:-1] in inodes:
        print(fd.split("/", 3)[2])
        raise SystemExit(0)
raise SystemExit(1)
PY
}

listener_pid_from_fuser() {
  command -v "$FUSER_BIN" >/dev/null 2>&1 || return 1
  local output token
  output="$($FUSER_BIN -n tcp "$1" 2>/dev/null || true)"
  for token in $output; do
    if is_uint "$token" && kill -0 "$token" 2>/dev/null; then
      printf '%s\n' "$token"
      return 0
    fi
  done
  return 1
}

listener_pid_from_ss() {
  command -v "$SS_BIN" >/dev/null 2>&1 || return 1
  local output match
  output="$($SS_BIN -H -ltnp "sport = :$1" 2>/dev/null || true)"
  # Support both pid=123 and this host's users:(("python",123,fd)) form.
  match="$(printf '%s\n' "$output" | grep -oE 'pid=[0-9]+|"[^"]+",[0-9]+,' | grep -oE '[0-9]+' | head -1 || true)"
  [[ -n "$match" ]] || return 1
  printf '%s\n' "$match"
}

listener_pid() {
  listener_pid_from_proc "$1" || listener_pid_from_fuser "$1" || listener_pid_from_ss "$1"
}

health_code() {
  local code
  code="$("$CURL_BIN" -sS -o /dev/null -w '%{http_code}' --max-time 3 \
    "http://127.0.0.1:$1/api/health" 2>/dev/null || true)"
  printf '%s\n' "${code:-000}"
}

status_write() {
  local phase="$1" result="$2" tmp="${STATUS}.tmp.$$"
  mkdir -p "$(dirname "$STATUS")"
  {
    printf 'version=1\n'
    printf 'updated_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'phase=%s\n' "$phase"
    printf 'result=%s\n' "$result"
    printf 'reason=%s\n' "$REASON"
    printf 'port=%s\n' "$PORT"
    printf 'preflight_port=%s\n' "$PREFLIGHT_PORT"
    printf 'session=%s\n' "$SESSION"
    printf 'old_pid=%s\n' "$OLD_PID"
    printf 'preflight_pid=%s\n' "$PREFLIGHT_PID"
    printf 'new_pid=%s\n' "$NEW_PID"
    printf 'http_code=%s\n' "$HTTP_CODE"
    printf 'startup_marker=%s\n' "$STARTUP_MARKER"
  } >"$tmp"
  mv -f "$tmp" "$STATUS"
}

wait_for_ready() {
  local port="$1" previous_pid="${2:-}" elapsed=0 pid=""
  READY_PID=""
  while ((elapsed < HEALTH_TIMEOUT)); do
    HTTP_CODE="$(health_code "$port")"
    pid="$(listener_pid "$port" 2>/dev/null || true)"
    if [[ "$HTTP_CODE" == 200 && -n "$pid" && "$pid" != "$previous_pid" ]] && kill -0 "$pid" 2>/dev/null; then
      READY_PID="$pid"
      return 0
    fi
    sleep "$POLL_INTERVAL"
    elapsed=$((elapsed + POLL_INTERVAL))
  done
  return 1
}

wait_for_port_free() {
  local port="$1" elapsed=0
  while ((elapsed < STOP_TIMEOUT)); do
    listener_pid "$port" >/dev/null 2>&1 || return 0
    sleep "$POLL_INTERVAL"
    elapsed=$((elapsed + POLL_INTERVAL))
  done
  return 1
}

screen_payload() {
  local marker="$1" port="$2" entry="$3" marker_q log_q entry_q
  printf -v marker_q '%q' "$marker"
  printf -v log_q '%q' "$STARTUP_LOG"
  printf -v entry_q '%q' "$entry"
  printf 'printf "%%s\\n" %s >> %s; export MUSELAB_PORT=%q; exec %s >> %s 2>&1' \
    "$marker_q" "$log_q" "$port" "$entry_q" "$log_q"
}

start_screen_service() {
  local session="$1" port="$2" entry="$3" marker="$4" payload
  mkdir -p "$(dirname "$STARTUP_LOG")"
  touch "$STARTUP_LOG" || return 1
  payload="$(screen_payload "$marker" "$port" "$entry")"
  "$SCREEN_BIN" -dmS "$session" bash -lc "$payload"
}

stop_service() {
  local session="$1" port="$2" expected_pid="${3:-}" pid=""
  "$SCREEN_BIN" -S "$session" -X quit >/dev/null 2>&1 || true

  # `screen -X quit` removes the terminal session but does not reliably signal
  # the exec'd listener. Waiting a full stop timeout before the first SIGTERM
  # added 30 seconds to both preflight cleanup and production switchover. Verify
  # that the port still belongs to the PID captured by this restart, then begin
  # the bounded graceful shutdown immediately.
  pid="$(listener_pid "$port" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 0
  [[ -n "$expected_pid" && "$pid" == "$expected_pid" ]] || return 1
  kill -TERM "$pid" 2>/dev/null || true
  wait_for_port_free "$port" && return 0

  # Uvicorn has its own 3-second connection drain and MuseLab bounds subsystem
  # cleanup. If either wedges beyond STOP_TIMEOUT, preserve the existing hard
  # stop fallback rather than allowing a restart to hang indefinitely.
  pid="$(listener_pid "$port" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 0
  [[ -n "$expected_pid" && "$pid" == "$expected_pid" ]] || return 1
  kill -KILL "$pid" 2>/dev/null || true
  wait_for_port_free "$port"
}

verify_startup_log() {
  grep -Fqx "$1" "$STARTUP_LOG" 2>/dev/null
}

run_preflight() {
  local preflight_session="${SESSION}-preflight-$$" marker pid
  listener_pid "$PREFLIGHT_PORT" >/dev/null 2>&1 && { REASON=preflight_port_in_use; return 1; }
  marker="SAFE_RESTART_PREFLIGHT_$(date +%s)_$$"
  STARTUP_MARKER="$marker"
  start_screen_service "$preflight_session" "$PREFLIGHT_PORT" "$ENTRY" "$marker" || { REASON=preflight_launch_failed; return 1; }
  if wait_for_ready "$PREFLIGHT_PORT" ""; then pid="$READY_PID"; else pid=""; fi
  PREFLIGHT_PID="$pid"
  if [[ -z "$pid" ]] || ! verify_startup_log "$marker"; then
    REASON=preflight_verification_failed
    stop_service "$preflight_session" "$PREFLIGHT_PORT" "$pid" || true
    return 1
  fi
  if ! stop_service "$preflight_session" "$PREFLIGHT_PORT" "$pid"; then
    REASON=preflight_cleanup_failed
    return 1
  fi
  return 0
}

start_and_verify_production() {
  local entry="$1" previous_pid="$2" marker pid
  marker="SAFE_RESTART_START_$(date +%s)_$$"
  STARTUP_MARKER="$marker"
  start_screen_service "$SESSION" "$PORT" "$entry" "$marker" || return 1
  if wait_for_ready "$PORT" "$previous_pid"; then pid="$READY_PID"; else pid=""; fi
  [[ -n "$pid" ]] && verify_startup_log "$marker" || return 1
  NEW_PID="$pid"
  return 0
}

restore_rollback_env() {
  [[ -n "$ROLLBACK_ENV" ]] || return 0
  local target="$REPO/.env" tmp="${target}.safe-restart.$$"
  cp -p "$ROLLBACK_ENV" "$tmp" && mv -f "$tmp" "$target"
}

rollback() {
  status_write rollback in_progress
  stop_service "$SESSION" "$PORT" "$NEW_PID" || true
  if ! restore_rollback_env; then
    REASON=rollback_env_restore_failed
    status_write done failed
    return 1
  fi
  NEW_PID=""
  if start_and_verify_production "$ROLLBACK_ENTRY" ""; then
    REASON=""
    status_write done rolled_back
    return 0
  fi
  REASON=rollback_start_failed
  status_write done failed
  return 1
}

run_watchdog() {
  exec >>"$WATCHDOG_LOG" 2>&1
  sleep "$DETACH_DELAY"
  if ! validate_options; then
    status_write aborted failed
    return 1
  fi
  OLD_PID="$(listener_pid "$PORT" 2>/dev/null || true)"
  HTTP_CODE="$(health_code "$PORT")"
  if [[ "$HTTP_CODE" != 200 || -z "$OLD_PID" ]]; then
    REASON=baseline_changed
    status_write aborted failed
    return 1
  fi
  status_write stopping in_progress
  if ! stop_service "$SESSION" "$PORT" "$OLD_PID"; then
    REASON=production_port_not_released
    status_write rollback in_progress
    rollback
    return $?
  fi
  status_write starting in_progress
  NEW_PID=""
  if start_and_verify_production "$ENTRY" "$OLD_PID"; then
    REASON=""
    status_write done success
    return 0
  fi
  REASON=candidate_start_failed
  rollback
}

coordinator() {
  if ! validate_options; then
    status_write aborted failed
    return 1
  fi
  OLD_PID="$(listener_pid "$PORT" 2>/dev/null || true)"
  HTTP_CODE="$(health_code "$PORT")"
  if [[ "$HTTP_CODE" != 200 || -z "$OLD_PID" ]]; then
    REASON=baseline_unhealthy
    status_write aborted failed
    return 1
  fi
  status_write baseline in_progress
  if ! run_preflight; then
    status_write preflight failed
    return 1
  fi
  status_write preflight success

  local args=(
    --watchdog --port "$PORT" --preflight-port "$PREFLIGHT_PORT" --preflight-pid "$PREFLIGHT_PID"
    --entry "$ENTRY" --rollback-entry "$ROLLBACK_ENTRY" --session "$SESSION"
    --status "$STATUS" --startup-log "$STARTUP_LOG" --watchdog-log "$WATCHDOG_LOG"
  )
  [[ -z "$ROLLBACK_ENV" ]] || args+=(--rollback-env "$ROLLBACK_ENV")
  status_write detached in_progress
  "$NOHUP_BIN" "$SELF" "${args[@]}" </dev/null >>"$WATCHDOG_LOG" 2>&1 &
  printf 'safe restart watchdog detached; status=%s\n' "$STATUS"
}

case "$MODE" in
  listener) listener_pid "$PORT" ;;
  watchdog) run_watchdog ;;
  coordinator) coordinator ;;
esac
