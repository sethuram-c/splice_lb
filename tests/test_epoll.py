#!/usr/bin/env python3
"""Phase 5 epoll/O_NONBLOCK checks adapted for Phase 6 persistent relay."""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from pathlib import Path

from test_proxy import TIMEOUT, check_request_response, recv_until_eof, start_proxy, stop_proxy, wait_for_proxy_cleanup


def kernel_descriptors(pid: int) -> tuple[list[int], list[int]]:
    """Report the proxy's TCP socket fds and eventpoll fds via procfs."""
    socket_fds: list[int] = []
    epoll_fds: list[int] = []
    for entry in (Path("/proc") / str(pid) / "fd").iterdir():
        try:
            target = os.readlink(entry)
        except FileNotFoundError:
            continue  # A file descriptor can close between listing and lookup.
        if target.startswith("socket:["):
            socket_fds.append(int(entry.name))
        if "eventpoll" in target:
            epoll_fds.append(int(entry.name))
    return socket_fds, epoll_fds


def all_nonblocking(pid: int, socket_fds: list[int]) -> bool:
    for fd in socket_fds:
        info = (Path("/proc") / str(pid) / "fdinfo" / str(fd)).read_text()
        match = re.search(r"^flags:\s*([0-7]+)$", info, flags=re.MULTILINE)
        assert match is not None, info
        if not (int(match.group(1), 8) & os.O_NONBLOCK):
            return False
    return True


def epoll_target_count(pid: int, epoll_fd: int) -> int:
    info = (Path("/proc") / str(pid) / "fdinfo" / str(epoll_fd)).read_text()
    return len(re.findall(r"^tfd:\s*\d+", info, re.MULTILINE))


def check_epoll_registration_and_socket_flags() -> None:
    payload = b"phase5\0raw\xff"
    release_backend = threading.Event()
    backend_ready = threading.Event()
    errors: list[BaseException] = []

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(TIMEOUT)

        def backend_worker() -> None:
            try:
                with listener.accept()[0] as connection:
                    connection.settimeout(TIMEOUT)
                    backend_ready.set()
                    assert release_backend.wait(TIMEOUT), "backend release timed out"
                    assert recv_until_eof(connection) == payload
                    connection.sendall(b"ACK:" + payload)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=backend_worker, daemon=True)
        worker.start()
        proc, port = start_proxy(listener.getsockname()[1])
        try:
            with socket.create_connection(("127.0.0.1", port), TIMEOUT) as client:
                client.settimeout(TIMEOUT)
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
                assert backend_ready.wait(TIMEOUT), "backend was never connected"
                deadline = time.monotonic() + TIMEOUT
                while True:
                    sockets, epolls = kernel_descriptors(proc.pid)
                    if len(sockets) >= 3 and len(epolls) == 1 and \
                            epoll_target_count(proc.pid, epolls[0]) >= 3:
                        break
                    if time.monotonic() > deadline:
                        raise AssertionError(("epoll registration timeout", sockets, epolls))
                    time.sleep(0.01)
                assert all_nonblocking(proc.pid, sockets), "not all proxy sockets are nonblocking"
                assert epoll_target_count(proc.pid, epolls[0]) == 4, "expected listener+signalfd+client+backend registered"
                release_backend.set()
                assert recv_until_eof(client) == b"ACK:" + payload
            wait_for_proxy_cleanup(proc)
            assert proc.poll() is None, "proxy must remain active after a session"
            proc.terminate()
            output, err = proc.communicate(timeout=TIMEOUT)
            assert proc.returncode == -15, err.decode(errors="replace")
            assert b"Connected to backend" in output
            assert f"client-to-backend={len(payload)} bytes".encode() in output
            assert f"backend-to-client={len(payload) + 4} bytes".encode() in output
        finally:
            release_backend.set()
            stop_proxy(proc)
            worker.join(timeout=TIMEOUT)
        assert not worker.is_alive(), "backend worker still running"
        if errors:
            raise errors[0]
    print("PASS: epoll watches listener+signalfd+client+backend; all sockets have O_NONBLOCK")


def main() -> None:
    check_epoll_registration_and_socket_flags()
    check_request_response(bytes(range(256)) * 4096)  # 1 MiB, binary and multi-chunk
    print("PASS: Phase 5 nonblocking sockets and epoll setup test suite")


if __name__ == "__main__":
    main()
