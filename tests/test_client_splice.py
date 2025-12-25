#!/usr/bin/env python3
"""Phase 11: verify live socket->pipe splice, integrity, and staged data."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import tempfile
from pathlib import Path

from test_proxy import (TIMEOUT, recv_until_eof, run_backend, start_proxy,
                        stop_proxy, wait_for_proxy_cleanup)
from test_preconnect_buffer import delayed_connect_environment

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = bytes(range(256)) * 2048  # 512 KiB with NUL and invalid UTF-8


def audit_library(folder: Path) -> Path:
    library = folder / 'splice_observer.so'
    subprocess.run([
        os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Wextra', '-Wpedantic',
        '-Werror', '-shared', '-fPIC',
        str(ROOT / 'tests' / 'splice_observer.c'), '-o', str(library),
    ], check=True, capture_output=True, timeout=TIMEOUT)
    return library


def exercise_splice(*, delay_connect: bool) -> None:
    def backend(conn: socket.socket) -> None:
        request = recv_until_eof(conn)
        assert request == PAYLOAD, (len(request), len(PAYLOAD))
        conn.sendall(b'ACK:' + request[::-1])

    def run(backend_port: int) -> None:
        with tempfile.TemporaryDirectory(prefix='phase11-client-pipe-') as temp:
            folder = Path(temp)
            library = audit_library(folder)
            env = os.environ.copy()
            env['SPLICE_AUDIT_FILE'] = str(folder / 'splice.log')
            if delay_connect:
                delayed = delayed_connect_environment(temp)
                env['LD_PRELOAD'] = f"{library}:{delayed['LD_PRELOAD']}"
            else:
                env['LD_PRELOAD'] = str(library)
            proc, port = start_proxy(backend_port, env=env)
            try:
                with socket.create_connection(('127.0.0.1', port), TIMEOUT) as client:
                    client.settimeout(20)
                    client.sendall(PAYLOAD)
                    client.shutdown(socket.SHUT_WR)
                    assert recv_until_eof(client) == b'ACK:' + PAYLOAD[::-1]
                wait_for_proxy_cleanup(proc)
                proc.terminate()
                stdout, stderr = proc.communicate(timeout=TIMEOUT)
                assert proc.returncode == -15, (stdout, stderr)
                assert not stderr, stderr
                match = re.search(rb'client-to-pipe=(\d+) bytes', stdout)
                assert match, stdout
                measured = int(match[1])
                assert 0 < measured <= len(PAYLOAD), (measured, len(PAYLOAD))
                lines = (folder / 'splice.log').read_text().splitlines()
                assert lines, 'proxy never called splice(socket, pipe)'
                observed = sum(int(m[1]) for line in lines
                               if (m := re.fullmatch(
                                   r'socket-to-pipe bytes=(\d+) flags=(\d+)', line)))
                assert len(lines) > 0 and observed == measured, (observed, measured)
                if delay_connect:
                    stage = re.search(rb'preconnect-buffered=(\d+) bytes', stdout)
                    assert stage and int(stage[1]) == 8192, (stage, stdout)
                    assert measured + int(stage[1]) == len(PAYLOAD), stdout
                else:
                    # At most 8192 bytes may have been staged before connect.
                    assert measured >= len(PAYLOAD) - 8192, measured
                assert (b'client-to-backend=524288 bytes, '
                        b'backend-to-client=524292 bytes') in stdout, stdout
            finally:
                stop_proxy(proc)

    run_backend(backend, run)
    print('PASS: socket-to-pipe splice verified via syscall interposer, '
          '512 KiB request/response intact' +
          (' (delayed backend)' if delay_connect else ''))


def check_empty_half_close() -> None:
    def backend(conn: socket.socket) -> None:
        assert recv_until_eof(conn) == b''
        conn.sendall(b'EMPTY')

    def run(port: int) -> None:
        proc, listener_port = start_proxy(port)
        try:
            with socket.create_connection(('127.0.0.1', listener_port), TIMEOUT) as client:
                client.settimeout(TIMEOUT)
                client.shutdown(socket.SHUT_WR)
                assert recv_until_eof(client) == b'EMPTY'
            wait_for_proxy_cleanup(proc)
            assert proc.poll() is None
        finally:
            stop_proxy(proc)

    run_backend(backend, run)
    print('PASS: empty request propagates EOF; backend reply survives half-close')


def main() -> None:
    exercise_splice(delay_connect=False)
    exercise_splice(delay_connect=True)
    check_empty_half_close()
    print('PASS: Phase 11 direction state and client-to-pipe splice test suite')


if __name__ == '__main__':
    main()
