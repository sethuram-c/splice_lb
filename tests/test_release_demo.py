#!/usr/bin/env python3
"""Phase 35 release smoke: verify runnable demo, output schema and CLI."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'demo' / 'final_demo.py'


def main() -> None:
    help_out = subprocess.run([sys.executable, str(DEMO), '--help'],
                              capture_output=True, text=True, timeout=5)
    assert help_out.returncode == 0, help_out.stderr
    assert '--algorithm' in help_out.stdout and '--report' in help_out.stdout
    bad = subprocess.run([sys.executable, str(DEMO), '--algorithm', 'random'],
                         capture_output=True, text=True, timeout=5)
    assert bad.returncode != 0
    with tempfile.TemporaryDirectory(prefix='phase35-release-') as tmp:
        report = Path(tmp) / 'results.json'
        proc = subprocess.run([sys.executable, str(DEMO), '--report', str(report)],
                              capture_output=True, text=True, timeout=80)
        if proc.returncode:
            raise AssertionError(f'demo failed:\n{proc.stdout}\n{proc.stderr}')
        info = json.loads(report.read_text(encoding='utf-8'))
        assert info['schema_version'] == 1
        assert info['suite'] == 'phase35-final-demo'
        assert [x['algorithm'] for x in info['results']] == [
            'round-robin', 'least-connections']
        rr, lc = info['results']
        assert rr['selections'] == [0, 1, 2, 0, 1, 2]
        assert rr['payload_sizes'] == [0, 1, 11, 8192, 65537, 131071]
        assert len(rr['sha256']) == 6
        assert lc['initial_active'] == [2, 2, 2]
        assert lc['new_selection'] == [2]
        assert 'PASS: Phase 35 final demo' in proc.stdout
    print('PASS: release demo starts/stops all servers, validates RR and Least Connections')
    print('PASS: byte-exact payloads, graceful shutdown, CLI validation and JSON schema')
    print('PASS: Phase 35 final release demo test suite')


if __name__ == '__main__':
    main()
