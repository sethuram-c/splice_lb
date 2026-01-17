#!/usr/bin/env python3
"""Phase 33: ASan/UBSan/LSan on live epoll/splice socket sessions.

Uses the *instrumented* build/sanitize/splice-lb, not the production binary.
Sanitizer-only SIGTERM return enables LeakSanitizer's atexit report.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import tempfile
import time

from harness import (HOST, assert_bytes_equal, deterministic_bytes,
                     request, streaming_echo_backend)

ROOT = Path(__file__).resolve().parents[1]
SANITIZED_PROXY = ROOT / 'build/sanitize/splice-lb'
SANITIZED_DEMO = ROOT / 'build/sanitize/splice-demo'
BAD_REPORTS = ('ERROR: AddressSanitizer', 'ERROR: LeakSanitizer',
               'runtime error:', 'SUMMARY: UndefinedBehaviorSanitizer',
               'SUMMARY: AddressSanitizer', 'SUMMARY: LeakSanitizer')


def verify_log(log: str) -> None:
    for marker in BAD_REPORTS:
        if marker in log:
            raise AssertionError(f'sanitizer diagnostic {marker!r}:\n{log[-5000:]}')


@contextmanager
def sanitized_proxy(backend_port: int):
    env = dict(os.environ)
    env['ASAN_OPTIONS'] = ('detect_leaks=1:halt_on_error=1:abort_on_error=1:'
                           'allocator_may_return_null=0')
    env['UBSAN_OPTIONS'] = 'halt_on_error=1:print_stacktrace=1'
    with tempfile.TemporaryDirectory(prefix='phase33-sanitize-') as tmp:
        logpath = Path(tmp) / 'sanitized-proxy.log'
        with logpath.open('wb') as sink:
            proc = subprocess.Popen(
                [str(SANITIZED_PROXY), '--max-connections=128',
                 '--shutdown-grace-ms=1000', '0', f'{HOST}:{backend_port}'],
                stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                close_fds=True, env=env)
        port = None
        try:
            until = time.monotonic() + 12
            while time.monotonic() < until:
                output = logpath.read_text(errors='replace')
                match = re.search(r'^Listening on 127\.0\.0\.1:(\d+)\b', output, re.M)
                if match:
                    port = int(match.group(1))
                    break
                if proc.poll() is not None:
                    raise AssertionError(f'sanitized proxy exited during startup: {output[-3000:]}')
                time.sleep(.03)
            if port is None:
                raise AssertionError('sanitized proxy did not start')
            yield proc, port
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            output = logpath.read_text(errors='replace')
            verify_log(output)
            if proc.returncode != 0:
                raise AssertionError(f'sanitized proxy exited rc={proc.returncode}:\n{output[-5000:]}')
            if 'event=server_stopped' not in output:
                raise AssertionError('clean shutdown not observed; LeakSanitizer result is not reliable')


def run_splice_demo() -> None:
    env = dict(os.environ)
    env['ASAN_OPTIONS'] = 'detect_leaks=1:halt_on_error=1:abort_on_error=1'
    env['UBSAN_OPTIONS'] = 'halt_on_error=1:print_stacktrace=1'
    with tempfile.TemporaryDirectory(prefix='phase33-splice-') as tmp:
        original = Path(tmp) / 'in.bin'
        result = Path(tmp) / 'out.bin'
        original.write_bytes(deterministic_bytes(512 * 1024 + 39, seed=33))
        completed = subprocess.run([str(SANITIZED_DEMO), str(original), str(result)],
                                   capture_output=True, text=True, env=env, timeout=20)
        verify_log(completed.stdout + completed.stderr)
        assert completed.returncode == 0, (completed.returncode, completed.stderr)
        assert_bytes_equal(original.read_bytes(), result.read_bytes(), label='sanitize-splice')
    print('PASS: sanitizer-instrumented splice demo 512 KiB+, byte exact', flush=True)


def run_proxy() -> None:
    with streaming_echo_backend() as backend_port:
        with sanitized_proxy(backend_port) as (proc, port):
            with ThreadPoolExecutor(max_workers=8) as pool:
                cases = [deterministic_bytes(1 + i * 37191, seed=i)
                         for i in range(16)]
                futures = [pool.submit(request, port, payload, timeout=25)
                           for payload in cases]
                for expected, result in zip(cases, futures):
                    assert_bytes_equal(expected, result.result(timeout=40), label='sanitize-proxy')
            print('PASS: 16 concurrent ASan/UBSan TCP forwarding sessions, byte exact', flush=True)
            for i in range(12):
                with socket.create_connection((HOST, port), timeout=7) as sock:
                    sock.sendall(deterministic_bytes(1024, seed=i))
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                    struct.pack('ii', 1, 0))
            assert proc.poll() is None
            assert request(port, b'after injected resets', timeout=20) == b'after injected resets'
            print('PASS: 12 reset clients; sanitized proxy remains operational', flush=True)
    print('PASS: sanitizer proxy returned cleanly; ASan/UBSan/LSan reported no errors', flush=True)


if __name__ == '__main__':
    if not SANITIZED_PROXY.is_file() or not SANITIZED_DEMO.is_file():
        raise SystemExit('Run `make sanitize-build` first')
    run_splice_demo()
    run_proxy()
    print('PASS: Phase 33 ASan/UBSan/LeakSanitizer suite', flush=True)
