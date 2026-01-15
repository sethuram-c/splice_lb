#!/usr/bin/env python3
"""Phase 31: real-TCP Round Robin vs Least Connections churn stress.

Three tagged TCP echo backends share the same deterministic lifetime schedule.
Each algorithm runs in an independent load-balancer process. Long-lived TCP
sessions are deliberately uneven before new arrivals; selection and byte
integrity are observed over the network, not inferred from log counts alone.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import socket

from rr_backends import start_backend
from test_least_connections import Proxy, finish, open_held, wait_until

ALGORITHMS = ('round-robin', 'least-connections')
BACKENDS = 3


def verify_probe(sock: socket.socket, tag: bytes) -> None:
    """Send binary bytes on a held-open TCP stream and verify exact echo."""
    sock.sendall(tag)
    found = bytearray()
    while len(found) < len(tag):
        chunk = sock.recv(len(tag) - len(found))
        if not chunk:
            raise AssertionError(f'premature backend EOF: {len(found)} / {len(tag)}')
        found.extend(chunk)
    if bytes(found) != tag:
        raise AssertionError(f'echo mismatch for {tag!r}: {bytes(found)!r}')


def open_verified(port: int, serial: int) -> tuple[socket.socket, int]:
    sock, index = open_held(port)
    try:
        # NUL and 0xff ensure the comparison does not accidentally rely on text.
        marker = b'\x00\xffphase31:' + serial.to_bytes(4, 'big') + b'\x00'
        verify_probe(sock, marker)
        return sock, index
    except BaseException:
        sock.close()
        raise


def counts_of(held: list[tuple[socket.socket, int]]) -> list[int]:
    counter = Counter(index for _, index in held)
    return [counter[index] for index in range(BACKENDS)]


def imbalance(counts: list[int]) -> int:
    return max(counts) - min(counts)


def run_algorithm(addresses: list[str], algorithm: str) -> dict:
    proxy = None
    held: list[tuple[socket.socket, int]] = []
    released = 0
    created = 0
    stages = []
    try:
        proxy = Proxy(addresses, algorithm=algorithm)

        def open_batch(count: int, parallel: bool = False) -> None:
            nonlocal created
            ids = list(range(created, created + count))
            if parallel:
                with ThreadPoolExecutor(max_workers=16) as pool:
                    new_clients = list(pool.map(lambda i: open_verified(proxy.port, i), ids))
            else:
                new_clients = [open_verified(proxy.port, i) for i in ids]
            held.extend(new_clients)
            created += count

        def close_from(index: int, count: int) -> None:
            nonlocal released
            matched = [(sock, chosen) for sock, chosen in held if chosen == index]
            if len(matched) < count:
                raise AssertionError(f'backend {index}: cannot remove {count} from {len(matched)}')
            for sock, _ in matched[:count]:
                finish(sock)
                held.remove((sock, index))
            released += count
            wait_until(lambda: proxy.releases() >= released, timeout=12)

        def record(label: str) -> None:
            counts = counts_of(held)
            if sum(counts) != len(held):
                raise AssertionError('wrong active count')
            stages.append({'stage': label, 'active': counts, 'spread': imbalance(counts)})
            print(f'  {algorithm:17s} {label:16s} active={counts} spread={imbalance(counts)}', flush=True)

        open_batch(24)
        if counts_of(held) != [8, 8, 8]:
            raise AssertionError(f'initial distribution not even: {counts_of(held)}')
        record('baseline')

        # Exactly the same arrival/departure schedule for both algorithms.
        # RR advances on every connection, while LC responds to live slots.
        for label, closing_backend, count_to_close, arrivals in (
            ('skew-backend-2', 2, 6, 18),
            ('skew-backend-1', 1, 6, 18),
            ('skew-backend-0', 0, 6, 18),
            ('deep-skew-2', 2, 12, 24),
        ):
            close_from(closing_backend, count_to_close)
            open_batch(arrivals)
            record(label)

        # Concurrent arrival wave on top of many long-lived sessions.
        open_batch(30, parallel=True)
        record('parallel-burst')
        if created != 132 or len(held) != 102:
            raise AssertionError(f'unexpected workload size: created={created}, held={len(held)}')
        if len(proxy.snapshot()) == 0:
            raise AssertionError('proxy emitted no logs')

        # Avoid interpreting a successful echo as sufficient proof of correct
        # backend accounting: release every held-open socket and check logs.
        for sock, _ in held:
            finish(sock)
        held.clear()
        released += 102
        wait_until(lambda: proxy.releases() == created, timeout=15)
        lines = proxy.snapshot()
        selects = [line for line in lines if line.startswith('Selected backend[')]
        verified = sum('transfer-integrity=verified' in line for line in lines)
        if len(selects) != created or verified != created:
            raise AssertionError(f'logs mismatch: selections={len(selects)}, '
                                 f'verified={verified}, created={created}')
        if not any('backend-active=0; total-active=0' in line for line in lines):
            raise AssertionError('final active connections did not reach zero')
        if any('accounting underflow' in line for line in lines):
            raise AssertionError('backend accounting underflow')
        print(f'PASS: {algorithm}: {created} byte-exact sessions, '
              '102 held at peak, all slots released', flush=True)
        return {'algorithm': algorithm, 'connections_tested': created,
                'peak_live_connections': 102, 'integrity_verified': verified,
                'stages': stages}
    finally:
        for sock, _ in held:
            sock.close()
        if proxy is not None:
            proxy.close()


def validate_comparison(results: dict[str, dict]) -> None:
    rr = {s['stage']: s for s in results['round-robin']['stages']}
    lc = {s['stage']: s for s in results['least-connections']['stages']}
    for key in ('skew-backend-2', 'skew-backend-1', 'deep-skew-2', 'parallel-burst'):
        if not lc[key]['spread'] < rr[key]['spread']:
            raise AssertionError(f'LC not more balanced at {key}: '
                                 f'RR={rr[key]}, LC={lc[key]}')
    # Do not claim LC is universally better; this is a controlled scenario.
    print('PASS: Least Connections has smaller active-load spread than '
          'Round Robin at four unequal-lifetime checkpoints', flush=True)


def run(report_json: Path | None, report_csv: Path | None) -> dict:
    servers = []
    try:
        for index in range(BACKENDS):
            server, _ = start_backend(0, index)
            servers.append(server)
        addresses = [f'127.0.0.1:{server.server_address[1]}' for server in servers]
        results = {alg: run_algorithm(addresses, alg) for alg in ALGORITHMS}
        validate_comparison(results)
        report = {'phase': 31, 'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                  'scope': 'connection-level fairness under one controlled lifetime schedule',
                  'algorithms': results}
        if report_json:
            report_json.parent.mkdir(parents=True, exist_ok=True)
            report_json.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            print(f'REPORT: {report_json}', flush=True)
        if report_csv:
            report_csv.parent.mkdir(parents=True, exist_ok=True)
            with report_csv.open('w', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow(['algorithm', 'stage', 'backend0', 'backend1',
                                 'backend2', 'active_total', 'active_spread'])
                for alg in ALGORITHMS:
                    for stage in results[alg]['stages']:
                        counts = stage['active']
                        writer.writerow([alg, stage['stage'], *counts, sum(counts), stage['spread']])
            print(f'REPORT: {report_csv}', flush=True)
        return report
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', type=Path, help='optional results JSON; use /tmp or another external directory')
    parser.add_argument('--csv', type=Path, help='optional comparison CSV; use /tmp or another external directory')
    args = parser.parse_args()
    run(args.json, args.csv)
    print('PASS: Phase 31 Round Robin and Least Connections stress test suite', flush=True)


if __name__ == '__main__':
    main()
