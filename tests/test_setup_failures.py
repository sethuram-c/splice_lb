#!/usr/bin/env python3
"""Phase 24: deterministic partial-setup failures and connection recovery.

Five isolated LD_PRELOAD fault points exercise rollback of 0, 1, or 2 pipe
pairs and 0, 1, or 2 epoll endpoint registrations. No external dependencies.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import time

from test_proxy import ROOT, TIMEOUT, start_proxy, stop_proxy, recv_until_eof
from test_abrupt_disconnects import fd_snapshot, await_fd_baseline

FAULT_LIBRARY = ROOT / 'build' / 'setup-faults.so'
STAGES = ('backend-socket', 'pipe-first', 'pipe-second', 'epoll-client', 'epoll-backend')


class EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(TIMEOUT)
        try:
            data = recv_until_eof(self.request)
            if data:
                self.request.sendall(b'ACK:' + data[::-1])
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass  # injected setup rollback deliberately severs this session


class EchoServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_stage(stage: str) -> None:
    with tempfile.TemporaryDirectory(prefix='splice-phase24-') as temporary:
        marker = Path(temporary) / 'injected'
        backend = EchoServer(('127.0.0.1', 0), EchoHandler)
        backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
        backend_thread.start()
        env = os.environ.copy()
        env['LD_PRELOAD'] = str(FAULT_LIBRARY)
        env['PHASE24_FAIL_STAGE'] = stage
        env['PHASE24_FAULT_MARKER'] = str(marker)
        proc = None
        try:
            proc, proxy_port = start_proxy(backend.server_address[1], env=env)
            baseline = fd_snapshot(proc.pid)
            assert baseline[1] == 1, ('unexpected idle sockets', baseline)
            # stdout/stderr capture pipes are present in the fd baseline.
            # First connection is intentionally sabotaged at the specified
            # stage. It may fail before the client sends or receives anything.
            with socket.create_connection(('127.0.0.1', proxy_port), TIMEOUT) as first:
                first.settimeout(1)
                try:
                    first.sendall(b'initial setup')
                    first.shutdown(socket.SHUT_WR)
                    first.recv(32)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
            deadline = time.monotonic() + TIMEOUT
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists(), f'{stage}: interposer never injected'
            assert marker.read_text() == stage, (stage, marker.read_text())
            await_fd_baseline(proc, baseline, seconds=TIMEOUT)
            assert proc.poll() is None, f'{stage}: listener died on setup error'
            # Reuse SAME proxy and backend. After one-shot failure, all syscall
            # wrappers pass through. Must forward byte-exact data and cleanup.
            payload = bytes(range(256)) * 48
            with socket.create_connection(('127.0.0.1', proxy_port), TIMEOUT) as second:
                second.settimeout(TIMEOUT)
                second.sendall(payload)
                second.shutdown(socket.SHUT_WR)
                reply = recv_until_eof(second)
            assert reply == b'ACK:' + payload[::-1], (stage, len(reply))
            await_fd_baseline(proc, baseline, seconds=TIMEOUT)
            assert fd_snapshot(proc.pid) == baseline, (stage, baseline, fd_snapshot(proc.pid))
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=TIMEOUT)
            assert proc.returncode == -15, (stage, proc.returncode, stderr)
            assert b'phase24-injected-failure=' + stage.encode() in stderr, stderr
            assert b'connection-setup-failed=' in stderr, (stage, stderr)
            assert b'connection-completion=graceful' in stdout, (stage, stdout)
            assert b'total-active=0' in stdout, (stage, stdout)
            print(f'PASS: {stage} rollback releases fds and recovers on next client')
        finally:
            if proc is not None:
                stop_proxy(proc)
            backend.shutdown()
            backend.server_close()
            backend_thread.join(timeout=TIMEOUT)
            assert not backend_thread.is_alive(), 'backend thread failed to terminate'


if __name__ == '__main__':
    assert FAULT_LIBRARY.exists(), 'run make before focused test'
    for stage in STAGES:
        run_stage(stage)
    print('PASS: Phase 24 safe connection lifetime and partial setup test suite')
