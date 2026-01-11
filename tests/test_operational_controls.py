#!/usr/bin/env python3
"""Phase 27: connection cap, operational logs, and signal-driven drain."""
from __future__ import annotations

import re
import signal
import socket
import socketserver
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / 'build' / 'splice-lb'


class EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        parts = []
        try:
            while True:
                block = self.request.recv(65536)
                if not block:
                    break
                parts.append(block)
            if self.server.reply_delay:
                time.sleep(self.server.reply_delay)
            self.request.sendall(b'ACK:' + b''.join(parts))
        except OSError:
            pass


class Backend(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def backend_start(delay=0):
    instance = Backend(('127.0.0.1', 0), EchoHandler)
    instance.reply_delay = delay
    worker = threading.Thread(target=instance.serve_forever, daemon=True)
    worker.start()
    return instance, worker


def backend_stop(backend, thread):
    backend.shutdown()
    backend.server_close()
    thread.join(timeout=3)


def launch(port, *options):
    proc = subprocess.Popen([str(PROXY), *options, '0', f'127.0.0.1:{port}'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    start = proc.stdout.readline().decode()
    match = re.search(r'Listening on 127\.0\.0\.1:(\d+)', start)
    assert match, (start, proc.poll())
    return proc, int(match.group(1)), start


def join(proc, timeout=5):
    stdout, stderr = proc.communicate(timeout=timeout)
    assert proc.returncode in (-signal.SIGTERM, -signal.SIGINT), (
        proc.returncode, stdout[-3000:], stderr[-3000:])
    assert b'active connection accounting underflow' not in stderr, stderr
    assert b'splice accounting invariant failed' not in stderr, stderr
    return (stdout + stderr).decode(errors='replace')


def connect(port):
    peer = socket.create_connection(('127.0.0.1', port), timeout=2)
    peer.settimeout(3)
    return peer


def recv_all(peer):
    data = bytearray()
    while True:
        block = peer.recv(65536)
        if not block:
            return bytes(data)
        data.extend(block)


def invalid_options():
    bad = [
        ['--max-connections=0'], ['--max-connections=100001'],
        ['--max-connections=-2'], ['--max-connections=abc'],
        ['--max-connections='], ['--max-connections', '3', '--max-connections=4'],
        ['--shutdown-grace-ms=99'], ['--shutdown-grace-ms=120001'],
        ['--shutdown-grace-ms=abc'], ['--shutdown-grace-ms='],
        ['--shutdown-grace-ms=500', '--shutdown-grace-ms=600'],
    ]
    for options in bad:
        result = subprocess.run([str(PROXY), *options, '0', '127.0.0.1:9091'],
                                capture_output=True, timeout=3)
        assert result.returncode != 0 and b'Usage:' in result.stderr, (options, result)
    help_text = subprocess.check_output([str(PROXY), '--help'], text=True)
    assert '--max-connections' in help_text
    assert '--shutdown-grace-ms' in help_text
    print(f'PASS: help documents operational limits; {len(bad)} invalid configurations rejected', flush=True)


def connection_limit():
    backend, worker = backend_start()
    proc = None
    peers = []
    try:
        proc, port, _ = launch(backend.server_address[1], '--max-connections=2')
        peers = [connect(port), connect(port)]
        time.sleep(0.12)
        third = connect(port)
        try:
            try:
                outcome = third.recv(16)
                assert outcome == b'', outcome
            except ConnectionResetError:
                pass
        finally:
            third.close()
        for index, peer in enumerate(peers):
            data = f'phase27-{index}'.encode()
            peer.sendall(data)
            peer.shutdown(socket.SHUT_WR)
            assert recv_all(peer) == b'ACK:' + data
            peer.close()
        peers.clear()
        data = b'capacity released'
        with connect(port) as recovered:
            recovered.sendall(data)
            recovered.shutdown(socket.SHUT_WR)
            assert recv_all(recovered) == b'ACK:' + data
        proc.send_signal(signal.SIGTERM)
        output = join(proc)
        assert 'event=connection_rejected' in output, output
        assert 'reason=connection_limit limit=2' in output, output
        assert re.search(r'event=server_stopped.*rejected=1.*connections=0', output), output
        assert output.count('transfer-integrity=verified') >= 3, output
        print('PASS: cap=2 rejects third client, active sessions drain, freed slot reused', flush=True)
    finally:
        for peer in peers:
            peer.close()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        backend_stop(backend, worker)


def signal_drains_inflight():
    backend, worker = backend_start(delay=0.22)
    proc = None
    try:
        proc, port, _ = launch(backend.server_address[1], '--shutdown-grace-ms=1800')
        payload = bytes(range(256)) * 200
        with connect(port) as peer:
            peer.sendall(payload)
            peer.shutdown(socket.SHUT_WR)
            time.sleep(0.12)
            proc.send_signal(signal.SIGTERM)
            assert recv_all(peer) == b'ACK:' + payload
        output = join(proc)
        assert 'event=shutdown_started signal=15' in output, output
        assert 'event=shutdown_drained connections=0' in output, output
        assert 'event=shutdown_forced' not in output, output
        assert 'connection-close-reason=graceful' in output, output
        assert 'event=server_stopped' in output, output
        try:
            with connect(port):
                raise AssertionError('listener accepted after shutdown')
        except OSError:
            pass
        print('PASS: SIGTERM stops acceptance, drains a delayed 50 KiB reply, closes cleanly', flush=True)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        backend_stop(backend, worker)


def forced_shutdown():
    backend, worker = backend_start()
    proc = None
    try:
        proc, port, _ = launch(backend.server_address[1], '--shutdown-grace-ms=350')
        peer = connect(port)
        time.sleep(0.08)
        start = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        output = join(proc)
        elapsed = time.monotonic() - start
        peer.close()
        assert 0.2 <= elapsed < 2.5, elapsed
        assert 'event=shutdown_forced connections=1' in output, output
        assert 'connection-close-reason=server-shutdown' in output, output
        assert 'event=server_stopped' in output and 'connections=0' in output, output
        print(f'PASS: held-open session force-closed after grace ({elapsed:.3f}s), no fd leaks', flush=True)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        backend_stop(backend, worker)


def ctrl_c_idle():
    backend, worker = backend_start()
    proc = None
    try:
        proc, _, _ = launch(backend.server_address[1])
        proc.send_signal(signal.SIGINT)
        output = join(proc)
        assert 'event=shutdown_started signal=2 connections=0' in output, output
        assert 'event=shutdown_drained connections=0' in output, output
        print('PASS: SIGINT wakes idle epoll and exits after orderly resource cleanup', flush=True)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        backend_stop(backend, worker)


if __name__ == '__main__':
    invalid_options()
    connection_limit()
    signal_drains_inflight()
    forced_shutdown()
    ctrl_c_idle()
    print('PASS: Phase 27 connection limits, logging, and graceful shutdown test suite', flush=True)
