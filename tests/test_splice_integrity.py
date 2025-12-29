#!/usr/bin/env python3
"""Phase 15: byte-exact, concurrent, streaming end-to-end splice integration.

Tests whole TCP streams, including preconnect boundary lengths and concurrent
full-duplex echoing. No external packages or privileged operations needed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import re
import socket
import socketserver
import threading
import time

from test_proxy import start_proxy, stop_proxy

ADDRESS = '127.0.0.1'
# Both small boundary cases and a transfer larger than TCP socket buffers.
SIZES = (0, 1, 7, 8191, 8192, 8193, 65535, 65536, 65537,
         262147, 1048699, 3 * 1024 * 1024 + 17)
CHUNKS = (1, 3, 8191, 4097, 65533, 90011)


class EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(35)
        while chunk := self.request.recv(32767):
            # Echo before FIN, exercising simultaneous traffic directions.
            self.request.sendall(chunk)


class EchoServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = True


def payload_for(index: int, length: int) -> bytes:
    # Deterministic binary, including arbitrary bytes, NULs and 0xff.
    return bytes(((i * 37 + index * 29) & 255) for i in range(length))


def exercise(port: int, index: int, length: int) -> tuple[int, int, str]:
    expected = payload_for(index, length)
    send_error: list[BaseException] = []
    with socket.create_connection((ADDRESS, port), timeout=15) as client:
        client.settimeout(35)

        def write_fragments() -> None:
            try:
                view = memoryview(expected)
                offset = 0
                piece = 0
                while offset < len(view):
                    count = min(CHUNKS[piece % len(CHUNKS)], len(view) - offset)
                    client.sendall(view[offset:offset + count])
                    offset += count
                    piece += 1
                client.shutdown(socket.SHUT_WR)
            except BaseException as exc:
                send_error.append(exc)

        writer = threading.Thread(target=write_fragments, daemon=True)
        writer.start()
        received = bytearray()
        while block := client.recv(32117):
            received.extend(block)
        writer.join(timeout=35)
        assert not writer.is_alive(), f'writer hung for length {length}'
        if send_error:
            raise send_error[0]
    actual = bytes(received)
    assert actual == expected, (index, len(actual), len(expected))
    sent_hash = hashlib.sha256(expected).hexdigest()
    assert hashlib.sha256(actual).hexdigest() == sent_hash
    return index, length, sent_hash


def main() -> None:
    with EchoServer((ADDRESS, 0), EchoHandler) as backend:
        listener = threading.Thread(target=backend.serve_forever, daemon=True)
        listener.start()
        proc, proxy_port = start_proxy(backend.server_address[1])
        lines: list[bytes] = []
        assert proc.stdout is not None
        reader = threading.Thread(target=lambda: lines.extend(proc.stdout), daemon=True)
        reader.start()
        try:
            with ThreadPoolExecutor(max_workers=8) as workers:
                results = list(workers.map(
                    lambda item: exercise(proxy_port, *item), enumerate(SIZES)
                ))
            assert len(results) == len(SIZES)
            # Allow the proxy's event loop to reach both EOFs, release both
            # pipes, print accounting, then keep accepting new clients.
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                done = sum(b'transfer-integrity=verified' in x for x in lines)
                if done == len(SIZES):
                    break
                time.sleep(.02)
            assert done == len(SIZES), (done, len(SIZES), b''.join(lines)[-3000:])
            assert proc.poll() is None, 'proxy unexpectedly exited'
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                stop_proxy(proc)
            reader.join(timeout=10)
            backend.shutdown()
        out = b''.join(lines)
        assert proc.returncode == -15, proc.returncode
        assert b'transfer-integrity=incomplete' not in out, out
        assert b'splice accounting invariant failed' not in out
        count = out.count(b'transfer-integrity=verified')
        assert count == len(SIZES), (count, out)
        completed = re.findall(
            rb'Closed connection: client-to-backend=(\d+) bytes, '
            rb'backend-to-client=(\d+) bytes', out
        )
        assert sorted((int(a), int(b)) for a, b in completed) == sorted(
            (length, length) for length in SIZES
        ), completed
        print(f'PASS: {len(SIZES)} concurrent streaming TCP sessions; '
              f'{sum(SIZES)} binary bytes each direction, byte-exact SHA-256')
        print('PASS: 0/1/8192/65536-byte boundaries and 3 MiB+ payload preserved')
        print(f'PASS: {count} completed sessions have consistent pipe/staged/forwarded accounting')
        print('PASS: Phase 15 splice integration and byte-integrity test suite')


if __name__ == '__main__':
    main()
