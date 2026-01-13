#!/usr/bin/env python3
"""Reusable, dependency-free TCP proxy integration-test fixtures.

Servers use loopback port 0. Proxy logs go to a regular file instead of an
undrained stdout PIPE, preventing process stalls under verbose logging.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / 'build' / 'splice-lb'
HOST = '127.0.0.1'


class _EchoHandler(socketserver.BaseRequestHandler):
    """Collect full TCP request, echo after client write-side EOF."""
    def handle(self) -> None:
        self.request.settimeout(15)
        payload = bytearray()
        while True:
            block = self.request.recv(65536)
            if not block:
                break
            payload.extend(block)
        self.request.sendall(bytes(payload))


class _EchoServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = True


@contextmanager
def echo_backend() -> Iterator[int]:
    server = _EchoServer((HOST, 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        if thread.is_alive():
            raise AssertionError('backend thread did not stop')


class _StreamingEchoHandler(socketserver.BaseRequestHandler):
    """Echo bytes as they arrive, without retaining the complete request."""
    def handle(self) -> None:
        self.request.settimeout(90)
        while True:
            block = self.request.recv(65536)
            if not block:
                break
            self.request.sendall(block)


@contextmanager
def streaming_echo_backend() -> Iterator[int]:
    """A bounded-memory echo fixture for multi-megabyte duplex tests."""
    server = _EchoServer((HOST, 0), _StreamingEchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        if thread.is_alive():
            raise AssertionError('streaming backend thread did not stop')


@dataclass
class ProxySession:
    proc: subprocess.Popen
    port: int
    logfile: Path

    def logs(self) -> str:
        return self.logfile.read_text(encoding='utf-8', errors='replace')


@contextmanager
def proxy_session(backend_port: int, *options: str,
                  startup_timeout: float = 5.0) -> Iterator[ProxySession]:
    """Wait for startup banner; reap child even when a test assertion fails."""
    with tempfile.TemporaryDirectory(prefix='splice-phase28-') as directory:
        logfile = Path(directory) / 'proxy.log'
        with logfile.open('wb') as sink:
            proc = subprocess.Popen(
                [str(PROXY), *options, '0', f'{HOST}:{backend_port}'],
                stdin=subprocess.DEVNULL, stdout=sink,
                stderr=subprocess.STDOUT, close_fds=True)
        try:
            banner = re.compile(r'^Listening on 127\.0\.0\.1:(\d+)\b', re.MULTILINE)
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                output = logfile.read_text(encoding='utf-8', errors='replace')
                match = banner.search(output)
                if match:
                    yield ProxySession(proc, int(match.group(1)), logfile)
                    break
                if proc.poll() is not None:
                    raise AssertionError(
                        f'proxy exited during startup ({proc.returncode}):\n{output[-2000:]}')
                time.sleep(.025)
            else:
                raise AssertionError('proxy did not start: ' + logfile.read_text()[-2000:])
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)


def deterministic_bytes(length: int, seed: int = 0x28) -> bytes:
    if length < 0:
        raise ValueError('negative payload size')
    return bytes(((i * 37 + (i >> 7) * 11 + seed) & 255) for i in range(length))


def assert_bytes_equal(expected: bytes, actual: bytes, *, label: str) -> None:
    """Diagnose mismatches by offset and hash instead of printing huge bytes."""
    if expected == actual:
        return
    offset = next((i for i, (x, y) in enumerate(zip(expected, actual)) if x != y),
                  min(len(expected), len(actual)))
    raise AssertionError(
        f'{label}: byte mismatch at offset {offset}; '
        f'expected len={len(expected)} sha256={hashlib.sha256(expected).hexdigest()}; '
        f'actual len={len(actual)} sha256={hashlib.sha256(actual).hexdigest()}')


def request(port: int, payload: bytes, *, fragment_sizes: tuple[int, ...] = (65536,),
            timeout: float = 15.0) -> bytes:
    """Concurrent send/receive prevents full-duplex TCP test deadlock."""
    if not fragment_sizes or any(n <= 0 for n in fragment_sizes):
        raise ValueError('fragment sizes must be positive')
    failures: list[BaseException] = []
    with socket.create_connection((HOST, port), timeout=timeout) as client:
        client.settimeout(timeout)

        def write_fragments() -> None:
            try:
                view = memoryview(payload)
                offset = piece = 0
                while offset < len(view):
                    count = fragment_sizes[piece % len(fragment_sizes)]
                    stop = min(len(view), offset + count)
                    client.sendall(view[offset:stop])
                    offset = stop
                    piece += 1
                client.shutdown(socket.SHUT_WR)
            except BaseException as exc:
                failures.append(exc)

        writer = threading.Thread(target=write_fragments, daemon=True)
        writer.start()
        result = bytearray()
        try:
            while block := client.recv(65536):
                result.extend(block)
        finally:
            writer.join(timeout=timeout)
        if writer.is_alive():
            raise AssertionError('sending thread did not terminate')
        if failures:
            raise AssertionError(f'sending thread failed: {failures[0]}') from failures[0]
        return bytes(result)
