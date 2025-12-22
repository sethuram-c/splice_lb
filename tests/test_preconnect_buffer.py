#!/usr/bin/env python3
"""Phase 8: deterministic preconnect staging, bounded buffer, FIN, recovery."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_proxy import (TIMEOUT, recv_until_eof, run_backend, start_proxy,
                        stop_proxy, wait_for_proxy_cleanup)
from test_multiclient import EchoHandler, ThreadedBackend

ROOT = Path(__file__).resolve().parents[1]


def delayed_connect_environment(directory: str) -> dict[str, str]:
    lib = Path(directory) / "delay_backend_ready.so"
    subprocess.run([
        os.environ.get("CC", "cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-fPIC", "-shared", str(ROOT / "tests" / "delay_backend_ready.c"),
        "-o", str(lib), "-ldl",
    ], check=True, capture_output=True, timeout=TIMEOUT)
    return dict(os.environ, LD_PRELOAD=str(lib))


def check_early_bytes_with_deferred_ready(payload: bytes, *, expected_stage: int) -> None:
    """The shim holds the backend readiness event while client EPOLLIN runs."""
    def backend(conn: socket.socket) -> None:
        request = recv_until_eof(conn)
        assert request == payload, (len(request), len(payload))
        conn.sendall(b"ACK:" + request[::-1])

    def exercise(backend_port: int) -> None:
        with tempfile.TemporaryDirectory(prefix="splice-phase8-") as directory:
            env = delayed_connect_environment(directory)
            proc, proxy_port = start_proxy(backend_port, env=env)
            try:
                with socket.create_connection(("127.0.0.1", proxy_port), TIMEOUT) as client:
                    client.settimeout(TIMEOUT)
                    if payload:
                        client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                    response = recv_until_eof(client)
                assert response == b"ACK:" + payload[::-1], len(response)
                wait_for_proxy_cleanup(proc)
                assert proc.poll() is None, "proxy stopped after client finished"
                proc.terminate()
                output, errors = proc.communicate(timeout=TIMEOUT)
                assert proc.returncode == -15, (proc.returncode, errors)
                stages = re.findall(rb"preconnect-buffered=(\d+) bytes", output)
                assert stages == [str(expected_stage).encode()], (stages, output)
                counts = (f"client-to-backend={len(payload)} bytes, "
                          f"backend-to-client={len(response)} bytes").encode()
                assert counts in output, output
            finally:
                stop_proxy(proc)

    run_backend(backend, exercise)
    print(f"PASS: delayed backend connection safely stages {expected_stage} of "
          f"{len(payload)} client bytes")


def check_concurrent_larger_than_staging_buffer() -> None:
    """Multiple large early sends must not exceed one connection's buffer."""
    backend = ThreadedBackend(("127.0.0.1", 0), EchoHandler)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    proc, port = start_proxy(backend.server_address[1])
    try:
        def request(index: int) -> None:
            payload = (b"early-" + index.to_bytes(2, "big") + bytes(range(256))) * 80
            with socket.create_connection(("127.0.0.1", port), TIMEOUT) as conn:
                conn.settimeout(TIMEOUT)
                conn.sendall(payload)
                conn.shutdown(socket.SHUT_WR)
                assert recv_until_eof(conn) == b"ACK:" + payload[::-1]
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(request, range(24)))
        wait_for_proxy_cleanup(proc)
        assert proc.poll() is None
    finally:
        stop_proxy(proc)
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=TIMEOUT)
    print("PASS: 24 concurrent clients forward >8 KiB requests intact")


def check_refusal_with_queued_client_data() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        proc, port = start_proxy(reserved.getsockname()[1])
        try:
            with socket.create_connection(("127.0.0.1", port), TIMEOUT) as conn:
                conn.settimeout(TIMEOUT)
                try:
                    conn.sendall(b"data before refused connect")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                try:
                    assert conn.recv(1) == b""
                except ConnectionResetError:
                    pass  # unread client data can legitimately produce TCP RST
            wait_for_proxy_cleanup(proc)
            assert proc.poll() is None
        finally:
            stop_proxy(proc)
    print("PASS: refused backend with early client data does not kill listener")


def main() -> None:
    check_early_bytes_with_deferred_ready(bytes(range(256)) * 1024,
                                          expected_stage=8192)
    check_early_bytes_with_deferred_ready(b"hello before connected",
                                          expected_stage=len(b"hello before connected"))
    check_early_bytes_with_deferred_ready(b"", expected_stage=0)
    check_concurrent_larger_than_staging_buffer()
    check_refusal_with_queued_client_data()
    print("PASS: Phase 8 client preconnect buffering test suite")


if __name__ == "__main__":
    main()
