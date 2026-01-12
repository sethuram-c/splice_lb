#!/usr/bin/env python3
"""Phase 28: basic TCP payload matrix with exact byte-by-byte assertions."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading

from harness import (assert_bytes_equal, deterministic_bytes, echo_backend,
                     proxy_session, request)

CASES = (
    ('empty', b''),
    ('single-byte', b'Z'),
    ('utf8', 'Fedora TCP: π λ ñ'.encode('utf-8')),
    ('nul-and-ff', b'\x00\xff\x00A\x80\xff'),
    ('text', b'hello from phase 28\n'),
    ('255-bytes', deterministic_bytes(255)),
    ('256-bytes', deterministic_bytes(256)),
    ('4095-bytes', deterministic_bytes(4095)),
    ('4096-bytes', deterministic_bytes(4096)),
    ('8191-bytes', deterministic_bytes(8191)),
    ('8192-bytes', deterministic_bytes(8192)),
    ('8193-bytes', deterministic_bytes(8193)),
    ('65535-bytes', deterministic_bytes(65535)),
    ('65536-bytes', deterministic_bytes(65536)),
    ('65537-bytes', deterministic_bytes(65537)),
    ('262147-bytes', deterministic_bytes(262147)),
)


def main() -> None:
    with echo_backend() as backend_port, proxy_session(backend_port) as proxy:
        total = 0
        for label, expected in CASES:
            actual = request(proxy.port, expected,
                             fragment_sizes=(1, 7, 4093, 16001, 32768))
            assert_bytes_equal(expected, actual, label=label)
            total += len(expected)
        print(f'PASS: {len(CASES)} payload cases, {total} bytes, '
              'including empty, UTF-8, binary and buffer boundaries', flush=True)

        payloads = [deterministic_bytes((i + 1) * 3071, seed=i) for i in range(12)]

        ready = threading.Barrier(len(payloads))

        def exchange(item: tuple[int, bytes]) -> int:
            index, payload = item
            ready.wait(timeout=10)  # all 12 begin together, not merely 12 total
            reply = request(proxy.port, payload, fragment_sizes=(5, 1021, 8191))
            assert_bytes_equal(payload, reply, label=f'parallel-{index}')
            assert hashlib.sha256(reply).digest() == hashlib.sha256(payload).digest()
            return len(reply)

        with ThreadPoolExecutor(max_workers=12) as pool:
            lengths = list(pool.map(exchange, enumerate(payloads)))
        assert lengths == list(map(len, payloads))
        assert proxy.proc.poll() is None
        print(f'PASS: 12 simultaneous-client workloads, {sum(lengths)} '
              'byte-exact bytes', flush=True)
        logs = proxy.logs()
        assert 'splice accounting invariant failed' not in logs
        assert 'active connection accounting underflow' not in logs
    print('PASS: Phase 28 basic payload test suite', flush=True)


if __name__ == '__main__':
    main()
