#!/usr/bin/env python3
"""Phase 26: monotonic connect and inactivity deadlines, no listener death."""
from pathlib import Path
import os
import re
import socket
import socketserver
import subprocess
import threading
import time
from rr_backends import start_backend

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / 'build' / 'splice-lb'
INJECT = ROOT / 'build' / 'timeout-inject.so'


class SilentHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            while self.request.recv(4096):
                pass
        except OSError:
            pass


class SilentServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def launch(port, *options, preload=False):
    env = os.environ.copy()
    if preload:
        env['LD_PRELOAD'] = str(INJECT)
    proc = subprocess.Popen([str(PROXY), *options, '0', f'127.0.0.1:{port}'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=0, env=env)
    line = proc.stdout.readline().decode()
    match = re.search(r'Listening on 127\.0\.0\.1:(\d+)', line)
    assert match, (line, proc.poll())
    return proc, int(match.group(1))


def stop(proc):
    proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
    assert b'splice accounting invariant failed' not in stderr, stderr
    assert b'active connection accounting underflow' not in stderr, stderr
    return (stdout + stderr).decode(errors='replace')


def exchange(port, data):
    with socket.create_connection(('127.0.0.1', port), timeout=3) as conn:
        conn.settimeout(4)
        conn.sendall(data)
        conn.shutdown(socket.SHUT_WR)
        received = bytearray()
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return bytes(received)
            received.extend(chunk)


def invalid_options():
    good = ['0', '127.0.0.1:9091']
    invalid = [
        ['--connect-timeout-ms=0'], ['--connect-timeout-ms=99'],
        ['--connect-timeout-ms=120001'], ['--connect-timeout-ms=-1'],
        ['--connect-timeout-ms=1x'], ['--connect-timeout-ms='],
        ['--idle-timeout-ms=0'], ['--idle-timeout-ms=99'],
        ['--idle-timeout-ms=3600001'], ['--idle-timeout-ms=+500'],
        ['--idle-timeout-ms=abc'], ['--idle-timeout-ms='],
        ['--idle-timeout-ms=250', '--idle-timeout-ms=300'],
        ['--connect-timeout-ms=200', '--connect-timeout-ms=300'],
    ]
    for prefix in invalid:
        result = subprocess.run([str(PROXY), *prefix, *good],
                                capture_output=True, timeout=3)
        assert result.returncode and b'Usage:' in result.stderr, (prefix, result)
    help_text = subprocess.check_output([str(PROXY), '--help'], text=True)
    assert '--connect-timeout-ms' in help_text and '--idle-timeout-ms' in help_text
    print(f'PASS: both timeout flags documented; {len(invalid)} invalid values rejected',
          flush=True)


def wait_closed(conn, seconds=2):
    conn.settimeout(seconds)
    start = time.monotonic()
    while True:
        if not conn.recv(64):
            return time.monotonic() - start


def idle_timeout_and_progress():
    backend, _ = start_backend(0, 0)
    proc = None
    try:
        proc, port = launch(backend.server_address[1],
                            '--idle-timeout-ms=300', '--connect-timeout-ms=1000')
        with socket.create_connection(('127.0.0.1', port), timeout=2) as conn:
            conn.settimeout(2)
            assert conn.recv(10) == b'backend-0|'
            elapsed = wait_closed(conn)
            assert 0.15 <= elapsed < 1.9, elapsed
        print(f'PASS: idle client closed after {elapsed:.3f}s without byte progress',
              flush=True)
        # Every 0.09s activity should reset the 0.3s deadline.
        with socket.create_connection(('127.0.0.1', port), timeout=2) as conn:
            conn.settimeout(3)
            assert conn.recv(10) == b'backend-0|'
            for b in b'abcdefgh':
                conn.sendall(bytes([b]))
                assert conn.recv(1) == bytes([b])
                time.sleep(0.09)
            assert wait_closed(conn) < 1.8
        payload = b'\x00\xff' + bytes(range(256)) * 40
        assert exchange(port, payload) == b'backend-0|' + payload
        assert proc.poll() is None
        print('PASS: periodic traffic refreshes idle deadline; healthy client recovers',
              flush=True)
    finally:
        output = stop(proc) if proc else ''
        backend.shutdown()
        backend.server_close()
    assert output.count('connection-close-reason=idle-timeout') >= 2, output
    assert 'connection-close-reason=graceful' in output, output
    assert 'total-active=0' in output, output
    print('PASS: timed-out sessions reclaim backend slots, listener stays alive',
          flush=True)


def connect_deadline_injected():
    server = SilentServer(('127.0.0.1', 0), SilentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proc = None
    try:
        proc, port = launch(server.server_address[1], '--connect-timeout-ms=240',
                            '--idle-timeout-ms=1500', '--backend-retry-ms=100',
                            preload=True)
        with socket.create_connection(('127.0.0.1', port), timeout=2) as conn:
            conn.sendall(b'early buffered bytes')
            elapsed = wait_closed(conn)
            assert 0.12 < elapsed < 1.6, elapsed
        # First backend connect was held in CONNECTING artificially. On the
        # next attempt, the shim allows normal EPOLLOUT connect completion.
        time.sleep(0.18)
        with socket.create_connection(('127.0.0.1', port), timeout=2) as conn:
            conn.settimeout(3)
            conn.shutdown(socket.SHUT_WR)
            assert wait_closed(conn) < 2.8
        assert proc.poll() is None
        print(f'PASS: pending handshake expires after {elapsed:.3f}s without EPOLLOUT',
              flush=True)
    finally:
        output = stop(proc) if proc else ''
        server.shutdown()
        server.server_close()
    assert 'backend-connect-timeout backend-index=0' in output, output
    assert 'connection-close-reason=backend-connect-timeout' in output, output
    assert 'Backend[0]=' in output and 'recovered after successful TCP handshake' in output, output
    assert 'total-active=0' in output, output
    print('PASS: backend timeout quarantines target; later recovery probe succeeds',
          flush=True)


if __name__ == '__main__':
    invalid_options()
    idle_timeout_and_progress()
    connect_deadline_injected()
    print('PASS: Phase 26 backend connection and idle timeouts test suite', flush=True)
