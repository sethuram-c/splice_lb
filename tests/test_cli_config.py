#!/usr/bin/env python3
"""Phase 25: strict CLI validation, address binding and live option behavior."""
from pathlib import Path
import re
import socket
import subprocess
import time

from rr_backends import start_backend

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / 'build' / 'splice-lb'


def run_bad(args):
    result = subprocess.run([str(PROXY), *args], text=True, capture_output=True,
                            timeout=4)
    assert result.returncode != 0 and 'Usage:' in result.stderr, (args, result)


def test_invalid():
    basic = ['0', '127.0.0.1:9091']
    cases = [
        ['--listen-ip', 'bad', *basic],
        ['--listen-ip=', *basic],
        ['--listen-ip', '::1', *basic],
        ['--listen-ip', '127.0.0.1', '--listen-ip=0.0.0.0', *basic],
        ['--backlog', '0', *basic],
        ['--backlog', '4097', *basic],
        ['--backlog', '-1', *basic],
        ['--backlog=+1', *basic],
        ['--backlog', '1.0', *basic],
        ['--backlog', '999999999999999999999999999', *basic],
        ['--backlog', '5', '--backlog=8', *basic],
        ['--backend-retry-ms=0', *basic],
        ['--backend-retry-ms=99', *basic],
        ['--backend-retry-ms', '60001', *basic],
        ['--backend-retry-ms', '1ms', *basic],
        ['--backend-retry-ms', '', *basic],
        ['--backend-retry-ms', '200', '--backend-retry-ms=300', *basic],
        ['--algorithm', 'other', *basic],
        ['--algorithm', 'round-robin', '--algorithm=least-connections', *basic],
        ['--unknown', '1', *basic],
        [*basic, '--backlog', '25'],
        [*basic, '127.0.0.1:9091'],
        ['--listen-ip=0.0.0.0', '9091', '127.0.0.1:9091'],
        ['--listen-ip=127.0.0.1', '9091', '127.0.0.1:9091'],
        ['--help', *basic],
        ['--listen-ip'],
        ['--backlog'],
        ['--backend-retry-ms'],
        ['--algorithm'],
    ]
    for case in cases:
        run_bad(case)
    help_result = subprocess.run([str(PROXY), '--help'], capture_output=True,
                                 text=True, timeout=3)
    assert help_result.returncode == 0
    assert all(k in help_result.stdout for k in ('--listen-ip', '--backlog',
                                                 '--backend-retry-ms', '--algorithm'))
    print(f'PASS: --help and {len(cases)} invalid configuration cases', flush=True)


def wait_listening(proc, address):
    line = proc.stdout.readline().decode()
    match = re.fullmatch(rf'Listening on {re.escape(address)}:(\d+) -> .*\n', line)
    assert match, (line, proc.poll())
    port = int(match.group(1))
    assert port > 0
    line = proc.stdout.readline().decode()
    assert line.startswith('Configuration '), line
    return port, line


def exchange(address, port, payload):
    with socket.create_connection((address, port), timeout=3) as sock:
        sock.settimeout(6)
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        buf = bytearray()
        while True:
            data = sock.recv(65536)
            if not data:
                return bytes(buf)
            buf.extend(data)


def stop(proc):
    proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=4)
    assert b'splice accounting invariant failed' not in stderr, stderr
    return stdout + stderr


def test_binding_and_compatibility():
    server, _ = start_backend(0, 0)
    target = f'127.0.0.1:{server.server_address[1]}'
    try:
        cases = [
            (['--listen-ip=127.0.0.2', '--backlog=16',
              '--backend-retry-ms=1200', '--algorithm=least-connections',
              '0', target], '127.0.0.2', '16', '1200', 'least-connections'),
            (['--algorithm', 'round-robin', '--backlog', '32',
              '--listen-ip', '127.0.0.1', '--backend-retry-ms', '700',
              '0', target], '127.0.0.1', '32', '700', 'round-robin'),
            (['0', '127.0.0.1', str(server.server_address[1])],
             '127.0.0.1', '128', '3000', 'round-robin'),
        ]
        for args, addr, backlog, retry, alg in cases:
            proc = subprocess.Popen([str(PROXY), *args], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, bufsize=0)
            try:
                port, line = wait_listening(proc, addr)
                assert f'backlog={backlog}' in line, line
                assert f'backend-retry-ms={retry}' in line, line
                assert f'algorithm={alg}' in line, line
                if addr == '127.0.0.2':
                    with socket.socket() as foreign:
                        foreign.settimeout(0.2)
                        assert foreign.connect_ex(('127.0.0.1', port)) != 0
                payload = b'\x00\xffphase25' + bytes(range(256)) * 256
                assert exchange(addr, port, payload) == b'backend-0|' + payload
                assert proc.poll() is None
                print(f'PASS: bind={addr} backlog={backlog} retry={retry} '
                      f'algorithm={alg}; binary payload intact', flush=True)
            finally:
                stop(proc)
    finally:
        server.shutdown()
        server.server_close()


def test_retry_duration():
    # A real refused TCP connect followed by recovery, using a SHORT custom
    # cooldown: proves that the configured value affects selection behavior.
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    backend = None
    proc = subprocess.Popen([str(PROXY), '--backend-retry-ms=200', '0',
                             f'127.0.0.1:{port}'], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)
    try:
        listening_port, config = wait_listening(proc, '127.0.0.1')
        assert 'backend-retry-ms=200' in config
        with socket.create_connection(('127.0.0.1', listening_port), timeout=3) as client:
            client.settimeout(3)
            try:
                assert client.recv(1) == b''
            except ConnectionResetError:
                pass
        # The failed target should become selectable again after 200ms.
        backend, _ = start_backend(port, 0)
        time.sleep(0.35)
        assert exchange('127.0.0.1', listening_port, b'recovered') == b'backend-0|recovered'
        print('PASS: configured 200 ms failure cooldown permits recovered backend', flush=True)
    finally:
        stop(proc)
        if backend is not None:
            backend.shutdown()
            backend.server_close()


def main():
    test_invalid()
    test_binding_and_compatibility()
    test_retry_duration()
    print('PASS: Phase 25 command-line configuration and validation test suite', flush=True)


if __name__ == '__main__':
    main()
