#!/usr/bin/env python3
"""Phase 32: mixed slow TCP peers, independent FINs, resets and backend recovery.

Local-only integration tests of the real production proxy. No third-party
packages, no privileged networking, and no files saved inside the repository.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from pathlib import Path
import socket
import socketserver
import struct
import threading
import time

from harness import (HOST, assert_bytes_equal, deterministic_bytes,
                     proxy_session, request)
from rr_backends import start_backend
from test_least_connections import Proxy, wait_until
from test_round_robin import one_client


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = True
    request_queue_size = 128


@contextmanager
def backend(handler):
    service = Server((HOST, 0), handler)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        yield service
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError('test backend service did not terminate')


class ThrottledEcho(socketserver.BaseRequestHandler):
    """Small reads and deliberate pauses stress simultaneous read/write pressure."""
    def handle(self):
        sock = self.request
        sock.settimeout(25)
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                time.sleep(.00045)  # test fixture slow receiver, not throughput timing
                sock.sendall(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            # RST is intentional for some peers. A backend must survive it.
            pass


def fd_count(pid):
    try:
        return len(list((Path('/proc') / str(pid) / 'fd').iterdir()))
    except FileNotFoundError:
        return -1


def wait_fd_recovery(proc, baseline, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise AssertionError('load balancer died while cleaning up clients')
        if fd_count(proc.pid) == baseline:
            return
        time.sleep(.04)
    raise AssertionError(f'fd leak or stuck peer: baseline={baseline}; now={fd_count(proc.pid)}')


def reset_client(port, marker):
    client = socket.create_connection((HOST, port), timeout=8)
    try:
        client.sendall(marker * 64)
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        # Linux sends a TCP RST instead of a graceful FIN on close.
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        client.close()


def slow_client(port, serial):
    size = 1_048_576 + 41 * serial
    expected = deterministic_bytes(size, seed=serial + 45)
    failures = []
    with socket.create_connection((HOST, port), timeout=12) as sock:
        sock.settimeout(30)

        def writer():
            try:
                view = memoryview(expected)
                for offset in range(0, len(view), 7003):
                    sock.sendall(view[offset:offset + 7003])
                sock.shutdown(socket.SHUT_WR)
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        # Let the backend and TCP sender build pressure before reading.
        time.sleep(.13)
        received = bytearray()
        while True:
            chunk = sock.recv(12_289)
            if not chunk:
                break
            received.extend(chunk)
            if serial % 2 == 0:
                time.sleep(.0005)
        thread.join(timeout=30)
        assert not thread.is_alive(), 'client write thread stalled after peer FIN'
        if failures:
            raise AssertionError(f'client writer failed: {failures[0]}')
        assert_bytes_equal(expected, bytes(received), label=f'slow peer {serial}')
        return size


def mixed_slow_peers_and_resets():
    with backend(ThrottledEcho) as service, proxy_session(
            service.server_address[1], '--idle-timeout-ms=30000',
            '--max-connections=64') as proxy:
        baseline = fd_count(proxy.proc.pid)
        with ThreadPoolExecutor(max_workers=20) as pool:
            good = [pool.submit(slow_client, proxy.port, i) for i in range(6)]
            resets = [pool.submit(reset_client, proxy.port, bytes([i + 1, 0, 255]))
                      for i in range(12)]
            sizes = [future.result(timeout=50) for future in good]
            for future in resets:
                future.result(timeout=15)
        # A new healthy client must work after reset storms and slow peers.
        payload = deterministic_bytes(16517, seed=121)
        assert_bytes_equal(payload, request(proxy.port, payload), label='post-RST recovery')
        wait_fd_recovery(proxy.proc, baseline)
        logs = proxy.logs()
        assert 'connection-close-reason=graceful' in logs, 'normal completion missing'
        assert ('client-abort' in logs or 'backend-abort' in logs), 'RST not classified'
        assert 'accounting underflow' not in logs
        print(f'PASS: 6 concurrent throttled duplex sessions, {sum(sizes)} bytes each way; '
              '12 client RSTs, healthy recovery and no fd growth', flush=True)


class FinFirstBackend(socketserver.BaseRequestHandler):
    completed = []
    lock = threading.Lock()

    def handle(self):
        conn = self.request
        conn.settimeout(25)
        banner = b'phase32-backend-fin\x00\xff'
        try:
            conn.sendall(banner)
            conn.shutdown(socket.SHUT_WR)
            digest = hashlib.sha256()
            total = 0
            while block := conn.recv(8192):
                digest.update(block)
                total += len(block)
            with self.lock:
                self.completed.append((total, digest.hexdigest()))
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass


def late_upload(port, size):
    payload = deterministic_bytes(size, seed=size % 239)
    expected_banner = b'phase32-backend-fin\x00\xff'
    with socket.create_connection((HOST, port), timeout=10) as sock:
        sock.settimeout(30)
        # Require a complete backend FIN BEFORE sending any client data.
        response = bytearray()
        while chunk := sock.recv(8192):
            response.extend(chunk)
        assert_bytes_equal(expected_banner, bytes(response), label='backend FIN first')
        view = memoryview(payload)
        for offset in range(0, size, 9_119):
            sock.sendall(view[offset:offset + 9_119])
        sock.shutdown(socket.SHUT_WR)
    return (size, hashlib.sha256(payload).hexdigest())


def backend_fin_first_while_client_uploads():
    FinFirstBackend.completed = []
    with backend(FinFirstBackend) as service, proxy_session(
            service.server_address[1], '--idle-timeout-ms=30000') as proxy:
        baseline = fd_count(proxy.proc.pid)
        sizes = [0, 8192, 262_144, 1_048_609]
        with ThreadPoolExecutor(max_workers=len(sizes)) as pool:
            expected = list(pool.map(lambda size: late_upload(proxy.port, size), sizes))
        end = time.monotonic() + 10
        while time.monotonic() < end:
            with FinFirstBackend.lock:
                got = list(FinFirstBackend.completed)
            if len(got) == len(sizes):
                break
            time.sleep(.03)
        assert Counter(got) == Counter(expected), (got, expected)
        wait_fd_recovery(proxy.proc, baseline)
        logs = proxy.logs()
        assert logs.count('connection-completion=graceful') >= 4
        assert 'accounting underflow' not in logs
        print('PASS: 4 backend-FIN-first sessions accept late client uploads '
              '(0 bytes through 1 MiB+), exact backend SHA-256', flush=True)


def unused_port():
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def refused_connection(port):
    with socket.create_connection((HOST, port), timeout=5) as s:
        s.settimeout(5)
        try:
            got = s.recv(1)
            assert got == b'', f'backend failure yielded unexpected bytes {got!r}'
        except ConnectionResetError:
            pass


def failed_backend_then_recovery():
    failed_port = unused_port()
    healthy, _ = start_backend(0, 1)
    recovered = None
    proxy = None
    try:
        addresses = [f'{HOST}:{failed_port}',
                     f'{HOST}:{healthy.server_address[1]}']
        proxy = Proxy(addresses, algorithm='round-robin')
        refused_connection(proxy.port)
        wait_until(lambda: proxy.releases() >= 1)
        with ThreadPoolExecutor(max_workers=5) as pool:
            payloads = [deterministic_bytes(4096 + 17 * i, seed=i) for i in range(5)]
            results = list(pool.map(lambda x: one_client(proxy.port, x), payloads))
        for payload, response in zip(payloads, results):
            assert_bytes_equal(b'backend-1|' + payload, response, label='healthy fallback')
        wait_until(lambda: proxy.releases() >= 6)
        # Backend 0 is restored at the same address. Its recovery probe only
        # becomes eligible after the configured Phase 19 cooldown (3s).
        recovered, _ = start_backend(failed_port, 0)
        time.sleep(3.2)
        found_recovery = False
        for index in range(8):
            data = b'recovery\x00\xff' + bytes([index])
            result = one_client(proxy.port, data)
            assert result in (b'backend-0|' + data, b'backend-1|' + data)
            if result.startswith(b'backend-0|'):
                found_recovery = True
        assert found_recovery, 'restored backend did not receive a recovery probe'
        wait_until(lambda: proxy.releases() >= 14)
        logs = proxy.snapshot()
        assert any('recovered after successful TCP handshake' in x for x in logs)
        assert any('backend-active=0; total-active=0' in x for x in logs)
        print('PASS: failed backend quarantined during 5 concurrent healthy requests, '
              'then recovered and received new traffic', flush=True)
    finally:
        if proxy:
            proxy.close()
        if recovered:
            recovered.shutdown()
            recovered.server_close()
        healthy.shutdown()
        healthy.server_close()


def main():
    mixed_slow_peers_and_resets()
    backend_fin_first_while_client_uploads()
    failed_backend_then_recovery()
    print('PASS: Phase 32 slow peers, half-closes and failure stress suite', flush=True)


if __name__ == '__main__':
    main()
