#!/usr/bin/env python3
"""Phase 19: real TCP refusal, cooldown, healthy fallback, and recovery tests."""
import re
import socket
import time

from rr_backends import start_backend
from test_least_connections import Proxy, wait_until
from test_round_robin import one_client


def unused_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def refused_client(port):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        sock.settimeout(5)
        try:
            assert sock.recv(1) == b"", "Expected TCP close after refusal"
        except ConnectionResetError:
            pass


def one_algorithm(algorithm):
    failed_port = unused_port()
    backends = []
    proxy = None
    try:
        for i in (1, 2):
            backend, _ = start_backend(0, i)
            backends.append(backend)
        addresses = ([f"127.0.0.1:{failed_port}"] +
                     [f"127.0.0.1:{b.server_address[1]}" for b in backends])
        proxy = Proxy(addresses, algorithm=algorithm)

        # Before the failure is discovered, backend 0 is selectable.
        refused_client(proxy.port)
        wait_until(lambda: proxy.releases() == 1)
        assert any("Released backend[0]=" in line and "backend-active=0" in line
                   for line in proxy.snapshot())

        # Sub-3-second follow-ups must NEVER attempt backend 0 again.
        for i in range(10):
            payload = bytes([i, 0, 255]) * (100 if i % 2 else 3000)
            answer = one_client(proxy.port, payload)
            assert answer in (b"backend-1|" + payload,
                              b"backend-2|" + payload), answer[:32]
        wait_until(lambda: proxy.releases() == 11)
        selected = [re.search(r"Selected backend\[(\d+)\]", line).group(1)
                    for line in proxy.snapshot() if line.startswith("Selected backend[")]
        assert selected[0] == "0" and "0" not in selected[1:], selected
        assert any("backend-active=0; total-active=0" in line
                   for line in proxy.snapshot())
        assert sum("transfer-integrity=verified" in line for line in proxy.snapshot()) == 10
        assert proxy.proc.poll() is None
        print(f"PASS: {algorithm} quarantines refused target; 10 byte-exact healthy forwards", flush=True)
    finally:
        if proxy is not None:
            # stdout is collected by Proxy; error logs include health transitions.
            proxy.close()
        for b in backends:
            b.shutdown()
            b.server_close()


def all_down_and_recover():
    backend_port = unused_port()
    proxy = None
    recovered = None
    try:
        proxy = Proxy([f"127.0.0.1:{backend_port}"], algorithm="round-robin")
        refused_client(proxy.port)
        wait_until(lambda: proxy.releases() == 1)
        # When ALL backends are cooling down, new clients are rejected safely.
        for _ in range(3):
            refused_client(proxy.port)
        assert proxy.proc.poll() is None
        wait_until(lambda: sum("Released backend[" in l for l in proxy.snapshot()) == 1)
        print("PASS: all-down pool rejects new clients without crashing or leaking slots", flush=True)

        # Start the same target, allow cooldown to expire, then traffic reprobes.
        recovered, _ = start_backend(backend_port, 0)
        time.sleep(3.2)
        payload = bytes(range(256)) * 256
        assert one_client(proxy.port, payload) == b"backend-0|" + payload
        wait_until(lambda: proxy.releases() == 2)
        assert any("recovered after successful TCP handshake" in l
                   for l in proxy.snapshot())
        assert one_client(proxy.port, b"more-data") == b"backend-0|more-data"
        wait_until(lambda: proxy.releases() == 3)
        assert any("backend-active=0; total-active=0" in l
                   for l in proxy.snapshot())
        print("PASS: recovered backend is re-probed after cooldown and reused", flush=True)
    finally:
        if proxy is not None:
            proxy.close()
        if recovered is not None:
            recovered.shutdown()
            recovered.server_close()


def main():
    for algorithm in ("round-robin", "least-connections"):
        one_algorithm(algorithm)
    all_down_and_recover()
    print("PASS: Phase 19 failed backend handling test suite", flush=True)


if __name__ == "__main__":
    main()
