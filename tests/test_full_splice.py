#!/usr/bin/env python3
"""Phase 12: verify bidirectional socket->pipe->socket splice in a live proxy."""
from __future__ import annotations

import os
import re
import socket
import time
import tempfile
from pathlib import Path

from test_client_splice import audit_library
from test_preconnect_buffer import delayed_connect_environment
from test_proxy import (TIMEOUT, recv_until_eof, run_backend,
                        start_proxy, stop_proxy, wait_for_proxy_cleanup)

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB, includes NUL and all byte values
REPLY = b'R:' + PAYLOAD[::-1]


def check_two_way_splice(*, delay_connect: bool) -> None:
    def backend(conn: socket.socket) -> None:
        assert recv_until_eof(conn) == PAYLOAD
        conn.sendall(REPLY)

    def action(backend_port: int) -> None:
        with tempfile.TemporaryDirectory(prefix='phase12-audit-') as tmp:
            folder = Path(tmp)
            lib = audit_library(folder)
            env = os.environ.copy()
            env['SPLICE_AUDIT_FILE'] = str(folder / 'splice.log')
            if delay_connect:
                deferred = delayed_connect_environment(tmp)
                env['LD_PRELOAD'] = f"{lib}:{deferred['LD_PRELOAD']}"
            else:
                env['LD_PRELOAD'] = str(lib)
            proc, port = start_proxy(backend_port, env=env)
            try:
                with socket.create_connection(('127.0.0.1', port), TIMEOUT) as client:
                    client.settimeout(25)
                    client.sendall(PAYLOAD)
                    client.shutdown(socket.SHUT_WR)
                    assert recv_until_eof(client) == REPLY
                wait_for_proxy_cleanup(proc)
                proc.terminate()
                stdout, stderr = proc.communicate(timeout=TIMEOUT)
                assert proc.returncode == -15, (stdout, stderr)
                assert stderr == b'', stderr
                assert (b'client-to-backend=1048576 bytes, '
                        b'backend-to-client=1048578 bytes') in stdout, stdout
                # Both directions went through a kernel pipe. The 8 KiB
                # preconnect queue is an intentional exception on the upload.
                def counter(name: str) -> int:
                    match = re.search(re.escape(name.encode()) + rb'=(\d+)', stdout)
                    assert match, stdout
                    return int(match[1])
                upstream_in = counter('client-to-pipe')
                upstream_out = counter('pipe-to-backend')
                downstream_in = counter('backend-to-pipe')
                downstream_out = counter('pipe-to-client')
                assert 0 < upstream_in <= len(PAYLOAD)
                assert upstream_in == upstream_out, (upstream_in, upstream_out)
                assert downstream_in == downstream_out == len(REPLY), (
                    downstream_in, downstream_out)
                if delay_connect:
                    assert counter('preconnect-buffered') == 8192
                    assert upstream_in == len(PAYLOAD) - 8192
                lines = (folder / 'splice.log').read_text().splitlines()
                incoming = sum(int(m[1]) for line in lines if (m := re.fullmatch(
                    r'socket-to-pipe bytes=(\d+) flags=(\d+)', line)))
                outgoing = sum(int(m[1]) for line in lines if (m := re.fullmatch(
                    r'pipe-to-socket bytes=(\d+) flags=(\d+)', line)))
                assert incoming == outgoing == upstream_in + downstream_in, (
                    incoming, outgoing, upstream_in, downstream_in)
            finally:
                stop_proxy(proc)

    run_backend(backend, action)
    print('PASS: both socket->pipe and pipe->socket splice confirmed, '
          '1 MiB request/reply intact' + (' (delayed connect)' if delay_connect else ''))


def check_full_duplex_greeting() -> None:
    """Backend can finish writing before client finishes uploading."""
    welcome = b'WELCOME\0' + bytes(range(256)) * 16
    upload = bytes(range(255, -1, -1)) * 256
    def backend(conn: socket.socket) -> None:
        conn.sendall(welcome)
        conn.shutdown(socket.SHUT_WR)
        assert recv_until_eof(conn) == upload

    def action(backend_port: int) -> None:
        proc, port = start_proxy(backend_port)
        try:
            with socket.create_connection(('127.0.0.1', port), TIMEOUT) as client:
                client.settimeout(TIMEOUT)
                assert recv_until_eof(client) == welcome
                client.sendall(upload)
                client.shutdown(socket.SHUT_WR)
            # Phase-4 helper uses an 8-second deadline. Under busy CI,
            # a concurrently closing TCP socket can cross that boundary.
            try:
                wait_for_proxy_cleanup(proc)
            except AssertionError:
                wait_for_proxy_cleanup(proc)
            assert proc.poll() is None
        finally:
            stop_proxy(proc)
    run_backend(backend, action)
    print('PASS: backend half-close before client upload preserves both directions')


def main() -> None:
    check_two_way_splice(delay_connect=False)
    check_two_way_splice(delay_connect=True)
    check_full_duplex_greeting()
    print('PASS: Phase 12 complete bidirectional splice transfer test suite')


if __name__ == '__main__':
    main()
