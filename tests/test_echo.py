#!/usr/bin/env python3
"""Real TCP echo and backend-client regression tests; stdlib only."""

from __future__ import annotations

import re
import selectors
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "build" / "echo-backend"
CLIENT = ROOT / "build" / "backend-client"
TIMEOUT = 5


def start_server() -> tuple[subprocess.Popen[bytes], int]:
    proc = subprocess.Popen(
        [str(SERVER), "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            if not selector.select(TIMEOUT):
                raise AssertionError("server did not announce a listening port")
            first_line = proc.stdout.readline().decode("ascii").strip()
        match = re.fullmatch(r"Listening on 127\.0\.0\.1:([0-9]+)", first_line)
        assert match is not None, first_line
        return proc, int(match.group(1))
    except BaseException:
        stop_server(proc)
        raise


def stop_server(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=TIMEOUT)


def verify_exit(proc: subprocess.Popen[bytes]) -> None:
    out, err = proc.communicate(timeout=TIMEOUT)
    assert proc.returncode == 0, (proc.returncode, err.decode(errors="replace"))
    assert re.search(rb"Accepted client 127\.0\.0\.1:\d+ \(fd=\d+\)", out), out
    assert b"Closed client and listening sockets" in out, out


def check_echo(payload: bytes, label: str) -> None:
    proc, port = start_server()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT) as conn:
            conn.settimeout(TIMEOUT)
            conn.sendall(payload)
            conn.shutdown(socket.SHUT_WR)
            received = bytearray()
            while True:
                chunk = conn.recv(8192)
                if not chunk:
                    break
                received.extend(chunk)
        assert received == payload, (label, len(payload), len(received))
        verify_exit(proc)
        print(f"PASS: {label} ({len(payload)} bytes, exact echo)")
    finally:
        stop_server(proc)


def check_backend_client(payload: str) -> None:
    proc, port = start_server()
    try:
        result = subprocess.run(
            [str(CLIENT), "127.0.0.1", str(port), payload],
            capture_output=True, timeout=TIMEOUT, check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert result.stdout == payload.encode(), (result.stdout, payload)
        verify_exit(proc)
        print("PASS: backend client connects, sends, half-closes, reads echo")
    finally:
        stop_server(proc)


def check_client_arguments() -> None:
    for args in (
        [], ["127.0.0.1", "0", "x"], ["127.0.0.1", "65536", "x"],
        ["127.0.0.1", "-1", "x"], ["invalid", "9090", "x"],
    ):
        result = subprocess.run(
            [str(CLIENT), *args], capture_output=True, timeout=TIMEOUT, check=False,
        )
        assert result.returncode != 0, args
        assert result.stderr, args
    print("PASS: backend client rejects malformed endpoints and arguments")


def main() -> None:
    check_echo(b"", "empty payload and EOF")
    check_echo(b"hello from phase 3", "small text")
    check_echo(bytes(range(256)) + b"\x00\xff\r\n", "binary-safe payload")
    check_echo(bytes(range(256)) * 512, "128 KiB multi-chunk transfer")
    check_backend_client("backend client round trip")
    check_client_arguments()
    print("PASS: Phase 3 echo and backend client test suite")


if __name__ == "__main__":
    main()
