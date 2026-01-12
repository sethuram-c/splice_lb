#!/usr/bin/env python3
"""Phase 28: self-test the reusable harness and its negative controls."""
from harness import (assert_bytes_equal, deterministic_bytes, echo_backend,
                     proxy_session, request)


def main() -> None:
    assert deterministic_bytes(0) == b''
    sample = deterministic_bytes(131072)
    assert sample == deterministic_bytes(131072)
    assert sample != deterministic_bytes(131072, seed=7)
    assert 0 in sample and 255 in sample
    assert_bytes_equal(sample, sample, label='known good')
    print('PASS: reproducible binary generator includes NUL and 0xff', flush=True)

    for corrupted in (sample[:-1], sample[:17] + b'X' + sample[18:]):
        try:
            assert_bytes_equal(sample, corrupted, label='deliberate corruption')
        except AssertionError as exc:
            assert 'byte mismatch at offset' in str(exc)
            assert 'sha256=' in str(exc)
        else:
            raise AssertionError('harness accepted corrupted data')
    print('PASS: negative controls reject truncated and altered replies', flush=True)

    for fragments in ((), (0,), (-1, 2)):
        try:
            request(12345, b'ignored', fragment_sizes=fragments)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid fragment specification accepted')

    with echo_backend() as backend_port:
        with proxy_session(backend_port) as proxy:
            assert request(proxy.port, b'harness lifecycle') == b'harness lifecycle'
            assert proxy.proc.poll() is None
            pid = proxy.proc.pid
        assert proxy.proc.poll() is not None, 'proxy not reaped'
        print(f'PASS: process {pid} terminated and reaped on fixture exit', flush=True)
    print('PASS: Phase 28 reusable harness self-test suite', flush=True)


if __name__ == '__main__':
    main()
