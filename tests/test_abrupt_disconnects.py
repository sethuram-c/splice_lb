#!/usr/bin/env python3
"""Phase 23: intentional TCP RST, backend RST, and post-failure recovery.

Uses loopback, SO_LINGER(1,0) for real RST, /proc for descriptor cleanup,
and a persistent backend with multiple scripted sessions. No external deps.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import struct
import threading
import time

from test_proxy import (TIMEOUT, recv_until_eof, start_proxy, stop_proxy)


def abort_with_rst(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
    sock.close()


def await_fd_baseline(proc, baseline: tuple[int, int, int], seconds: float = 18) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError('proxy exited unexpectedly during cleanup')
        if fd_snapshot(proc.pid) == baseline:
            return
        time.sleep(.025)
    raise AssertionError(('proxy descriptors not released', baseline, fd_snapshot(proc.pid)))


def fd_snapshot(pid: int) -> tuple[int, int, int]:
    # /proc is live: a fd can vanish between listing and readlink(). A
    # transient snapshot is not evidence of a leaked or cleaned descriptor.
    try:
        entries = list((Path('/proc') / str(pid) / 'fd').iterdir())
        targets = [os.readlink(item) for item in entries]
    except FileNotFoundError:
        return (-1, -1, -1)  # caller retries at the next idle checkpoint
    return (len(targets), sum(x.startswith('socket:[') for x in targets),
            sum(x.startswith('pipe:[') for x in targets))


class ScriptedBackend:
    def __init__(self, first_action: str) -> None:
        self.first_action = first_action
        self.ready = threading.Event()
        self.accepted = threading.Event()
        self.errors: list[Exception] = []
        self.served = 0
        self.stop_event = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(64)
        self.listener.settimeout(.2)
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()
        assert self.ready.wait(TIMEOUT)

    def run(self) -> None:
        self.ready.set()
        while not self.stop_event.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            index = self.served
            self.served += 1
            with conn:
                conn.settimeout(10)
                if index == 0 and self.first_action == 'client-rst':
                    self.accepted.set()
                    try:
                        recv_until_eof(conn)
                    except (ConnectionResetError, OSError):
                        pass
                    continue
                if index == 0 and self.first_action == 'backend-rst':
                    self.accepted.set()
                    abort_with_rst(conn)
                    continue
                try:
                    request = recv_until_eof(conn)
                    conn.sendall(b'ACK:' + request[::-1])
                except (BrokenPipeError, ConnectionResetError):
                    pass  # intentionally reset sessions during burst test
                except Exception as exc:
                    self.errors.append(exc)
                self.accepted.set()

    def close(self) -> None:
        self.stop_event.set()
        self.listener.close()
        self.worker.join(timeout=TIMEOUT)
        assert not self.worker.is_alive(), 'backend thread stuck'
        assert not self.errors, self.errors


def good_client(port: int, payload: bytes) -> None:
    with socket.create_connection(('127.0.0.1', port), timeout=TIMEOUT) as conn:
        conn.settimeout(TIMEOUT)
        conn.sendall(payload)
        conn.shutdown(socket.SHUT_WR)
        assert recv_until_eof(conn) == b'ACK:' + payload[::-1], 'corrupted recovery response'


def run_failure_scenario(mode: str) -> None:
    backend = ScriptedBackend(mode)
    proc, proxy_port = start_proxy(backend.port)
    try:
        baseline = fd_snapshot(proc.pid)
        assert baseline[1] == 1, baseline
        sock = socket.create_connection(('127.0.0.1', proxy_port), TIMEOUT)
        sock.settimeout(3)
        assert backend.accepted.wait(TIMEOUT), 'backend did not accept fault session'
        if mode == 'client-rst':
            abort_with_rst(sock)
        else:
            try:
                sock.recv(64)  # either EOF or ECONNRESET is an expected abrupt close
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            sock.close()
        await_fd_baseline(proc, baseline)
        assert fd_snapshot(proc.pid) == baseline, ('leaked descriptor after reset', mode,
                                                    baseline, fd_snapshot(proc.pid))
        # Reuse the same persistent listener and backend; must not be poisoned.
        good_client(proxy_port, bytes(range(256)) * 32)
        await_fd_baseline(proc, baseline)
        assert fd_snapshot(proc.pid) == baseline, ('leaked descriptor after recovery', mode)
        assert proc.poll() is None, 'proxy exited on an individual TCP reset'
        proc.terminate()
        out, err = proc.communicate(timeout=TIMEOUT)
        assert proc.returncode == -15, (out, err)
        expected_reason = b'client-abort' if mode == 'client-rst' else b'backend-abort'
        assert b'connection-close-reason=' + expected_reason in out, (mode, out, err)
        assert b'connection-close-reason=graceful' in out, (mode, out, err)
        assert b'connection-completion=graceful' in out, (mode, out, err)
        assert out.count(b'total-active=0') >= 2, (mode, out, err)
        assert b'connection-close-reason=server-shutdown' not in out, (mode, out, err)
        print(f'PASS: {mode} closes one session, releases all fds, listener serves next client')
    finally:
        stop_proxy(proc)
        backend.close()


def burst_rst_then_healthy() -> None:
    backend = ScriptedBackend('client-rst')
    proc, proxy_port = start_proxy(backend.port)
    try:
        baseline = fd_snapshot(proc.pid)
        with socket.create_connection(('127.0.0.1', proxy_port), TIMEOUT) as first:
            assert backend.accepted.wait(TIMEOUT)
            abort_with_rst(first)
        # Issue multiple new connections that all close abruptly; let a normal
        # client complete afterwards to exercise event-batch stale tokens.
        for _ in range(8):
            sock = socket.create_connection(('127.0.0.1', proxy_port), TIMEOUT)
            abort_with_rst(sock)
        await_fd_baseline(proc, baseline)
        good_client(proxy_port, b'last request' * 300)
        await_fd_baseline(proc, baseline)
        assert fd_snapshot(proc.pid) == baseline, ('fd leak after reset burst', fd_snapshot(proc.pid))
        proc.terminate()
        out, err = proc.communicate(timeout=TIMEOUT)
        assert proc.returncode == -15, (out, err)
        assert b'connection-close-reason=graceful' in out, (out, err)
        assert b'connection-completion=graceful' in out, (out, err)
        assert out.count(b'active=0') >= 2, (out, err)
        print('PASS: repeated abrupt disconnects, stale events ignored, recovery byte-exact')
    finally:
        stop_proxy(proc)
        backend.close()


if __name__ == '__main__':
    run_failure_scenario('client-rst')
    run_failure_scenario('backend-rst')
    burst_rst_then_healthy()
    print('PASS: Phase 23 abrupt disconnects and centralized cleanup test suite')
