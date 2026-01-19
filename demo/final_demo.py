#!/usr/bin/env python3
"""Self-contained Phase 35 demo: three tagged backends and the real C proxy.

Runs entirely on loopback using kernel-assigned ports. Does not benchmark speed.
Writes an optional machine-readable report outside the repository.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from rr_backends import start_backend  # noqa: E402

PROXY = ROOT / 'build' / 'splice-lb'
HOST = '127.0.0.1'
READY = re.compile(r'^Listening on 127\.0\.0\.1:(\d+) -> ([a-z-]+) \(3 backends\)', re.MULTILINE)


def receive_all(peer: socket.socket) -> bytes:
    data = bytearray()
    while True:
        block = peer.recv(65536)
        if not block:
            return bytes(data)
        data.extend(block)


def send_request(port: int, payload: bytes) -> tuple[int, str]:
    """Read a tagged echo and prove exact binary payload integrity."""
    with socket.create_connection((HOST, port), timeout=5) as peer:
        peer.settimeout(15)
        peer.sendall(payload)
        peer.shutdown(socket.SHUT_WR)
        answer = receive_all(peer)
    for index in range(3):
        prefix = f'backend-{index}|'.encode('ascii')
        if answer.startswith(prefix):
            observed = answer[len(prefix):]
            if observed != payload:
                raise AssertionError(f'backend {index}: echo differs from original bytes')
            return index, hashlib.sha256(observed).hexdigest()
    raise AssertionError(f'unknown backend marker: {answer[:40]!r}')


def open_held_session(port: int) -> tuple[socket.socket, int]:
    peer = socket.create_connection((HOST, port), timeout=5)
    peer.settimeout(15)
    try:
        # Each backend sends its marker immediately on TCP connect. The proxy
        # must not need a request payload to forward backend -> client data.
        tag = bytearray()
        while not tag.endswith(b'|'):
            block = peer.recv(1)
            if not block or len(tag) >= 32:
                raise AssertionError(f'no backend marker: {bytes(tag)!r}')
            tag.extend(block)
        match = re.fullmatch(rb'backend-([012])\|', bytes(tag))
        if not match:
            raise AssertionError(f'unexpected tag: {bytes(tag)!r}')
        return peer, int(match.group(1))
    except BaseException:
        peer.close()
        raise


def finish_held(peer: socket.socket) -> None:
    try:
        peer.shutdown(socket.SHUT_WR)
        if receive_all(peer):
            raise AssertionError('held marker-only connection returned extra data')
    finally:
        peer.close()


@contextmanager
def three_backends() -> Iterator[list[str]]:
    running = []
    try:
        for index in range(3):
            server, thread = start_backend(0, index)
            running.append((server, thread))
        yield [f'{HOST}:{server.server_address[1]}' for server, _ in running]
    finally:
        for server, thread in running:
            server.shutdown()
            server.server_close()
            thread.join(timeout=4)
            if thread.is_alive():
                raise AssertionError('backend did not shut down')


@contextmanager
def started_proxy(algorithm: str, addresses: list[str]) -> Iterator[tuple[int, Path]]:
    with tempfile.TemporaryDirectory(prefix='splice-phase35-') as folder:
        logfile = Path(folder) / 'proxy.log'
        with logfile.open('wb') as sink:
            proc = subprocess.Popen(
                [str(PROXY), '--algorithm', algorithm, '--shutdown-grace-ms', '1500',
                 '0', *addresses], cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=sink, stderr=subprocess.STDOUT, close_fds=True)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                log = logfile.read_text(encoding='utf-8', errors='replace')
                matched = READY.search(log)
                if matched:
                    if matched.group(2) != algorithm:
                        raise AssertionError('incorrect proxy algorithm')
                    yield int(matched.group(1)), logfile
                    break
                if proc.poll() is not None:
                    raise AssertionError(f'proxy exited before ready: {log[-1800:]}')
                time.sleep(.03)
            else:
                raise AssertionError('proxy startup timed out: ' + logfile.read_text()[-1200:])
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
            try:
                status = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=4)
                raise AssertionError('proxy failed to shut down within deadline')
            log = logfile.read_text(encoding='utf-8', errors='replace')
            if status != -signal.SIGTERM:
                raise AssertionError(f'proxy unexpected shutdown status={status}: {log[-1800:]}')
            if 'event=shutdown_started' not in log or 'event=server_stopped' not in log:
                raise AssertionError(f'missing lifecycle logs: {log[-1800:]}')
            if 'splice accounting invariant failed' in log or 'underflow' in log:
                raise AssertionError(f'proxy invariant failed: {log[-1800:]}')


def run_round_robin(addresses: list[str]) -> dict:
    lengths = [0, 1, 11, 8192, 65537, 131071]
    selections = []
    hashes = []
    with started_proxy('round-robin', addresses) as (port, _):
        for serial, size in enumerate(lengths):
            payload = bytes(((i * 37 + serial) & 255) for i in range(size))
            selected, digest = send_request(port, payload)
            selections.append(selected)
            hashes.append(digest)
    if selections != [0, 1, 2, 0, 1, 2]:
        raise AssertionError(f'wrong Round Robin order: {selections}')
    print('PASS: round-robin selections 0,1,2,0,1,2; six byte-exact binary responses')
    return {'algorithm': 'round-robin', 'selections': selections,
            'payload_sizes': lengths, 'sha256': hashes, 'result': 'pass'}


def run_least_connections(addresses: list[str]) -> dict:
    held = []
    selected = []
    try:
        with started_proxy('least-connections', addresses) as (port, _):
            for _ in range(6):
                peer, backend = open_held_session(port)
                held.append((peer, backend))
            initial = [sum(1 for _, idx in held if idx == i) for i in range(3)]
            if initial != [2, 2, 2]:
                raise AssertionError(f'unexpected initial Least Connections balance: {initial}')
            remaining = []
            for peer, backend in held:
                if backend == 2:
                    finish_held(peer)
                else:
                    remaining.append((peer, backend))
            held = remaining
            # recv-to-EOF synchronizes the FIN path through the proxy. Wait
            # until the release bookkeeping has been emitted by the event loop.
            selected_backend, _ = send_request(port, b'phase35-least-connections')
            if selected_backend != 2:
                raise AssertionError(f'expected least-loaded backend 2, got {selected_backend}')
            selected.append(selected_backend)
            for peer, _ in held:
                finish_held(peer)
            held.clear()
    finally:
        for peer, _ in held:
            peer.close()
    print('PASS: least-connections held 2/2/2, released backend 2, new request selected backend 2')
    return {'algorithm': 'least-connections', 'initial_active': [2, 2, 2],
            'freed_backend': 2, 'new_selection': selected, 'result': 'pass'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--algorithm', choices=['both', 'round-robin', 'least-connections'],
                        default='both')
    parser.add_argument('--report', type=Path, help='optional output JSON (use /tmp, not repository)')
    options = parser.parse_args()
    if not PROXY.is_file():
        parser.error('missing build/splice-lb; run make first')
    output = {'schema_version': 1, 'suite': 'phase35-final-demo', 'results': []}
    with three_backends() as addresses:
        if options.algorithm in ('both', 'round-robin'):
            output['results'].append(run_round_robin(addresses))
        if options.algorithm in ('both', 'least-connections'):
            output['results'].append(run_least_connections(addresses))
    if options.report is not None:
        options.report.parent.mkdir(parents=True, exist_ok=True)
        options.report.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
    print('PASS: Phase 35 final demo and graceful shutdown')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, TimeoutError) as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        raise SystemExit(1) from exc
