#!/usr/bin/env python3
"""Phase 20: two-way TCP backpressure with deterministic stalled readers.

This is an integration test: both peers write a multi-MiB binary stream while
neither side reads. Once both destination sockets need EPOLLOUT, unblock both
readers and prove payload integrity, queue bound, and pause/resume accounting.
No root, external packages, or kernel tuning are required.
"""
from __future__ import annotations

import re
import select
import socket
import threading
import time

from test_bidirectional_buffers import watch_masks
from test_proxy import recv_until_eof, start_proxy, stop_proxy, wait_for_proxy_cleanup

SIZE = 6 * 1024 * 1024
CLIENT_TO_BACKEND = bytes(range(256)) * (SIZE // 256)
BACKEND_TO_CLIENT = bytes(reversed(range(256))) * (SIZE // 256)


def check_full_duplex_backpressure() -> None:
    release = threading.Event()
    backend_ready = threading.Event()
    errors: list[BaseException] = []
    captured: list[bytes] = []

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        listener.settimeout(20)

        def backend_worker() -> None:
            try:
                with listener.accept()[0] as sock:
                    sock.settimeout(60)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
                    backend_ready.set()
                    def write_reply() -> None:
                        try:
                            sock.sendall(BACKEND_TO_CLIENT)
                            sock.shutdown(socket.SHUT_WR)
                        except BaseException as exc:
                            errors.append(exc)
                    writer = threading.Thread(target=write_reply, daemon=True)
                    writer.start()
                    assert release.wait(25), 'readers were not released'
                    captured.append(recv_until_eof(sock))
                    writer.join(45)
                    assert not writer.is_alive(), 'backend writer hung'
            except BaseException as exc:
                errors.append(exc)

        backend_thread = threading.Thread(target=backend_worker, daemon=True)
        backend_thread.start()
        proc, port = start_proxy(listener.getsockname()[1])
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=10) as client:
                client.settimeout(60)
                client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
                assert backend_ready.wait(10), 'backend did not accept'

                def send_request() -> None:
                    try:
                        client.sendall(CLIENT_TO_BACKEND)
                        client.shutdown(socket.SHUT_WR)
                    except BaseException as exc:
                        errors.append(exc)

                sender = threading.Thread(target=send_request, daemon=True)
                sender.start()
                # Both endpoints should have pending outbound splice bytes:
                # EPOLLOUT is armed only while a nonblocking write is blocked.
                deadline = time.monotonic() + 15
                masks: list[int] = []
                while time.monotonic() < deadline:
                    masks = watch_masks(proc.pid)
                    if sum(bool(mask & select.EPOLLOUT) for mask in masks) >= 2:
                        break
                    time.sleep(.02)
                else:
                    raise AssertionError(f'expected two simultaneous EPOLLOUT watches: {masks}')

                release.set()
                answer = recv_until_eof(client)
                sender.join(45)
                assert not sender.is_alive(), 'client writer hung'
                assert answer == BACKEND_TO_CLIENT, (len(answer), len(BACKEND_TO_CLIENT))
            backend_thread.join(45)
            assert not backend_thread.is_alive(), 'backend thread hung'
            if errors:
                raise errors[0]
            assert captured == [CLIENT_TO_BACKEND], [len(x) for x in captured]
            wait_for_proxy_cleanup(proc)
            proc.terminate()
            out, err = proc.communicate(timeout=10)
            assert proc.returncode == -15 and not err, (out, err)
            assert b'transfer-integrity=verified' in out, out
            metrics = re.search(
                rb'backpressure-pauses=(\d+)/(\d+); '
                rb'backpressure-resumes=(\d+)/(\d+); '
                rb'backpressure-peak=(\d+)/(\d+) bytes; queue-cap=(\d+) bytes', out
            )
            assert metrics, out
            pauses_up, pauses_down, resumes_up, resumes_down, peak_up, peak_down, cap = map(int, metrics.groups())
            assert pauses_up and pauses_down, metrics.groups()
            assert resumes_up and resumes_down, metrics.groups()
            assert 0 < peak_up <= cap == 65536, metrics.groups()
            assert 0 < peak_down <= cap, metrics.groups()
            assert f'client-to-backend={SIZE} bytes, backend-to-client={SIZE} bytes'.encode() in out, out
        finally:
            release.set()
            stop_proxy(proc)
            backend_thread.join(3)
    print('PASS: simultaneous two-way backpressure (6 MiB per direction), both EPOLLOUT watches armed')
    print('PASS: independent pause/resume counters, bounded queues <= 65536 bytes, and byte-exact delivery')
    print('PASS: Phase 20 bidirectional backpressure test suite')


if __name__ == '__main__':
    check_full_duplex_backpressure()
