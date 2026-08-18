import os
import signal
import subprocess
import sys
import time
from pathlib import Path


LAUNCHER = Path(__file__).parents[1] / "scripts" / "rotating_log_launcher.py"


def test_launcher_rotates_combined_output_by_size(tmp_path):
    log = tmp_path / "muselab.log"
    code = (
        "import sys; "
        "[(print(f'line-{i:02d}-' + 'x' * 20, flush=True), "
        "  print(f'err-{i:02d}', file=sys.stderr, flush=True)) for i in range(12)]"
    )

    result = subprocess.run(
        [sys.executable, str(LAUNCHER), "--log", str(log),
         "--max-bytes", "100", "--keep", "2", "--",
         sys.executable, "-c", code],
        check=False,
        timeout=5,
    )

    assert result.returncode == 0
    assert log.exists()
    assert (tmp_path / "muselab.log.1").exists()
    assert (tmp_path / "muselab.log.2").exists()
    assert not (tmp_path / "muselab.log.3").exists()
    retained = b"".join(path.read_bytes() for path in (
        tmp_path / "muselab.log.2",
        tmp_path / "muselab.log.1",
        log,
    ))
    assert b"line-11-" in retained
    assert b"err-11" in retained


def test_launcher_forwards_sigterm_and_returns_child_status(tmp_path):
    log = tmp_path / "muselab.log"
    code = """
import signal
import sys
import time

def stop(_signum, _frame):
    print("child-got-term", flush=True)
    raise SystemExit(42)

signal.signal(signal.SIGTERM, stop)
print("child-ready", flush=True)
while True:
    time.sleep(0.05)
"""
    process = subprocess.Popen([
        sys.executable, str(LAUNCHER), "--log", str(log), "--",
        sys.executable, "-c", code,
    ])
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if log.exists() and b"child-ready" in log.read_bytes():
                break
            time.sleep(0.02)
        else:
            raise AssertionError("child did not become ready")

        os.kill(process.pid, signal.SIGTERM)
        assert process.wait(timeout=2) == 42
        assert b"child-got-term" in log.read_bytes()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
