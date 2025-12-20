#!/usr/bin/env python3
"""Phase 6: multiple simultaneously active connections and cleanup."""
from __future__ import annotations

import socket
import socketserver
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from test_proxy import TIMEOUT, recv_until_eof, start_proxy, stop_proxy
from test_epoll import all_nonblocking, epoll_target_count, kernel_descriptors


class EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(TIMEOUT)
        data = recv_until_eof(self.request)
        self.request.sendall(b"ACK:" + data[::-1])


class ThreadedBackend(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def wait_for_descriptor_count(pid: int, count: int) -> tuple[list[int], list[int]]:
    end = time.monotonic() + TIMEOUT
    while time.monotonic() < end:
        fds, epolls = kernel_descriptors(pid)
        if len(fds) == count and len(epolls) == 1:
            return fds, epolls
        time.sleep(0.01)
    raise AssertionError(f"expected {count} sockets, found {kernel_descriptors(pid)}")


def round_trip(proxy_port: int, sequence: int) -> None:
    data = bytes(range(256)) * (sequence % 12 + 1) + sequence.to_bytes(4, "big")
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=TIMEOUT) as client:
        client.settimeout(TIMEOUT)
        client.sendall(data)
        client.shutdown(socket.SHUT_WR)
        assert recv_until_eof(client) == b"ACK:" + data[::-1]


def main() -> None:
    backend = ThreadedBackend(("127.0.0.1", 0), EchoHandler)
    server_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    server_thread.start()
    proc, port = start_proxy(backend.server_address[1])
    # Many per-session log messages would fill an undrained subprocess PIPE.
    # A dedicated reader keeps the test from blocking the server on printf().
    def consume_logs() -> None:
        assert proc.stdout is not None
        for _ in proc.stdout:
            pass

    log_reader = threading.Thread(target=consume_logs, daemon=True)
    log_reader.start()
    try:
        baseline, epolls = wait_for_descriptor_count(proc.pid, 1)
        assert len(baseline) == 1
        assert epoll_target_count(proc.pid, epolls[0]) == 1

        # Hold two connections open at once. The proxy must not stop listening.
        with socket.create_connection(("127.0.0.1", port), TIMEOUT) as first, \
             socket.create_connection(("127.0.0.1", port), TIMEOUT) as second:
            first.settimeout(TIMEOUT)
            second.settimeout(TIMEOUT)
            fds, epolls = wait_for_descriptor_count(proc.pid, 5)
            assert all_nonblocking(proc.pid, fds)
            assert epoll_target_count(proc.pid, epolls[0]) == 5
            first.sendall(b"first\x00connection")
            second.sendall(b"second\xffconnection")
            second.shutdown(socket.SHUT_WR)
            assert recv_until_eof(second) == b"ACK:" + b"second\xffconnection"[::-1]
            assert proc.poll() is None, "proxy terminated after one client"
            first.shutdown(socket.SHUT_WR)
            assert recv_until_eof(first) == b"ACK:" + b"first\x00connection"[::-1]
        wait_for_descriptor_count(proc.pid, 1)
        print("PASS: 2 simultaneous clients, independent buffers, fd cleanup")

        # Keep one idle session open; progress on unrelated sessions is essential.
        with socket.create_connection(("127.0.0.1", port), TIMEOUT) as idle:
            idle.settimeout(TIMEOUT)
            with ThreadPoolExecutor(max_workers=12) as pool:
                list(pool.map(lambda i: round_trip(port, i), range(48)))
            assert proc.poll() is None
            fds, _ = wait_for_descriptor_count(proc.pid, 3)
            assert len(fds) == 3, "only listener plus idle socket pair should remain"
            idle.sendall(b"idle survived")
            idle.shutdown(socket.SHUT_WR)
            assert recv_until_eof(idle) == b"ACK:" + b"idle survived"[::-1]
        wait_for_descriptor_count(proc.pid, 1)
        print("PASS: 48 concurrent requests while another client stays idle")

        # Sequential fd reuse must not resurrect stale epoll events.
        for i in range(24):
            round_trip(port, 100 + i)
        wait_for_descriptor_count(proc.pid, 1)
        print("PASS: 24 sequential reconnects, descriptor reuse, no fd leaks")
        assert proc.poll() is None, "persistent listener exited unexpectedly"
    finally:
        stop_proxy(proc)
        log_reader.join(timeout=TIMEOUT)
        backend.shutdown()
        backend.server_close()
        server_thread.join(timeout=TIMEOUT)
    print("PASS: Phase 6 multi-client connection tracking test suite")


if __name__ == "__main__":
    main()
