#!/usr/bin/env python3
"""Phase 21: distinguish observed client EOF from forwarded backend FIN.

No external services or root privileges; all ports are ephemeral loopback.
"""
from __future__ import annotations

import socket
import time
import tempfile

from test_proxy import (TIMEOUT, recv_until_eof, run_backend, start_proxy,
                        stop_proxy, wait_for_proxy_cleanup)
from test_preconnect_buffer import delayed_connect_environment


def exercise(payload: bytes, *, delay_connect: bool, pause_backend: bool) -> None:
    response = b'CLIENT-FIN-ACK:' + bytes(range(256)) * 2

    def backend(conn: socket.socket) -> None:
        conn.settimeout(35)
        if pause_backend:
            time.sleep(.20)  # force the sender to encounter backpressure
        assert recv_until_eof(conn) == payload, 'backend lost upload before FIN'
        conn.sendall(response)

    def client_work(backend_port: int) -> None:
        with tempfile.TemporaryDirectory(prefix='phase21-') as tmp:
            env = delayed_connect_environment(tmp) if delay_connect else None
            proc, port = start_proxy(backend_port, env=env)
            try:
                with socket.create_connection(('127.0.0.1', port), TIMEOUT) as client:
                    client.settimeout(35)
                    if payload:
                        client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                    assert recv_until_eof(client) == response
                wait_for_proxy_cleanup(proc)
                assert proc.poll() is None, 'listener stopped after client FIN'
                proc.terminate()
                out, err = proc.communicate(timeout=TIMEOUT)
                assert proc.returncode == -15 and not err, (out, err)
                assert out.count(b'client-eof-state=observed;') == 1, out
                assert out.count(b'client-eof-state=forwarded;') == 1, out
                assert b'client-eof-final=forwarded;' in out, out
                assert b'transfer-integrity=verified' in out, out
                assert f'client-to-backend={len(payload)} bytes'.encode() in out, out
                assert b'pending=0; pipe-pending=0\n' in out, out
                if delay_connect and not payload:
                    # no preconnect bytes can hide EOF; FIN must await TCP handshake
                    assert b'client-eof-state=observed; deferred=yes;' in out, out
                    assert b'backend=connecting' in out, out
                    assert b'client-fin-deferred=yes;' in out, out
            finally:
                stop_proxy(proc)

    run_backend(backend, client_work)
    print('PASS: client FIN deferred safely (' +
          ('delayed-connect' if delay_connect else 'normal') +
          f', payload={len(payload)}, slow-backend={pause_backend})')


def main() -> None:
    # Immediate EOF during an unfinished connect must NOT call shutdown on an
    # unconnected backend. The zero-length case proves the state transition.
    exercise(b'', delay_connect=True, pause_backend=False)
    # Both paths must forward all queued bytes before forwarding FIN.
    exercise(bytes(range(256)) * 32, delay_connect=True, pause_backend=False)
    exercise(bytes(range(256)) * 16384, delay_connect=False, pause_backend=True)
    # Server continues accepting after the test connection has completed.
    print('PASS: Phase 21 client EOF and deferred FIN lifecycle test suite')


if __name__ == '__main__':
    main()
