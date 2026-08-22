import os
import socket
import subprocess
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "safe-restart.sh"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def _fake_screen(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "screen-state"
    state.mkdir()
    screen = _write_executable(
        tmp_path / "screen",
        """#!/usr/bin/env bash
set -u
state=${SCREEN_STATE_DIR:?}
if [[ ${1:-} == -dmS ]]; then
  session=$2
  shift 2
  "$@" &
  printf '%s\n' "$!" > "$state/$session.pid"
  exit 0
fi
if [[ ${1:-} == -S && ${3:-} == -X && ${4:-} == quit ]]; then
  file="$state/$2.pid"
  if [[ -f $file ]]; then
    pid=$(<"$file")
    if [[ ${SCREEN_QUIT_NO_SIGNAL:-0} != 1 ]]; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
    rm -f "$file"
  fi
  exit 0
fi
exit 1
""",
    )
    return screen, state


def _good_entry(tmp_path: Path) -> Path:
    return _write_executable(
        tmp_path / "good-entry.sh",
        """#!/usr/bin/env bash
exec python3 - "$MUSELAB_PORT" <<'PY'
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *args):
        pass

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PY
""",
    )


def _candidate_entry(tmp_path: Path, preflight_port: int) -> Path:
    return _write_executable(
        tmp_path / "candidate-entry.sh",
        f"""#!/usr/bin/env bash
if [[ "$MUSELAB_PORT" != {preflight_port} ]]; then
  exit 23
fi
exec {tmp_path / 'good-entry.sh'}
""",
    )


def _wait_health(port: int, timeout: float = 5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"port {port} did not become ready")


def _read_status(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def _wait_result(path: Path, expected: str, timeout: float = 12) -> dict[str, str]:
    deadline = time.time() + timeout
    latest = {}
    while time.time() < deadline:
        latest = _read_status(path)
        if latest.get("result") == expected:
            return latest
        time.sleep(0.05)
    raise AssertionError(f"status never reached {expected}: {latest}")


def _base_env(tmp_path: Path, screen: Path, state: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "SCREEN_STATE_DIR": str(state),
            "MUSELAB_RESTART_SCREEN_BIN": str(screen),
            "MUSELAB_RESTART_HEALTH_TIMEOUT": "5",
            "MUSELAB_RESTART_STOP_TIMEOUT": "2",
            "MUSELAB_RESTART_POLL_INTERVAL": "1",
            "MUSELAB_RESTART_DETACH_DELAY": "0",
        }
    )
    return env


def _start_baseline(screen: Path, state: Path, session: str, entry: Path, port: int) -> None:
    env = os.environ.copy()
    env["SCREEN_STATE_DIR"] = str(state)
    payload = f"export MUSELAB_PORT={port}; exec {entry}"
    subprocess.run(
        [str(screen), "-dmS", session, "bash", "-lc", payload],
        check=True,
        env=env,
    )
    _wait_health(port)


def _stop_screen(screen: Path, state: Path, session: str) -> None:
    env = os.environ.copy()
    env["SCREEN_STATE_DIR"] = str(state)
    subprocess.run(
        [str(screen), "-S", session, "-X", "quit"],
        check=False,
        env=env,
    )


def test_listener_pid_supports_local_ss_users_format(tmp_path: Path):
    ss = _write_executable(
        tmp_path / "ss",
        """#!/usr/bin/env bash
printf '%s\n' 'LISTEN 0 2048 127.0.0.1:8766 0.0.0.0:* users:(("python",43210,7))'
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "MUSELAB_RESTART_DISABLE_PROC": "1",
            "MUSELAB_RESTART_FUSER_BIN": str(tmp_path / "missing-fuser"),
            "MUSELAB_RESTART_SS_BIN": str(ss),
        }
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "--listener-pid", "8766"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.stdout.strip() == "43210"


def test_safe_restart_cold_starts_then_switches_with_new_pid(tmp_path: Path):
    port, preflight_port = _free_port(), _free_port()
    screen, state = _fake_screen(tmp_path)
    entry = _good_entry(tmp_path)
    session = f"muselab-{port}"
    status = tmp_path / "status"
    startup_log = tmp_path / "startup.log"
    watchdog_log = tmp_path / "watchdog.log"
    env = _base_env(tmp_path, screen, state)
    _start_baseline(screen, state, session, entry, port)
    try:
        subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--port",
                str(port),
                "--preflight-port",
                str(preflight_port),
                "--entry",
                str(entry),
                "--session",
                session,
                "--status",
                str(status),
                "--startup-log",
                str(startup_log),
                "--watchdog-log",
                str(watchdog_log),
            ],
            check=True,
            env=env,
        )
        final = _wait_result(status, "success")
        assert final["phase"] == "done"
        assert final["old_pid"].isdigit()
        assert final["preflight_pid"].isdigit()
        assert final["new_pid"].isdigit()
        assert final["old_pid"] != final["new_pid"]
        assert final["startup_marker"] in startup_log.read_text().splitlines()
    finally:
        _stop_screen(screen, state, session)


def test_safe_restart_signals_listener_without_waiting_for_screen_timeout(
        tmp_path: Path):
    port, preflight_port = _free_port(), _free_port()
    screen, state = _fake_screen(tmp_path)
    entry = _good_entry(tmp_path)
    session = f"muselab-{port}"
    status = tmp_path / "status"
    env = _base_env(tmp_path, screen, state)
    env["SCREEN_QUIT_NO_SIGNAL"] = "1"
    env["MUSELAB_RESTART_STOP_TIMEOUT"] = "4"
    _start_baseline(screen, state, session, entry, port)
    started = time.monotonic()
    try:
        subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--port",
                str(port),
                "--preflight-port",
                str(preflight_port),
                "--entry",
                str(entry),
                "--session",
                session,
                "--status",
                str(status),
                "--startup-log",
                str(tmp_path / "startup.log"),
                "--watchdog-log",
                str(tmp_path / "watchdog.log"),
            ],
            check=True,
            env=env,
        )
        final = _wait_result(status, "success")
        elapsed = time.monotonic() - started
        assert final["phase"] == "done"
        # The previous implementation spent STOP_TIMEOUT once before sending
        # SIGTERM to each of the two listeners, so this fixture took >8 seconds.
        assert elapsed < 7
    finally:
        _stop_screen(screen, state, session)


def test_failed_candidate_automatically_restores_rollback_entry(tmp_path: Path):
    port, preflight_port = _free_port(), _free_port()
    screen, state = _fake_screen(tmp_path)
    good_entry = _good_entry(tmp_path)
    candidate = _candidate_entry(tmp_path, preflight_port)
    session = f"muselab-{port}"
    status = tmp_path / "status"
    startup_log = tmp_path / "startup.log"
    env = _base_env(tmp_path, screen, state)
    _start_baseline(screen, state, session, good_entry, port)
    try:
        subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--port",
                str(port),
                "--preflight-port",
                str(preflight_port),
                "--entry",
                str(candidate),
                "--rollback-entry",
                str(good_entry),
                "--session",
                session,
                "--status",
                str(status),
                "--startup-log",
                str(startup_log),
                "--watchdog-log",
                str(tmp_path / "watchdog.log"),
            ],
            check=True,
            env=env,
        )
        final = _wait_result(status, "rolled_back", timeout=15)
        assert final["phase"] == "done"
        assert final["new_pid"].isdigit()
        assert final["new_pid"] != final["old_pid"]
        _wait_health(port)
    finally:
        _stop_screen(screen, state, session)
