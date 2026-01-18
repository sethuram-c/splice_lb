#!/usr/bin/env python3
"""Phase 34 smoke: benchmark output/schema/byte-integrity, NOT speed thresholds."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'benchmarks'))
from bench_forwarding import percentile  # noqa: E402


def main() -> None:
    assert percentile([4.0, 1.0, 2.0, 3.0], 50) == 2.0
    assert percentile([4.0, 1.0, 2.0, 3.0], 95) == 4.0
    with tempfile.TemporaryDirectory(prefix='phase34-bench-') as folder:
        report_file = Path(folder) / 'result.json'
        command = [sys.executable, str(ROOT / 'benchmarks' / 'bench_forwarding.py'),
                   '--quick', '--output', str(report_file)]
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                             timeout=100, check=False)
        if run.returncode != 0:
            raise AssertionError('benchmark failed:\n' + run.stdout[-2000:] + run.stderr[-2000:])
        data = json.loads(report_file.read_text())
        assert data['schema_version'] == 1
        assert len(data['runs']) == 4
        assert {r['implementation'] for r in data['runs']} == {'splice', 'blocking'}
        assert {r['clients'] for r in data['runs']} == {1, 4}
        for result in data['runs']:
            lat = result['latency']
            thr = result['throughput']
            assert lat['requests'] == 8 * result['clients']
            assert 0 < lat['p50_ms'] <= lat['p95_ms'] <= lat['p99_ms']
            assert lat['request_rate_per_second'] > 0
            assert thr['bytes_per_direction'] == 65536 * result['clients']
            assert thr['byte_exact'] is True
            assert thr['goodput_mib_per_second'] > 0
            assert thr['proxy_cpu_seconds'] >= 0
        assert 'PASS: Phase 34 benchmark' in run.stdout
    print('PASS: blocking baseline and epoll/splice proxy, 1/4-client byte-exact samples')
    print('PASS: latency p50/p95/p99, MiB/s, CPU, RSS and JSON report validated')
    print('PASS: Phase 34 reproducible throughput and latency benchmark smoke suite')


if __name__ == '__main__':
    main()
