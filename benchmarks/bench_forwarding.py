#!/usr/bin/env python3
"""Phase 34: local end-to-end latency/goodput benchmark (stdlib only).

Results describe the ENTIRE loopback path and Python load generator, NOT isolated
splice() syscall speed. Do not interpret a single run as a universal speedup.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from scalable_backend import scalable_echo_backend  # noqa: E402
from harness import HOST, proxy_session  # noqa: E402

BASELINE = ROOT / 'build' / 'blocking-baseline'


def percentile(values: list[float], pct: float) -> float:
    """Nearest rank percentile; no interpolation, stable for small samples."""
    if not values or not 0 <= pct <= 100:
        raise ValueError('invalid percentile input')
    ranked = sorted(values)
    return ranked[max(0, math.ceil(len(ranked) * pct / 100) - 1)]


def proc_cpu_seconds(pid: int) -> float:
    """CPU utime+stime from /proc/<pid>/stat, comm may contain spaces."""
    content = Path(f'/proc/{pid}/stat').read_text()
    fields = content[content.rfind(')') + 2:].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')


def proc_peak_rss_kib(pid: int) -> int | None:
    try:
        status = Path(f'/proc/{pid}/status').read_text()
    except FileNotFoundError:
        return None
    match = re.search(r'^VmHWM:\s+(\d+)\s+kB', status, re.MULTILINE)
    return int(match.group(1)) if match else None


@contextmanager
def blocking_proxy(backend_port: int) -> Iterator[tuple[int, int]]:
    if not BASELINE.is_file():
        raise FileNotFoundError(f'build baseline first: make {BASELINE.relative_to(ROOT)}')
    with tempfile.TemporaryDirectory(prefix='splice-phase34-baseline-') as folder:
        logfile = Path(folder) / 'baseline.log'
        with logfile.open('wb') as output:
            proc = subprocess.Popen([str(BASELINE), '0', str(backend_port)],
                                    stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.STDOUT, close_fds=True)
        try:
            deadline = time.monotonic() + 5
            match = None
            while time.monotonic() < deadline:
                match = re.search(r'Listening on 127\.0\.0\.1:(\d+)',
                                  logfile.read_text(errors='replace'))
                if match:
                    break
                if proc.poll() is not None:
                    raise RuntimeError('baseline exited: ' + logfile.read_text()[-1500:])
                time.sleep(.02)
            if not match:
                raise RuntimeError('baseline startup timeout')
            yield int(match.group(1)), proc.pid
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)


def read_exact(sock: socket.socket, required: int) -> bytes:
    blocks = bytearray()
    while len(blocks) < required:
        portion = sock.recv(required - len(blocks))
        if not portion:
            raise AssertionError(f'early EOF after {len(blocks)}/{required} bytes')
        blocks.extend(portion)
    return bytes(blocks)


def latency_work(port: int, requests: int, barrier: threading.Barrier) -> list[float]:
    payload = b'phase34/latency' + bytes(range(48))
    measurements = []
    with socket.create_connection((HOST, port), timeout=15) as peer:
        peer.settimeout(15)
        peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        barrier.wait(timeout=15)
        for _ in range(requests):
            begin = time.perf_counter_ns()
            peer.sendall(payload)
            result = read_exact(peer, len(payload))
            elapsed = (time.perf_counter_ns() - begin) / 1_000_000
            if result != payload:
                raise AssertionError('latency echo bytes corrupted')
            measurements.append(elapsed)
        peer.shutdown(socket.SHUT_WR)
        if peer.recv(1):
            raise AssertionError('unexpected extra response after latency requests')
    return measurements


def throughput_work(port: int, payload: bytes, barrier: threading.Barrier) -> int:
    failures: list[BaseException] = []
    expected = hashlib.sha256(payload).digest()
    with socket.create_connection((HOST, port), timeout=30) as peer:
        peer.settimeout(30)
        barrier.wait(timeout=20)

        def writer() -> None:
            try:
                for start in range(0, len(payload), 16384):
                    peer.sendall(payload[start:start + 16384])
                peer.shutdown(socket.SHUT_WR)
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=writer, daemon=True)
        worker.start()
        observed = hashlib.sha256()
        received = 0
        while True:
            block = peer.recv(65536)
            if not block:
                break
            observed.update(block)
            received += len(block)
        worker.join(timeout=30)
        if worker.is_alive():
            raise AssertionError('writer deadlocked')
        if failures:
            raise AssertionError(f'writer failed: {failures[0]}') from failures[0]
        if received != len(payload) or observed.digest() != expected:
            raise AssertionError(f'corrupt forwarding: received {received}, expected {len(payload)}')
        return received


def run_latency(port: int, proc_pid: int, clients: int, requests: int) -> dict:
    barrier = threading.Barrier(clients + 1)
    with ThreadPoolExecutor(max_workers=clients) as executor:
        futures = [executor.submit(latency_work, port, requests, barrier)
                   for _ in range(clients)]
        before_cpu = proc_cpu_seconds(proc_pid)
        barrier.wait(timeout=15)
        start = time.perf_counter()
        measurements = [m for future in futures for m in future.result(timeout=90)]
        elapsed = time.perf_counter() - start
    return {
        'clients': clients, 'requests': len(measurements),
        'elapsed_seconds': round(elapsed, 6),
        'request_rate_per_second': round(len(measurements) / elapsed, 3),
        'p50_ms': round(percentile(measurements, 50), 4),
        'p95_ms': round(percentile(measurements, 95), 4),
        'p99_ms': round(percentile(measurements, 99), 4),
        'proxy_cpu_seconds': round(proc_cpu_seconds(proc_pid) - before_cpu, 6),
        'proxy_peak_rss_kib': proc_peak_rss_kib(proc_pid),
    }


def run_throughput(port: int, proc_pid: int, clients: int, size: int) -> dict:
    payload = bytes((i * 37 + (i >> 8) * 11 + 0x34) & 255 for i in range(size))
    barrier = threading.Barrier(clients + 1)
    with ThreadPoolExecutor(max_workers=clients) as executor:
        futures = [executor.submit(throughput_work, port, payload, barrier)
                   for _ in range(clients)]
        before_cpu = proc_cpu_seconds(proc_pid)
        barrier.wait(timeout=20)
        start = time.perf_counter()
        total = sum(future.result(timeout=150) for future in futures)
        elapsed = time.perf_counter() - start
    return {
        'clients': clients, 'bytes_per_direction': total,
        'elapsed_seconds': round(elapsed, 6),
        'goodput_mib_per_second': round(total / (1024 ** 2) / elapsed, 3),
        'proxy_cpu_seconds': round(proc_cpu_seconds(proc_pid) - before_cpu, 6),
        'proxy_peak_rss_kib': proc_peak_rss_kib(proc_pid),
        'byte_exact': True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quick', action='store_true', help='fast CI/smoke workload')
    parser.add_argument('--implementations', choices=['both', 'splice', 'blocking'], default='both')
    parser.add_argument('--clients', default=None, help='comma-separated client counts (e.g. 1,8,32)')
    parser.add_argument('--requests', type=int, default=40, help='ping-pongs/client for latency')
    parser.add_argument('--payload-kib', type=int, default=512, help='bytes/client in KiB for goodput')
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--output', type=Path, default=None, help='JSON output, prefer /tmp/...')
    args = parser.parse_args()
    if args.quick:
        clients = [1, 4] if args.clients is None else [int(x) for x in args.clients.split(',')]
        requests, payload_kib, repetitions = 8, 64, 1
    else:
        clients = [1, 8, 32] if args.clients is None else [int(x) for x in args.clients.split(',')]
        requests, payload_kib, repetitions = args.requests, args.payload_kib, args.repetitions
    if (not clients or any(c < 1 or c > 128 for c in clients)
            or requests < 1 or payload_kib < 1 or repetitions < 1 or repetitions > 20):
        parser.error('clients 1..128, requests/payload/repetitions must be positive; repetitions <= 20')
    choice = ['splice', 'blocking'] if args.implementations == 'both' else [args.implementations]
    report = {
        'schema_version': 1,
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'environment': {
            'platform': platform.platform(),
            'kernel': platform.release(),
            'python_version': platform.python_version(),
            'cpus': os.cpu_count(),
            'rlimit_nofile_soft': resource.getrlimit(resource.RLIMIT_NOFILE)[0],
            'scope': 'local loopback end-to-end echo; includes Python generator/backend overhead',
        },
        'configuration': {'implementations': choice, 'clients': clients,
                          'requests_per_client': requests, 'payload_kib': payload_kib,
                          'repetitions': repetitions, 'nodelay_client': True},
        'runs': [],
    }
    with scalable_echo_backend() as backend:
        for implementation in choice:
            context = (proxy_session(backend.port, '--max-connections', '256')
                       if implementation == 'splice' else blocking_proxy(backend.port))
            with context as handle:
                if implementation == 'splice':
                    port, proc_pid = handle.port, handle.proc.pid
                else:
                    port, proc_pid = handle
                for concurrency in clients:
                    for repeat in range(repetitions):
                        latency = run_latency(port, proc_pid, concurrency, requests)
                        throughput = run_throughput(port, proc_pid, concurrency, payload_kib * 1024)
                        run = {'implementation': implementation, 'clients': concurrency,
                               'repetition': repeat + 1,
                               'latency': latency, 'throughput': throughput}
                        report['runs'].append(run)
                        print(f'{implementation:8s} clients={concurrency:3d} '
                              f'run={repeat + 1} p50={latency["p50_ms"]:8.3f} ms '
                              f'p99={latency["p99_ms"]:8.3f} ms '
                              f'goodput={throughput["goodput_mib_per_second"]:8.2f} MiB/s',
                              flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(f'report={args.output}')
    print('PASS: Phase 34 benchmark, all payloads byte-exact')


if __name__ == '__main__':
    main()
