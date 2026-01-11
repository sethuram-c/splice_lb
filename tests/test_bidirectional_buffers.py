#!/usr/bin/env python3
"""Phase 9: active two-way byte queues, idle EPOLLOUT, real TCP backpressure."""
from __future__ import annotations

import re
import select
import socket
import threading
import time
from pathlib import Path

from test_proxy import (TIMEOUT, recv_exact, recv_until_eof, run_backend,
                        start_proxy, stop_proxy, wait_for_proxy_cleanup)
from test_epoll import kernel_descriptors

LARGE = bytes(range(256)) * 32768  # 8 MiB; deterministic binary including NUL


def watch_masks(pid: int) -> list[int]:
    _, epolls = kernel_descriptors(pid)
    assert len(epolls) == 1, epolls
    info = (Path('/proc') / str(pid) / 'fdinfo' / str(epolls[0])).read_text()
    return [int(mask, 16) for mask in re.findall(r'^tfd:\s*\d+\s+events:\s*([0-9a-fA-F]+)',
                                                info, re.MULTILINE)]


def wait_for_masks(pid: int, expected: int = 4) -> list[int]:
    end = time.monotonic() + TIMEOUT
    while time.monotonic() < end:
        masks = watch_masks(pid)
        if len(masks) == expected:
            return masks
        time.sleep(0.01)
    raise AssertionError(('did not see required epoll watches', masks))


def check_idle_no_epollout() -> None:
    backend_ready = threading.Event()

    def backend(conn: socket.socket) -> None:
        backend_ready.set()
        assert recv_until_eof(conn) == b'quiet'
        conn.sendall(b'OK')

    def action(backend_port: int) -> None:
        proc, port = start_proxy(backend_port)
        try:
            with socket.create_connection(('127.0.0.1', port), TIMEOUT) as client:
                client.settimeout(TIMEOUT)
                assert backend_ready.wait(TIMEOUT)
                # Allow the connect completion event to change the mask.
                end = time.monotonic() + TIMEOUT
                while True:
                    masks = wait_for_masks(proc.pid)
                    if not any(mask & select.EPOLLOUT for mask in masks):
                        break
                    if time.monotonic() > end:
                        raise AssertionError(('EPOLLOUT still armed without queued bytes', masks))
                    time.sleep(0.01)
                # A second sample guards against an ephemeral snapshot during setup.
                time.sleep(0.08)
                assert not any(mask & select.EPOLLOUT for mask in watch_masks(proc.pid))
                client.sendall(b'quiet')
                client.shutdown(socket.SHUT_WR)
                assert recv_until_eof(client) == b'OK'
            wait_for_proxy_cleanup(proc)
        finally:
            stop_proxy(proc)

    run_backend(backend, action)
    print('PASS: 4 epoll watches; no EPOLLOUT subscription on idle connections')


def check_slow_client() -> None:
    """Backend emits 8 MiB while proxy client deliberately does not read."""
    backend_sent = threading.Event()

    def backend(conn: socket.socket) -> None:
        assert recv_until_eof(conn) == b'GO'
        conn.sendall(LARGE)
        conn.shutdown(socket.SHUT_WR)
        backend_sent.set()

    def action(backend_port: int) -> None:
        proc, port = start_proxy(backend_port)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
                client.settimeout(20)
                client.connect(('127.0.0.1', port))
                client.sendall(b'GO')
                client.shutdown(socket.SHUT_WR)
                seen_write_interest = False
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    masks = wait_for_masks(proc.pid)
                    if any(mask & select.EPOLLOUT for mask in masks):
                        seen_write_interest = True
                        break
                    time.sleep(0.02)
                assert seen_write_interest, 'slow client never triggered pending-output EPOLLOUT'
                # Consume the large binary stream, allowing both pipe buffers to drain.
                data = recv_until_eof(client)
                assert data == LARGE, (len(data), len(LARGE))
            assert backend_sent.wait(TIMEOUT)
            wait_for_proxy_cleanup(proc)
        finally:
            stop_proxy(proc)

    run_backend(backend, action)
    print('PASS: slow client triggers EPOLLOUT; 8 MiB backend->client intact')


def check_slow_backend() -> None:
    """A paused reader must not lose client upload data or block other sessions."""
    hold = threading.Event()
    errors: list[BaseException] = []

    def backend(conn: socket.socket) -> None:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
        assert hold.wait(TIMEOUT), 'backend release not signaled'
        assert recv_until_eof(conn) == LARGE
        conn.sendall(b'COMPLETE')

    def action(backend_port: int) -> None:
        proc, port = start_proxy(backend_port)
        try:
            with socket.create_connection(('127.0.0.1', port), 20) as client:
                client.settimeout(30)
                def sender() -> None:
                    try:
                        client.sendall(LARGE)
                        client.shutdown(socket.SHUT_WR)
                    except BaseException as exc:
                        errors.append(exc)
                thread = threading.Thread(target=sender, daemon=True)
                thread.start()
                seen_write_interest = False
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    masks = wait_for_masks(proc.pid)
                    if any(mask & select.EPOLLOUT for mask in masks):
                        seen_write_interest = True
                        break
                    time.sleep(0.02)
                assert seen_write_interest, 'slow backend never triggered pending-output EPOLLOUT'
                hold.set()
                thread.join(30)
                assert not thread.is_alive(), 'client upload failed to drain'
                if errors:
                    raise errors[0]
                assert recv_until_eof(client) == b'COMPLETE'
            wait_for_proxy_cleanup(proc)
        finally:
            hold.set()
            stop_proxy(proc)

    run_backend(backend, action)
    print('PASS: slow backend triggers EPOLLOUT; 8 MiB client->backend intact')


def main() -> None:
    check_idle_no_epollout()
    check_slow_client()
    check_slow_backend()
    print('PASS: Phase 9 bidirectional buffering and efficient EPOLLOUT test suite')


if __name__ == '__main__':
    main()
