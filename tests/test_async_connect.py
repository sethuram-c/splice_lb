#!/usr/bin/env python3
"""Phase 7: prove O_NONBLOCK was set *before* connect() and SO_ERROR handled."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_proxy import (TIMEOUT, recv_until_eof, start_proxy,
                        stop_proxy, wait_for_proxy_cleanup)
from test_multiclient import EchoHandler, ThreadedBackend

ROOT = Path(__file__).resolve().parents[1]


def check_nonblocking_at_connect() -> None:
    """Instrument the real proxy without altering its source or TCP behavior."""
    with tempfile.TemporaryDirectory(prefix="splice-phase7-") as directory:
        shared_object = Path(directory) / "connect_observer.so"
        log = Path(directory) / "connect.log"
        subprocess.run([
            os.environ.get("CC", "cc"), "-std=c11", "-Wall", "-Wextra",
            "-Werror", "-fPIC", "-shared",
            str(ROOT / "tests" / "connect_observer.c"), "-o", str(shared_object),
        ], check=True, capture_output=True, timeout=TIMEOUT)
        env = os.environ.copy()
        env["LD_PRELOAD"] = str(shared_object)
        env["CONNECT_AUDIT_FILE"] = str(log)

        backend = ThreadedBackend(("127.0.0.1", 0), EchoHandler)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        proc, port = start_proxy(backend.server_address[1], env=env)
        try:
            def request(index: int) -> None:
                payload = b"early-send:" + index.to_bytes(4, "big") + bytes(range(256))
                with socket.create_connection(("127.0.0.1", port), TIMEOUT) as conn:
                    conn.settimeout(TIMEOUT)
                    # Deliberately send before backend connect can be guaranteed ready.
                    conn.sendall(payload)
                    conn.shutdown(socket.SHUT_WR)
                    assert recv_until_eof(conn) == b"ACK:" + payload[::-1]

            with ThreadPoolExecutor(max_workers=12) as executor:
                list(executor.map(request, range(36)))
            wait_for_proxy_cleanup(proc)
            lines = log.read_text().splitlines()
            assert len(lines) == 36, (len(lines), lines)
            assert all(re.fullmatch(r"nonblocking=1 result=(?:-1|0) error=\d+", line)
                       for line in lines), lines
            assert proc.poll() is None, "concurrent connections stopped the proxy"
        finally:
            stop_proxy(proc)
            backend.shutdown()
            backend.server_close()
            thread.join(timeout=TIMEOUT)
        print("PASS: 36 backend connect() calls entered with O_NONBLOCK set")
        print("PASS: 36 early client payloads preserved through async connect")


def check_async_refusal_and_recovery() -> None:
    # No process is listening on this reserved local TCP port.  The kernel
    # reports failure either from connect() or later via SO_ERROR.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        proc, port = start_proxy(reserved.getsockname()[1])
        try:
            for _ in range(8):
                with socket.create_connection(("127.0.0.1", port), TIMEOUT) as conn:
                    conn.settimeout(TIMEOUT)
                    assert conn.recv(1) == b"", "failed backend must close client"
            wait_for_proxy_cleanup(proc)
            assert proc.poll() is None, "backend refusal killed the listener"
            proc.terminate()
            out, err = proc.communicate(timeout=TIMEOUT)
            assert b"connect(backend) failed:" in err, (out, err)
            assert b"Connected to backend" not in out, out
        finally:
            stop_proxy(proc)
    print("PASS: 8 async backend refusals cleanly isolated; listener stays live")


def main() -> None:
    check_nonblocking_at_connect()
    check_async_refusal_and_recovery()
    print("PASS: Phase 7 nonblocking backend connection test suite")


if __name__ == "__main__":
    main()
