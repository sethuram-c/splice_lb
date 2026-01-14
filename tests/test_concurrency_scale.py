#!/usr/bin/env python3
"""Phase 30: measured 1k/5k/10k established, simultaneously live TCP sessions.

Default: 1000. Higher scales are explicit, never silently asserted as passed.
Each client has a separate socket; test-only backend uses selectors, not one
thread per connection. Report records successes, time, peak fds/RSS, CPU.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import resource
import socket
import time

from harness import HOST, proxy_session
from scalable_backend import scalable_echo_backend

MIB = 1024 * 1024


def proc_fds(pid: int) -> int:
    return len(os.listdir(f'/proc/{pid}/fd'))


def proc_rss_kib(pid: int) -> int:
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
        if line.startswith('VmRSS:'):
            return int(line.split()[1])
    raise RuntimeError('VmRSS missing from /proc')


def cpu_seconds(pid: int) -> float:
    # /proc/[pid]/stat comm is enclosed in parentheses and can contain spaces.
    raw = Path(f'/proc/{pid}/stat').read_text()
    tail = raw[raw.rfind(')') + 2:].split()
    return (int(tail[11]) + int(tail[12])) / os.sysconf('SC_CLK_TCK')


def preflight(count: int) -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    # Each proxy session owns two TCP sockets and two pairs of pipe ends.
    need = 6 * count + 96
    if soft < need:
        raise RuntimeError(
            f'RLIMIT_NOFILE soft={soft}, hard={hard} cannot hold {count} sessions: '
            f'proxy needs roughly {need} fds. On Fedora check `ulimit -Sn`, '
            '`ulimit -Hn`; raise limits before launching this test. '
            'No count or limit was silently reduced.')
    low, high = [int(n) for n in
                 Path('/proc/sys/net/ipv4/ip_local_port_range').read_text().split()]
    if high - low + 1 < count + 100:
        raise RuntimeError(
            f'ephemeral port range {low}-{high} may be insufficient for '
            f'{count} simultaneous connections; check sysctl net.ipv4.ip_local_port_range')


def recv_exact(sock: socket.socket, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        block = sock.recv(length - len(result))
        if not block:
            raise AssertionError(f'early EOF after {len(result)}/{length} bytes')
        result.extend(block)
    return bytes(result)


def probe(sock: socket.socket, nonce: int) -> None:
    marker = nonce.to_bytes(8, 'big') + b'P30!'
    sock.sendall(marker)
    received = recv_exact(sock, len(marker))
    if received != marker:
        raise AssertionError(f'payload corruption for nonce {nonce}')


def wait_for(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError(message)


def run(count: int, timeout: float, report: Path | None) -> dict:
    preflight(count)
    clients: list[socket.socket] = []
    started = time.monotonic()
    try:
        with ExitStack() as stack:
            backend = stack.enter_context(scalable_echo_backend())
            # Prevent idle timeout on large-scale connection establishment.
            proxy = stack.enter_context(proxy_session(
                backend.port, '--max-connections', str(count),
                '--idle-timeout-ms', '3600000', '--shutdown-grace-ms', '3000'))
            rss_baseline = proc_rss_kib(proxy.proc.pid)
            fd_baseline = proc_fds(proxy.proc.pid)
            cpu_start = cpu_seconds(proxy.proc.pid)
            peak_fds, peak_rss = fd_baseline, rss_baseline
            opened = time.monotonic()
            try:
                for i in range(count):
                    if time.monotonic() - started > timeout:
                        raise TimeoutError(f'only {i}/{count} sessions live at {timeout}s')
                    sock = socket.create_connection((HOST, proxy.port), timeout=10)
                    sock.settimeout(20)
                    clients.append(sock)
                    probe(sock, i)
                    if (i + 1) % 100 == 0 or i + 1 == count:
                        peak_fds = max(peak_fds, proc_fds(proxy.proc.pid))
                        peak_rss = max(peak_rss, proc_rss_kib(proxy.proc.pid))
                opened_seconds = time.monotonic() - opened
                wait_for(lambda: backend.snapshot()[1] == count,
                         10, 'backend live-count never reached target')
                fds_held = proc_fds(proxy.proc.pid)
                rss_held = proc_rss_kib(proxy.proc.pid)
                peak_fds = max(peak_fds, fds_held)
                peak_rss = max(peak_rss, rss_held)
                # Re-probe a deterministic sample after all sessions are live.
                sample = sorted(set([0, count // 4, count // 2, count - 1]))
                for index in sample:
                    probe(clients[index], count + index)
                cpu_held = cpu_seconds(proxy.proc.pid) - cpu_start
                accepted, live = backend.snapshot()
                assert live == count and accepted >= count, (accepted, live)
                # Must test active ceiling; a connect may succeed before rejection.
                overflow_rejected = False
                with socket.create_connection((HOST, proxy.port), timeout=5) as extra:
                    extra.settimeout(5)
                    try:
                        extra.sendall(b'over-limit')
                        overflow_rejected = not extra.recv(64)
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        overflow_rejected = True
                if not overflow_rejected:
                    raise AssertionError('max-connections limit not enforced')
                for sock in clients:
                    sock.close()
                clients.clear()
                wait_for(lambda: backend.snapshot()[1] == 0,
                         max(20, min(timeout, 120)), 'backend still has live sockets')
                wait_for(lambda: proc_fds(proxy.proc.pid) == fd_baseline,
                         max(20, min(timeout, 120)),
                         'proxy FD count did not return to baseline')
                elapsed = time.monotonic() - started
                result = {
                    'phase': 30, 'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                    'target_connections': count, 'verified_simultaneously_live': count,
                    'echo_probes_passed': count + len(sample),
                    'overflow_rejected': True,
                    'backend_total_accepted': accepted,
                    'establish_seconds': round(opened_seconds, 3),
                    'elapsed_seconds': round(elapsed, 3),
                    'proxy_cpu_seconds_to_hold': round(cpu_held, 3),
                    'proxy_fd_baseline': fd_baseline,
                    'proxy_fd_held': fds_held,
                    'proxy_fd_peak_observed': peak_fds,
                    'proxy_rss_baseline_kib': rss_baseline,
                    'proxy_rss_held_kib': rss_held,
                    'proxy_rss_peak_observed_kib': peak_rss,
                    'proxy_fd_after_cleanup': proc_fds(proxy.proc.pid),
                    'kernel': os.uname().release,
                }
                if report is not None:
                    report.parent.mkdir(parents=True, exist_ok=True)
                    report.write_text(json.dumps(result, indent=2) + '\n')
                print(f'PASS: {count} simultaneously live client/backend sessions; '
                      f'{result["echo_probes_passed"]} byte-exact probes')
                print(f'PASS: connection cap rejected overflow; '
                      f'fds returned to {fd_baseline}')
                print(f'OBSERVED: establish={opened_seconds:.2f}s, '
                      f'proxy RSS={rss_held / 1024:.1f} MiB, '
                      f'proxy fds={fds_held}, CPU={cpu_held:.2f}s')
                if report:
                    print(f'REPORT: {report}')
                return result
            except BaseException as exc:
                raise AssertionError(
                    f'Phase 30 failed at {len(clients)}/{count} clients: {exc}; '
                    f'proxy log tail:\n{proxy.logs()[-1500:]}') from exc
    finally:
        for sock in clients:
            sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connections', type=int, default=1000)
    parser.add_argument('--timeout', type=float, default=300,
                        help='overall establishment deadline, seconds (default 300)')
    parser.add_argument('--report', type=Path, help='optional JSON report outside repo')
    args = parser.parse_args()
    if args.connections < 1 or args.connections > 10000:
        parser.error('--connections must be from 1 to 10000')
    if not 30 <= args.timeout <= 3600:
        parser.error('--timeout must be from 30 to 3600 seconds')
    try:
        run(args.connections, args.timeout, args.report)
    except RuntimeError as exc:
        parser.exit(2, f'ERROR: {exc}\n')
    print('PASS: Phase 30 high-concurrency test suite')


if __name__ == '__main__':
    main()
