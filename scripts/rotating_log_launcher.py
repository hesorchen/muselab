#!/usr/bin/env python3
"""Run a command while rotating its combined stdout/stderr by size."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path


def _rotate(log_path: Path, keep: int) -> None:
    if keep <= 0:
        log_path.unlink(missing_ok=True)
        return
    oldest = log_path.with_name(f"{log_path.name}.{keep}")
    oldest.unlink(missing_ok=True)
    for generation in range(keep - 1, 0, -1):
        source = log_path.with_name(f"{log_path.name}.{generation}")
        if source.exists():
            source.replace(log_path.with_name(
                f"{log_path.name}.{generation + 1}"))
    if log_path.exists():
        log_path.replace(log_path.with_name(f"{log_path.name}.1"))


def run(command: list[str], *, log_path: Path, max_bytes: int, keep: int) -> int:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if keep < 0:
        raise ValueError("keep must be non-negative")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.is_symlink():
        raise ValueError("log path must not be a symlink")
    if log_path.exists() and not log_path.is_file():
        raise ValueError("log path must be a regular file")
    if log_path.exists() and log_path.stat().st_size >= max_bytes:
        _rotate(log_path, keep)

    child = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    def forward(signum, _frame) -> None:
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    forwarded = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous = {signum: signal.signal(signum, forward) for signum in forwarded}
    try:
        size = log_path.stat().st_size if log_path.exists() else 0
        output = child.stdout
        assert output is not None
        sink = log_path.open("ab", buffering=0)
        try:
            while True:
                chunk = output.read1(64 * 1024)
                if not chunk:
                    break
                remaining = memoryview(chunk)
                while remaining:
                    if size >= max_bytes:
                        sink.close()
                        _rotate(log_path, keep)
                        sink = log_path.open("ab", buffering=0)
                        size = 0
                    part = remaining[:max_bytes - size]
                    sink.write(part)
                    size += len(part)
                    remaining = remaining[len(part):]
        finally:
            sink.close()
        return_code = child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            child.wait()
    return 128 - return_code if return_code < 0 else return_code


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--max-bytes", type=_positive_int, default=50 * 1024 * 1024)
    parser.add_argument("--keep", type=_nonnegative_int, default=5)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    try:
        return run(command, log_path=args.log,
                   max_bytes=args.max_bytes, keep=args.keep)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
