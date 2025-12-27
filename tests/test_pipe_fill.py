#!/usr/bin/env python3
"""Phase 13: actual partial splice and full-pipe EAGAIN in both directions."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import tempfile
from pathlib import Path

from test_proxy import (TIMEOUT, recv_until_eof, run_backend,
                        start_proxy, stop_proxy, wait_for_proxy_cleanup)

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = bytes(range(256)) * 4096  # 1 MiB, includes NUL and invalid UTF-8
REPLY = PAYLOAD[::-1] + b'END'


def build_probe(path: Path) -> Path:
    result = path / 'pipe_fill_probe.so'
    subprocess.run([
        os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Wextra',
        '-Wpedantic', '-Werror', '-shared', '-fPIC',
        str(ROOT / 'tests' / 'pipe_fill_probe.c'), '-o', str(result),
    ], check=True, capture_output=True, timeout=TIMEOUT)
    return result


def check_partial_and_pipe_full() -> None:
    def backend(conn: socket.socket) -> None:
        conn.settimeout(25)
        assert recv_until_eof(conn) == PAYLOAD
        conn.sendall(REPLY)
        conn.shutdown(socket.SHUT_WR)

    def action(backend_port: int) -> None:
        with tempfile.TemporaryDirectory(prefix='phase13-') as folder:
            path = Path(folder)
            probe = build_probe(path)
            env = os.environ.copy()
            env['LD_PRELOAD'] = str(probe)
            env['PHASE13_SPLICE_LOG'] = str(path / 'splice.txt')
            process, port = start_proxy(backend_port, env=env)
            try:
                with socket.create_connection(('127.0.0.1', port), TIMEOUT) as client:
                    client.settimeout(25)
                    client.sendall(PAYLOAD)
                    client.shutdown(socket.SHUT_WR)
                    assert recv_until_eof(client) == REPLY
                wait_for_proxy_cleanup(process)
                process.terminate()
                stdout, stderr = process.communicate(timeout=TIMEOUT)
                assert process.returncode == -15, (stdout, stderr)
                assert stderr == b'', stderr
                assert (f'client-to-backend={len(PAYLOAD)} bytes, '
                        f'backend-to-client={len(REPLY)} bytes').encode() in stdout
                counts = re.search(
                    rb'partial-pipe-writes=(\d+)/(\d+); pipe-write-EAGAIN=(\d+)/(\d+)',
                    stdout,
                )
                assert counts, stdout
                partial_up, partial_down, again_up, again_down = map(int, counts.groups())
                assert partial_up > 0 and partial_down > 0, counts.groups()
                assert again_up > 0 and again_down > 0, counts.groups()
                lines = (path / 'splice.txt').read_text().splitlines()
                assert any(line.startswith('SHORT ') for line in lines), lines[:5]
                # Stronger than merely observing EAGAIN: FIONREAD confirms
                # the source socket still had unread data at that moment.
                assert any(line.startswith('PIPE_FULL_EAGAIN socket_queued=')
                           for line in lines), lines[:10]
            finally:
                stop_proxy(process)

    run_backend(backend, action)
    print('PASS: real partial splice and full-pipe EAGAIN; 1 MiB each direction intact')


def main() -> None:
    check_partial_and_pipe_full()
    print('PASS: Phase 13 partial transfers and pipe-fill EAGAIN test suite')


if __name__ == '__main__':
    main()
