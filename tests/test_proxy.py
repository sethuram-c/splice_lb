#!/usr/bin/env python3
"""Phase 4 forwarding regression checks adapted for Phase 6 persistent proxy."""

from __future__ import annotations

import re
import os
import time
import selectors
import socket
import subprocess
import threading
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "build" / "splice-lb"
CLIENT = ROOT / "build" / "backend-client"
TIMEOUT = 8.0


def start_proxy(backend_port: int) -> tuple[subprocess.Popen[bytes], int]:
    proc = subprocess.Popen(
        [str(PROXY), "0", "127.0.0.1", str(backend_port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            if not selector.select(TIMEOUT):
                raise AssertionError("proxy did not announce its port")
            first = proc.stdout.readline().decode("ascii").strip()
        matched = re.fullmatch(
            rf"Listening on 127\.0\.0\.1:(\d+) -> 127\.0\.0\.1:{backend_port}", first
        )
        assert matched, first
        return proc, int(matched.group(1))
    except BaseException:
        stop_proxy(proc)
        raise


def stop_proxy(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=TIMEOUT)


def wait_for_proxy_cleanup(proc: subprocess.Popen[bytes]) -> None:
    """Wait until only the listener remains; avoids racing log assertions."""
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        try:
            targets = [os.readlink(entry) for entry in
                       (Path("/proc") / str(proc.pid) / "fd").iterdir()]
        except (FileNotFoundError, ProcessLookupError):
            break
        if sum(target.startswith("socket:[") for target in targets) == 1:
            return
        time.sleep(0.01)
    raise AssertionError("proxy did not finish and release its session sockets")


def completed_proxy(proc: subprocess.Popen[bytes], forward: int, backward: int) -> None:
    # Phase 6 keeps listening after a completed session; terminate only the
    # test process, then verify that its per-connection accounting was printed.
    wait_for_proxy_cleanup(proc)
    assert proc.poll() is None, "proxy unexpectedly exited after client"
    proc.terminate()
    out, err = proc.communicate(timeout=TIMEOUT)
    assert proc.returncode == -15, (proc.returncode, err.decode(errors="replace"))
    assert b"Connected to backend" in out, out
    assert (f"Closed connection: client-to-backend={forward} bytes, "
            f"backend-to-client={backward} bytes; active=0").encode() in out, out


def run_backend(handler: Callable[[socket.socket], None],
                action: Callable[[int], None]) -> None:
    """Raise failures from the backend thread in the test's main thread."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen(1)
        listening.settimeout(TIMEOUT)
        address = listening.getsockname()[1]
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                with listening.accept()[0] as conn:
                    conn.settimeout(TIMEOUT)
                    handler(conn)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            action(address)
        finally:
            thread.join(timeout=TIMEOUT)
        assert not thread.is_alive(), "backend thread hung"
        if errors:
            raise errors[0]


def recv_exact(conn: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise AssertionError("premature TCP EOF")
        data.extend(chunk)
    return bytes(data)


def recv_until_eof(conn: socket.socket) -> bytes:
    chunks = []
    while data := conn.recv(65536):
        chunks.append(data)
    return b"".join(chunks)


def check_request_response(payload: bytes) -> None:
    def backend(conn: socket.socket) -> None:
        request = recv_until_eof(conn)
        assert request == payload
        # Request EOF reaches backend; client must remain able to read reply.
        conn.sendall(b"ACK:" + request[::-1])

    def action(port: int) -> None:
        proc, proxy_port = start_proxy(port)
        try:
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=TIMEOUT) as conn:
                conn.settimeout(TIMEOUT)
                conn.sendall(payload)
                conn.shutdown(socket.SHUT_WR)
                response = recv_until_eof(conn)
            assert response == b"ACK:" + payload[::-1], len(response)
            completed_proxy(proc, len(payload), len(response))
        finally:
            stop_proxy(proc)

    run_backend(backend, action)
    print(f"PASS: request/response, partial chunks, integrity ({len(payload)} bytes)")


def check_backend_first() -> None:
    def backend(conn: socket.socket) -> None:
        conn.sendall(b"WELCOME\n")
        assert recv_until_eof(conn) == b"ping"
        conn.sendall(b"PONG\n")

    def action(port: int) -> None:
        proc, proxy_port = start_proxy(port)
        try:
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=TIMEOUT) as conn:
                conn.settimeout(TIMEOUT)
                assert recv_exact(conn, 8) == b"WELCOME\n"
                conn.sendall(b"ping")
                conn.shutdown(socket.SHUT_WR)
                assert recv_until_eof(conn) == b"PONG\n"
            completed_proxy(proc, 4, 13)
        finally:
            stop_proxy(proc)

    run_backend(backend, action)
    print("PASS: backend-first greeting and bidirectional interleaving")


def check_backend_half_close_first() -> None:
    def backend(conn: socket.socket) -> None:
        conn.sendall(b"READY")
        conn.shutdown(socket.SHUT_WR)
        assert recv_until_eof(conn) == b"upload"

    def action(port: int) -> None:
        proc, proxy_port = start_proxy(port)
        try:
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=TIMEOUT) as conn:
                conn.settimeout(TIMEOUT)
                assert recv_until_eof(conn) == b"READY"
                # Backend's FIN must NOT close the other direction.
                conn.sendall(b"upload")
                conn.shutdown(socket.SHUT_WR)
            completed_proxy(proc, 6, 5)
        finally:
            stop_proxy(proc)

    run_backend(backend, action)
    print("PASS: backend half-close does not discard pending client upload")


def check_backend_client() -> None:
    def backend(conn: socket.socket) -> None:
        data = recv_until_eof(conn)
        assert data == b"client through proxy"
        conn.sendall(data)

    def action(port: int) -> None:
        proc, proxy_port = start_proxy(port)
        try:
            completed = subprocess.run(
                [str(CLIENT), "127.0.0.1", str(proxy_port), "client through proxy"],
                capture_output=True, timeout=TIMEOUT, check=False,
            )
            assert completed.returncode == 0, completed.stderr
            assert completed.stdout == b"client through proxy"
            completed_proxy(proc, 20, 20)
        finally:
            stop_proxy(proc)

    run_backend(backend, action)
    print("PASS: Phase 3 backend-client connects through Phase 4 proxy")


def check_backend_refusal() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        refused_port = reservation.getsockname()[1]
        proc, proxy_port = start_proxy(refused_port)
        try:
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=TIMEOUT) as conn:
                conn.settimeout(TIMEOUT)
                assert conn.recv(1) == b"", "refused backend should close the client"
            # A failed backend is isolated; the persistent listener survives.
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=TIMEOUT) as conn:
                conn.settimeout(TIMEOUT)
                assert conn.recv(1) == b""
            assert proc.poll() is None, "failed backend killed the entire proxy"
            proc.terminate()
            _, error = proc.communicate(timeout=TIMEOUT)
            assert b"connect(backend) failed:" in error, error
        finally:
            stop_proxy(proc)
    print("PASS: refused backend affects only its client; listener survives")


def check_arguments() -> None:
    for args in ((), ("0", "127.0.0.1"),
                 ("-1", "127.0.0.1", "80"),
                 ("65536", "127.0.0.1", "80"),
                 ("0", "bad-address", "80"),
                 ("0", "127.0.0.1", "0"),
                 ("0", "127.0.0.1", "65536")):
        result = subprocess.run([str(PROXY), *args], capture_output=True,
                                timeout=TIMEOUT, check=False)
        assert result.returncode != 0 and b"Usage:" in result.stderr, args
    print("PASS: malformed listener/backend arguments rejected")


def main() -> None:
    check_arguments()
    check_request_response(b"")
    check_request_response(bytes(range(256)) + b"\0END\xff")
    check_request_response(bytes(range(256)) * 1024)  # 256 KiB
    check_backend_first()
    check_backend_half_close_first()
    check_backend_client()
    check_backend_refusal()
    print("PASS: Phase 4 proxy forwarding regression suite (Phase 6 persistent mode)")


if __name__ == "__main__":
    main()
