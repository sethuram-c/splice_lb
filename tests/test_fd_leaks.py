#!/usr/bin/env python3
"""Phase 33: bounded /proc file-descriptor lifetime tests for the real proxy.

Note: /proc snapshots detect leaked descriptor *counts* at idle checkpoints,
not all possible kernel allocations or very short-lived leaks.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import socket
import struct
import time

from harness import (HOST, assert_bytes_equal, deterministic_bytes,
                     proxy_session, request, streaming_echo_backend)


def snapshot(pid: int) -> tuple[int, int, int]:
    path = Path(f'/proc/{pid}/fd')
    try:
        fds = list(path.iterdir())
        targets = [os.readlink(fd) for fd in fds]
    except (FileNotFoundError, ProcessLookupError):
        raise AssertionError(f'proxy exited while inspecting fd table (pid {pid})')
    return (len(targets),
            sum(name.startswith('socket:[') for name in targets),
            sum(name.startswith('pipe:[') for name in targets))


def wait_baseline(pid: int, expected: tuple[int, int, int], *, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if snapshot(pid) == expected:
            return
        time.sleep(.025)
    raise AssertionError(f'descriptor leak: baseline={expected}, current={snapshot(pid)}')


def reset_peer(port: int, index: int) -> None:
    with socket.create_connection((HOST, port), timeout=6) as client:
        client.settimeout(6)
        client.sendall(deterministic_bytes(4096, seed=index))
        # Force a TCP reset instead of a graceful FIN.
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))


def exercise() -> None:
    with streaming_echo_backend() as backend_port:
        with proxy_session(backend_port, '--max-connections=128',
                           '--idle-timeout-ms=30000') as proxy:
            baseline = snapshot(proxy.proc.pid)
            # Repeated real connections (not just opening and closing locally).
            for cycle in range(3):
                with ThreadPoolExecutor(max_workers=12) as workers:
                    work = []
                    for i in range(24):
                        payload = deterministic_bytes(8193 + (i * 13), seed=i + cycle)
                        work.append((payload, workers.submit(request, proxy.port, payload)))
                    for expected, future in work:
                        assert_bytes_equal(expected, future.result(timeout=35), label='fd-cycle')
                wait_baseline(proxy.proc.pid, baseline)
                print(f'PASS: {24 * (cycle + 1)} completed TCP sessions; fds back to {baseline}', flush=True)

            for i in range(36):
                reset_peer(proxy.port, i)
            wait_baseline(proxy.proc.pid, baseline)
            print('PASS: 36 abrupt TCP resets released socket and pipe descriptors', flush=True)
            assert proxy.proc.poll() is None
            assert request(proxy.port, b'healthy after resets') == b'healthy after resets'
            wait_baseline(proxy.proc.pid, baseline)
            print('PASS: final healthy session and baseline fd count; listener still active', flush=True)
            logs = proxy.logs()
            assert 'splice accounting invariant failed' not in logs
            assert 'active connection accounting underflow' not in logs
            assert 'event=server_ready' in logs
    print('PASS: Phase 33 /proc file descriptor lifetime suite', flush=True)


if __name__ == '__main__':
    exercise()
