"""Small PTY broker used by :mod:`backend.terminal`.

The web process deliberately does not call ``pty.fork()`` itself.  Python's
documentation warns that mixing ``forkpty`` with high-level system APIs is
unsafe on macOS, and muselab's FastAPI process has already initialized
threads, TLS and networking libraries.  This helper starts as a fresh,
single-threaded process, forks the user's shell immediately, then proxies the
PTY through a tiny framed protocol on stdin/stdout.

Frame format: one unsigned byte kind, one big-endian uint32 payload length,
then the payload.  Parent -> worker kinds are input/resize/terminate; worker ->
parent kinds are output/exit/error.
"""

from __future__ import annotations

import errno
import os
import pty
import selectors
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path


HEADER = struct.Struct("!BI")
SIZE = struct.Struct("!HH")
EXIT = struct.Struct("!i")

INPUT = 1
RESIZE = 2
TERMINATE = 3

OUTPUT = 1
EXITED = 2
ERROR = 3

MAX_FRAME = 256 * 1024


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _send(kind: int, payload: bytes = b"") -> None:
    _write_all(sys.stdout.fileno(), HEADER.pack(kind, len(payload)) + payload)


def _set_size(fd: int, payload: bytes) -> None:
    if len(payload) != SIZE.size:
        return
    rows, cols = SIZE.unpack(payload)
    rows = max(2, min(int(rows), 500))
    cols = max(2, min(int(cols), 1000))
    import fcntl

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _signal_terminal(pid: int, master_fd: int, sig: signal.Signals) -> None:
    """Signal both the login shell and its current foreground job.

    Interactive job control moves commands such as ``vim`` or ``npm test``
    into their own process group. Signalling only the shell's group would
    leave that foreground group orphaned when a terminal is closed.
    """
    # Cover background jobs as well as the shell/foreground groups. Every
    # process started from the PTY belongs to the login shell's session even
    # when job control gives it a distinct process group.
    try:
        listing = subprocess.check_output(
            ["ps", "-Ao", "pid=,sid="], text=True,
            stderr=subprocess.DEVNULL, timeout=1.0,
        )
        for line in listing.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == str(pid):
                try:
                    os.kill(int(fields[0]), sig)
                except (ProcessLookupError, PermissionError):
                    pass
    except (OSError, subprocess.SubprocessError):
        pass

    groups = {pid}
    try:
        foreground = os.tcgetpgrp(master_fd)
        if foreground > 0:
            groups.add(foreground)
    except OSError:
        pass
    for group in groups:
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            continue
        except OSError:
            try:
                os.kill(group, sig)
            except OSError:
                pass


def _parse_frames(buffer: bytearray) -> list[tuple[int, bytes]]:
    frames: list[tuple[int, bytes]] = []
    while len(buffer) >= HEADER.size:
        kind, length = HEADER.unpack(buffer[:HEADER.size])
        if length > MAX_FRAME:
            raise ValueError("terminal worker frame too large")
        total = HEADER.size + length
        if len(buffer) < total:
            break
        frames.append((kind, bytes(buffer[HEADER.size:total])))
        del buffer[:total]
    return frames


def run(shell: str, cwd: str, rows: int, cols: int) -> int:
    target = Path(cwd)
    if not target.is_dir():
        _send(ERROR, b"working directory does not exist")
        return 2

    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(target)
            argv0 = "-" + Path(shell).name
            os.execvpe(shell, [argv0], os.environ)
        except BaseException as exc:
            os.write(2, f"muselab: cannot start shell: {exc}\r\n".encode())
            os._exit(127)

    _set_size(master_fd, SIZE.pack(rows, cols))
    os.set_blocking(master_fd, False)
    os.set_blocking(sys.stdin.fileno(), False)

    selector = selectors.DefaultSelector()
    selector.register(master_fd, selectors.EVENT_READ, "pty")
    selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "control")
    control = bytearray()
    stopping = False
    stop_started: float | None = None

    def request_stop(_signum=None, _frame=None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    status: int | None = None
    try:
        while status is None:
            if stopping:
                if stop_started is None:
                    stop_started = time.monotonic()
                    _signal_terminal(pid, master_fd, signal.SIGTERM)
                elif time.monotonic() - stop_started >= 1.0:
                    _signal_terminal(pid, master_fd, signal.SIGKILL)
            for key, _ in selector.select(timeout=0.25):
                if key.data == "pty":
                    try:
                        chunk = os.read(master_fd, 16 * 1024)
                    except BlockingIOError:
                        continue
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            chunk = b""
                        else:
                            raise
                    if chunk:
                        _send(OUTPUT, chunk)
                    else:
                        try:
                            selector.unregister(master_fd)
                        except Exception:
                            pass
                else:
                    try:
                        chunk = os.read(sys.stdin.fileno(), 64 * 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        stopping = True
                        try:
                            selector.unregister(sys.stdin.fileno())
                        except Exception:
                            pass
                        continue
                    control.extend(chunk)
                    for kind, payload in _parse_frames(control):
                        if kind == INPUT:
                            if payload:
                                _write_all(master_fd, payload)
                        elif kind == RESIZE:
                            _set_size(master_fd, payload)
                            try:
                                os.killpg(pid, signal.SIGWINCH)
                            except OSError:
                                pass
                        elif kind == TERMINATE:
                            stopping = True

            waited, raw = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = os.waitstatus_to_exitcode(raw)

        _send(EXITED, EXIT.pack(status))
        return 0
    except BaseException as exc:
        _send(ERROR, str(exc).encode("utf-8", "replace")[:4096])
        _signal_terminal(pid, master_fd, signal.SIGKILL)
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        return 1
    finally:
        selector.close()
        try:
            os.close(master_fd)
        except OSError:
            pass


def main() -> int:
    if len(sys.argv) != 5:
        return 2
    shell, cwd, rows_raw, cols_raw = sys.argv[1:]
    try:
        rows = max(2, min(int(rows_raw), 500))
        cols = max(2, min(int(cols_raw), 1000))
    except ValueError:
        return 2
    return run(shell, cwd, rows, cols)


if __name__ == "__main__":
    raise SystemExit(main())
