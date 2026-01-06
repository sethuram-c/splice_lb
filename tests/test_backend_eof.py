#!/usr/bin/env python3
"""Phase 22: backend FIN and client FIN are independent, with lossless teardown.

Uses only loopback sockets; payloads are deterministic binary streams.
"""
from __future__ import annotations

import socket
import threading
import time

from test_proxy import (TIMEOUT, recv_exact, recv_until_eof, run_backend,
                        start_proxy, stop_proxy, wait_for_proxy_cleanup)


ACK = b'BACKEND-FIN-EARLY\0' + bytes(range(256)) * 2


def assert_graceful(proc, upload: int, response: int, *, backend_first: bool) -> None:
    wait_for_proxy_cleanup(proc)
    assert proc.poll() is None, 'listener exited after a completed connection'
    proc.terminate()
    out, err = proc.communicate(timeout=TIMEOUT)
    assert proc.returncode == -15 and not err, (out, err)
    assert out.count(b'backend-eof-state=observed;') == 1, out
    assert out.count(b'backend-eof-state=forwarded;') == 1, out
    assert b'backend-eof-final=forwarded;' in out, out
    assert b'client-eof-final=forwarded;' in out, out
    assert b'connection-completion=graceful; both-fins-forwarded=yes;' in out, out
    assert b'queued-to-backend=0; queued-to-client=0' in out, out
    assert b'transfer-integrity=verified' in out, out
    assert f'client-to-backend={upload} bytes'.encode() in out, out
    assert f'backend-to-client={response} bytes'.encode() in out, out
    if backend_first:
        # Once the backend announces FIN, it can still RECEIVE the upload.
        assert b'backend-eof-state=observed;' in out, out
        assert b'client-upload-done=no' in out, out


def backend_fin_before_client_upload(*, empty_reply: bool) -> None:
    upload = bytes(range(256)) * 8192  # 2 MiB after backend sends FIN
    reply = b'' if empty_reply else ACK

    def backend(conn: socket.socket) -> None:
        conn.settimeout(30)
        if reply:
            conn.sendall(reply)
        conn.shutdown(socket.SHUT_WR)  # write FIN first; still read client bytes
        assert recv_until_eof(conn) == upload, 'backend lost late client upload'

    def client(backend_port: int) -> None:
        proc, proxy_port = start_proxy(backend_port)
        try:
            with socket.create_connection(('127.0.0.1', proxy_port), TIMEOUT) as sock:
                sock.settimeout(30)
                # The backend FIN must arrive before we send *any* upload.
                assert recv_until_eof(sock) == reply, 'backend reply/FIN missing'
                sock.sendall(upload)
                sock.shutdown(socket.SHUT_WR)
            assert_graceful(proc, len(upload), len(reply), backend_first=True)
        finally:
            stop_proxy(proc)

    run_backend(backend, client)
    print(f'PASS: backend FIN first, reply={len(reply)} bytes, then 2 MiB upload intact')


def delayed_client_read_after_backend_fin() -> None:
    upload = bytes(range(256)) * 4096  # 1 MiB
    response = bytes(range(255, -1, -1)) * 16384  # 4 MiB

    def backend(conn: socket.socket) -> None:
        conn.settimeout(45)
        # Simultaneous duplex traffic without deadlocking on the TCP windows.
        errors: list[BaseException] = []

        def send_response() -> None:
            try:
                conn.sendall(response)
                conn.shutdown(socket.SHUT_WR)
            except BaseException as exc:
                errors.append(exc)

        t = threading.Thread(target=send_response, daemon=True)
        t.start()
        assert recv_until_eof(conn) == upload, 'backend lost request during reply'
        t.join(timeout=40)
        assert not t.is_alive(), 'backend send stuck'
        if errors:
            raise errors[0]

    def client(backend_port: int) -> None:
        proc, proxy_port = start_proxy(backend_port)
        try:
            with socket.create_connection(('127.0.0.1', proxy_port), TIMEOUT) as sock:
                sock.settimeout(45)
                sock.sendall(upload)
                sock.shutdown(socket.SHUT_WR)
                # Slow reader forces downstream buffering/EPOLLOUT retries.
                time.sleep(.18)
                assert recv_until_eof(sock) == response, 'truncated delayed response'
            assert_graceful(proc, len(upload), len(response), backend_first=False)
        finally:
            stop_proxy(proc)

    run_backend(backend, client)
    print('PASS: delayed client receives full 4 MiB response after upload FIN')


def main() -> None:
    backend_fin_before_client_upload(empty_reply=False)
    backend_fin_before_client_upload(empty_reply=True)
    delayed_client_read_after_backend_fin()
    print('PASS: Phase 22 backend half-close and graceful connection completion test suite')


if __name__ == '__main__':
    main()
