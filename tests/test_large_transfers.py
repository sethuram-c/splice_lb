#!/usr/bin/env python3
"""Phase 29: streamed multi-megabyte TCP integrity and concurrency tests.

Neither the test client nor its backend needs to store an entire payload.
SHA-256 digests and exact byte counts detect truncation, duplication,
reordering and corruption. Timings are diagnostics, not benchmark claims.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import random
import socket
import threading
import time

from harness import HOST, proxy_session, streaming_echo_backend

MIB = 1024 * 1024


def proc_fds(pid: int) -> int:
    return len(os.listdir(f'/proc/{pid}/fd'))


def transfer(port: int, size: int, seed: int, fragments: tuple[int, ...],
             timeout: float = 90.0) -> tuple[int, float, str]:
    """Send and receive concurrently; compare exact byte length + SHA-256."""
    if size < 0 or not fragments or min(fragments) < 1:
        raise ValueError('invalid transfer parameters')
    tx_digest = hashlib.sha256()
    rx_digest = hashlib.sha256()
    errors: list[BaseException] = []
    results = {'sent': 0}
    started = time.monotonic()
    with socket.create_connection((HOST, port), timeout=timeout) as client:
        client.settimeout(timeout)

        def upload() -> None:
            rng = random.Random(seed)
            left = size
            fragment = 0
            try:
                while left:
                    count = min(left, fragments[fragment % len(fragments)])
                    payload = rng.randbytes(count)
                    client.sendall(payload)
                    tx_digest.update(payload)
                    results['sent'] += count
                    left -= count
                    fragment += 1
                client.shutdown(socket.SHUT_WR)
            except BaseException as error:
                errors.append(error)
                # Prevent the receiving side from waiting forever on failed writes.
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        writer = threading.Thread(target=upload, name='large-tcp-writer', daemon=True)
        writer.start()
        received = 0
        receive_error: BaseException | None = None
        try:
            while True:
                block = client.recv(131072)
                if not block:
                    break
                received += len(block)
                if received > size:
                    raise AssertionError(f'oversized echo: {received} > {size}')
                rx_digest.update(block)
        except BaseException as error:
            receive_error = error
        finally:
            # close() interrupts a stalled writer if the read path has failed.
            if receive_error is not None:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            writer.join(timeout=timeout)
        if writer.is_alive():
            raise AssertionError('upload thread stuck after reader finished')
        if errors:
            raise AssertionError(f'upload failed: {errors[0]}') from errors[0]
        if receive_error is not None:
            raise AssertionError(f'receive failed: {receive_error}') from receive_error
    tx = tx_digest.hexdigest()
    rx = rx_digest.hexdigest()
    if (results['sent'], received, rx) != (size, size, tx):
        raise AssertionError(f'transfer mismatch sent={results["sent"]} '
                             f'received={received} expected={size} '
                             f'tx-sha256={tx} rx-sha256={rx}')
    return received, time.monotonic() - started, rx


def wait_for_fd_baseline(pid: int, baseline: int, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc_fds(pid) == baseline:
            return
        time.sleep(.05)
    raise AssertionError(f'open fd leak: baseline={baseline} now={proc_fds(pid)}')


def main() -> None:
    with streaming_echo_backend() as backend, proxy_session(backend) as proxy:
        baseline = proc_fds(proxy.proc.pid)
        size = 16 * MIB + 17
        received, seconds, digest = transfer(
            proxy.port, size, 2901, (1, 113, 4093, 65536, 8191))
        assert received == size
        wait_for_fd_baseline(proxy.proc.pid, baseline)
        print(f'PASS: fragmented {size} B large transfer, SHA-256={digest[:16]}, '
              f'elapsed={seconds:.2f}s', flush=True)

        size = 32 * MIB + 1
        received, seconds, digest = transfer(
            proxy.port, size, 2902, (131072, 65539, 32767))
        assert received == size
        wait_for_fd_baseline(proxy.proc.pid, baseline)
        print(f'PASS: streamed {size} B single session, SHA-256={digest[:16]}, '
              f'elapsed={seconds:.2f}s', flush=True)

        sizes = tuple(8 * MIB + offset for offset in (0, 1, 31, 4097))
        def concurrent(item: tuple[int, int]) -> int:
            index, count = item
            transferred, _, _ = transfer(proxy.port, count, 3000 + index,
                                         (4093, 65536, 32769))
            return transferred
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(concurrent, enumerate(sizes)))
        assert results == list(sizes)
        wait_for_fd_baseline(proxy.proc.pid, baseline)
        print(f'PASS: 4 concurrent streaming sessions, {sum(results)} B '
              f'each direction, exact SHA-256 and fd cleanup', flush=True)
        assert proxy.proc.poll() is None, 'proxy exited unexpectedly'
        log = proxy.logs()
        assert 'splice accounting invariant failed' not in log
        assert 'active connection accounting underflow' not in log
    print('PASS: Phase 29 large transfer and byte-integrity test suite', flush=True)


if __name__ == '__main__':
    main()
