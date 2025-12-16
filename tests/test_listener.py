#!/usr/bin/env python3
"""End-to-end listener checks; uses only Python's standard library."""

from __future__ import annotations

import re
import selectors
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "build" / "splice-lb"
TIMEOUT_SECONDS = 4


def run_case_with_client() -> None:
    proc = subprocess.Popen(
        [str(BINARY), "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            if not selector.select(TIMEOUT_SECONDS):
                raise AssertionError("server did not announce its port")
            first_line = proc.stdout.readline().strip()

        match = re.fullmatch(r"Listening on 127\.0\.0\.1:([0-9]+)", first_line)
        assert match is not None, f"unexpected startup output: {first_line!r}"
        port = int(match.group(1))
        assert 1 <= port <= 65535
        print("PASS: listener binds loopback on an OS-selected port")

        with socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT_SECONDS) as client:
            assert client.getpeername()[1] == port

        remaining_out, stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
        assert proc.returncode == 0, f"server exit {proc.returncode}: {stderr}"
        lines = remaining_out.splitlines()
        assert len(lines) == 2, f"unexpected server output: {lines!r}"
        assert re.fullmatch(r"Accepted client 127\.0\.0\.1:[0-9]+ \(fd=[0-9]+\)", lines[0]), lines
        assert lines[1] == "Closed client and listening sockets", lines
        print("PASS: server accepts one TCP client and exits cleanly")

        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                raise AssertionError("server still listening after clean exit")
        except ConnectionRefusedError:
            pass
        print("PASS: listener no longer accepts connections after exit")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=TIMEOUT_SECONDS)


def run_invalid_port_tests() -> None:
    for value in ("-1", "+1", "65536", "abc", "12x", ""):
        completed = subprocess.run(
            [str(BINARY), value], capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, check=False,
        )
        assert completed.returncode != 0, value
        assert "Usage:" in completed.stderr, (value, completed.stderr)
    print("PASS: invalid port arguments are rejected")


def run_bind_failure_test() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        busy_port = busy.getsockname()[1]
        completed = subprocess.run(
            [str(BINARY), str(busy_port)],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
        )
        assert completed.returncode != 0
        assert "bind() failed:" in completed.stderr, completed.stderr
    print("PASS: occupied listening ports report bind() failure")


def main() -> None:
    run_case_with_client()
    run_invalid_port_tests()
    run_bind_failure_test()
    print("PASS: Phase 2 listener test suite")


if __name__ == "__main__":
    main()
