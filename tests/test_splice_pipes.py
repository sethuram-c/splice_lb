#!/usr/bin/env python3
"""Phase 10: standalone splice experiment and two inactive pipes per TCP session."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from test_multiclient import EchoHandler, ThreadedBackend
from test_proxy import TIMEOUT, recv_until_eof, start_proxy, stop_proxy
import threading

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / 'build' / 'splice-demo'


def check_splice_demo() -> None:
    lengths = [0, 1, 4095, 65536, 65537, 1048576, 2 * 1048576 + 9]
    with tempfile.TemporaryDirectory(prefix='phase10-splice-') as name:
        temp = Path(name)
        source, result = temp / 'source.bin', temp / 'result.bin'
        for size in lengths:
            data = (bytes(range(256)) * ((size + 255) // 256))[:size]
            source.write_bytes(data)
            result.write_bytes(b'old-content' * 17)
            process = subprocess.run([str(DEMO), str(source), str(result)],
                                     text=True, capture_output=True, timeout=TIMEOUT, check=True)
            assert process.stdout.strip() == f'splice-demo: transferred {size} bytes', process.stdout
            assert result.read_bytes() == data, f'{size}-byte splice stream corrupted'

        before = source.read_bytes()
        bad = subprocess.run([str(DEMO), str(source), str(source)],
                             text=True, capture_output=True, timeout=TIMEOUT)
        assert bad.returncode != 0 and source.read_bytes() == before
        alias = temp / 'alias.bin'
        os.link(source, alias)
        bad = subprocess.run([str(DEMO), str(source), str(alias)],
                             text=True, capture_output=True, timeout=TIMEOUT)
        assert bad.returncode != 0 and source.read_bytes() == before
        missing = subprocess.run([str(DEMO), str(temp / 'missing.bin'), str(result)],
                                 capture_output=True, timeout=TIMEOUT)
        assert missing.returncode != 0
    print('PASS: splice file->pipe->file: 0 B to 2 MiB binary, same-file safety and errors')


def pipe_table(pid: int) -> dict[str, list[int]]:
    """Return inode -> list[fd] for kernel pipes belonging to the proxy."""
    pipes: dict[str, list[int]] = {}
    for entry in (Path('/proc') / str(pid) / 'fd').iterdir():
        if int(entry.name) < 3:  # subprocess stdout/stderr are also pipes
            continue
        try:
            link = os.readlink(entry)
        except FileNotFoundError:
            continue
        if re.fullmatch(r'pipe:\[\d+\]', link):
            pipes.setdefault(link, []).append(int(entry.name))
    return pipes


def wait_for_pipes(pid: int, total_fds: int) -> dict[str, list[int]]:
    end = time.monotonic() + TIMEOUT
    while time.monotonic() < end:
        pipes = pipe_table(pid)
        if sum(map(len, pipes.values())) == total_fds:
            return pipes
        time.sleep(0.01)
    raise AssertionError(f'expected {total_fds} pipe fds, found {pipes}')


def check_connection_pipe_lifecycle() -> None:
    backend = ThreadedBackend(('127.0.0.1', 0), EchoHandler)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    proc, port = start_proxy(backend.server_address[1])
    try:
        assert not pipe_table(proc.pid), 'listener must not allocate data pipes'
        with socket.create_connection(('127.0.0.1', port), TIMEOUT) as first, \
             socket.create_connection(('127.0.0.1', port), TIMEOUT) as second:
            first.settimeout(TIMEOUT)
            second.settimeout(TIMEOUT)
            pipes = wait_for_pipes(proc.pid, 8)  # 2 directions * 2 ends * 2 sessions
            assert len(pipes) == 4, pipes
            assert all(len(fds) == 2 for fds in pipes.values()), pipes
            for fds in pipes.values():
                for fd in fds:
                    info = (Path('/proc') / str(proc.pid) / 'fdinfo' / str(fd)).read_text()
                    match = re.search(r'^flags:\s*([0-7]+)$', info, re.MULTILINE)
                    assert match, info
                    flags = int(match.group(1), 8)
                    assert flags & os.O_NONBLOCK, (fd, info)
                    assert flags & os.O_CLOEXEC, (fd, info)
            for client, payload in ((first, b'one\x00'), (second, b'two\xff' * 4000)):
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
                assert recv_until_eof(client) == b'ACK:' + payload[::-1]
        assert not wait_for_pipes(proc.pid, 0), 'pipe fds leaked after session close'
        assert proc.poll() is None, 'listener must survive per-connection cleanup'
    finally:
        stop_proxy(proc)
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=TIMEOUT)
    print('PASS: 2 sessions use 4 nonblocking CLOEXEC pipe pairs; no fds leak')


def main() -> None:
    check_splice_demo()
    check_connection_pipe_lifecycle()
    print('PASS: Phase 10 splice experiment and per-connection pipes test suite')


if __name__ == '__main__':
    main()
