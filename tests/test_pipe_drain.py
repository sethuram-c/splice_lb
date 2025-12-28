#!/usr/bin/env python3
"""Phase 14: real pipe->socket EAGAIN, epoll write interest and FIFO integrity."""
from __future__ import annotations

import re
import select
import socket
import threading
import time

from test_bidirectional_buffers import wait_for_masks, watch_masks
from test_proxy import (recv_exact, recv_until_eof, run_backend, start_proxy,
                        stop_proxy, wait_for_proxy_cleanup)

# Large enough to fill Linux loopback send/receive buffers with a paused peer.
PAYLOAD = bytes(range(256)) * 32768   # 8 MiB, binary including embedded NUL


def wait_for_output_interest(pid: int, *, armed: bool, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    observed = []
    while time.monotonic() < deadline:
        observed = wait_for_masks(pid)
        has_out = any(mask & select.EPOLLOUT for mask in observed)
        if has_out == armed:
            return
        time.sleep(.01)
    raise AssertionError(f'EPOLLOUT armed={armed} not observed; masks={observed}')


def check_metrics(out: bytes, *, reverse: bool) -> None:
    hits = re.search(rb'pipe-drain-EAGAIN=(\d+)/(\d+)', out)
    assert hits, out
    up, down = map(int, hits.groups())
    if reverse:
        assert down > 0, f'backend->client drain never returned EAGAIN: {hits.groups()}'
    else:
        assert up > 0, f'client->backend drain never returned EAGAIN: {hits.groups()}'


def check_slow_client() -> None:
    allow_fin = threading.Event()
    def backend(conn: socket.socket) -> None:
        conn.settimeout(30)
        assert recv_until_eof(conn) == b'GO'
        conn.sendall(PAYLOAD)
        assert allow_fin.wait(20)
        conn.shutdown(socket.SHUT_WR)

    def action(backend_port: int) -> None:
        proc, port = start_proxy(backend_port)
        try:
            with socket.create_connection(('127.0.0.1', port), 15) as client:
                client.settimeout(30)
                client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
                client.sendall(b'GO')
                client.shutdown(socket.SHUT_WR)
                # Keep the reader idle until there are undelivered pipe bytes.
                wait_for_output_interest(proc.pid, armed=True)
                assert recv_exact(client, len(PAYLOAD)) == PAYLOAD
                # The backend leaves its write direction open until after
                # we inspect epoll: the 3 watches must still exist.
                wait_for_output_interest(proc.pid, armed=False)
                allow_fin.set()
                assert recv_until_eof(client) == b''
            wait_for_proxy_cleanup(proc)
            proc.terminate()
            out, err = proc.communicate(timeout=10)
            assert proc.returncode == -15 and not err, (out, err)
            assert f'backend-to-client={len(PAYLOAD)} bytes'.encode() in out
            check_metrics(out, reverse=True)
        finally:
            allow_fin.set()
            stop_proxy(proc)

    run_backend(backend, action)
    print('PASS: stalled client -> real pipe-drain EAGAIN -> EPOLLOUT retry -> idle disarm; 8 MiB intact')


def check_slow_backend() -> None:
    let_backend_read = threading.Event()
    allow_fin = threading.Event()
    send_errors: list[BaseException] = []

    def backend(conn: socket.socket) -> None:
        conn.settimeout(30)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
        assert let_backend_read.wait(20)
        assert recv_until_eof(conn) == PAYLOAD
        conn.sendall(b'ACK')
        assert allow_fin.wait(20)
        conn.shutdown(socket.SHUT_WR)

    def action(backend_port: int) -> None:
        proc, port = start_proxy(backend_port)
        try:
            with socket.create_connection(('127.0.0.1', port), 15) as client:
                client.settimeout(30)
                def send() -> None:
                    try:
                        client.sendall(PAYLOAD)
                        client.shutdown(socket.SHUT_WR)
                    except BaseException as ex:
                        send_errors.append(ex)
                sender = threading.Thread(target=send, daemon=True)
                sender.start()
                wait_for_output_interest(proc.pid, armed=True)
                let_backend_read.set()
                sender.join(30)
                assert not sender.is_alive(), 'upload thread blocked indefinitely'
                if send_errors:
                    raise send_errors[0]
                assert recv_exact(client, 3) == b'ACK'
                wait_for_output_interest(proc.pid, armed=False)
                allow_fin.set()
                assert recv_until_eof(client) == b''
            wait_for_proxy_cleanup(proc)
            proc.terminate()
            out, err = proc.communicate(timeout=10)
            assert proc.returncode == -15 and not err, (out, err)
            assert f'client-to-backend={len(PAYLOAD)} bytes'.encode() in out
            check_metrics(out, reverse=False)
        finally:
            let_backend_read.set()
            allow_fin.set()
            stop_proxy(proc)

    run_backend(backend, action)
    print('PASS: stalled backend -> real pipe-drain EAGAIN -> EPOLLOUT retry -> idle disarm; 8 MiB intact')


def main() -> None:
    check_slow_client()
    check_slow_backend()
    print('PASS: Phase 14 pipe-drain EAGAIN and event-interest updates test suite')

if __name__ == '__main__':
    main()
