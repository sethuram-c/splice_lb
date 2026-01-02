#!/usr/bin/env python3
"""Phase 18: deterministic RR-vs-LC comparison under unequal lifetimes.

Uses actual TCP sessions and tagged echo backends, not a simulated picker.
Six initially held-open connections establish a 2/2/2 baseline; ending
backend-2's two sessions creates 2/2/0. Three further held-open probes
show how each algorithm responds to the identical load imbalance.
"""
import argparse
from collections import Counter
import csv
from pathlib import Path
import re
import socket
import subprocess

from rr_backends import start_backend
from test_least_connections import Proxy, finish, open_held, wait_until

PROXY_BIN = Path(__file__).resolve().parents[1] / "build" / "splice-lb"


def check_cli():
    help_result = subprocess.run([str(PROXY_BIN), "--help"], capture_output=True,
                                 text=True, timeout=3)
    assert help_result.returncode == 0 and "least-connections" in help_result.stdout
    invalid = [
        ["--algorithm="], ["--algorithm=other", "0", "127.0.0.1:9091"],
        ["--algorithm", "", "0", "127.0.0.1:9091"],
        ["--algorithm", "round-robin", "--algorithm=least-connections", "0", "127.0.0.1:9091"],
        ["--algorithm=round-robin", "--algorithm=least-connections", "0", "127.0.0.1:9091"],
        ["--algorithm=least-connections", "0", "invalid:9091"],
        ["--algorithm=round-robin", "0"],
    ]
    for args in invalid:
        result = subprocess.run([str(PROXY_BIN), *args], capture_output=True,
                                text=True, timeout=3)
        assert result.returncode != 0 and "Usage:" in result.stderr, (args, result)
    print(f"PASS: --help and {len(invalid)} invalid configuration cases", flush=True)


def connect_and_identify(port, marker):
    """Hold the connection open after verifying actual bidirectional bytes."""
    client, index = open_held(port)
    client.sendall(marker)
    echoed = b""
    while len(echoed) < len(marker):
        chunk = client.recv(len(marker) - len(echoed))
        assert chunk, "Backend unexpectedly returned EOF"
        echoed += chunk
    assert echoed == marker, (marker, echoed)
    return client, index


def run_scenario(addresses, alg, style):
    # Reuse the proven event-log collector from Phase 17, with an isolated
    # fresh process per scenario so selection cursors cannot contaminate runs.
    proxy = None
    held = []
    probes = []
    selections = []
    try:
        # Use both supported selector syntaxes in focused tests; explicitly
        # check --algorithm=... with a real server rather than only help text.
        if style == "equals":
            proxy = ComparisonProxy(addresses, alg)
        else:
            proxy = Proxy(addresses, algorithm=alg)
        for i in range(6):
            sock, index = connect_and_identify(proxy.port, f"seed-{i}".encode())
            held.append((sock, index))
        assert Counter(index for _, index in held) == {0: 2, 1: 2, 2: 2}
        target = [(sock, index) for sock, index in held if index == 2]
        previous = proxy.releases()
        for sock, _ in target:
            finish(sock)
        held = [(sock, index) for sock, index in held if index != 2]
        wait_until(lambda: proxy.releases() == previous + 2)
        for i in range(3):
            sock, index = connect_and_identify(proxy.port, f"probe-{i}".encode())
            probes.append((sock, index))
            selections.append(index)
        expected = [0, 1, 2] if alg == "round-robin" else [2, 2, 0]
        assert selections == expected, (alg, selections, expected)
        total_active = Counter(index for _, index in held + probes)
        assert sum(total_active.values()) == 7
        assert sorted(total_active.values()) == ([1, 3, 3] if alg == "round-robin" else [2, 2, 3])
        for sock, _ in held + probes:
            finish(sock)
        held.clear()
        probes.clear()
        wait_until(lambda: proxy.releases() == 9)
        lines = proxy.snapshot()
        assert sum("transfer-integrity=verified" in line for line in lines) == 9
        selected = [line for line in lines if line.startswith("Selected backend[")]
        assert len(selected) == 9 and all(f"algorithm={alg}" in line for line in selected)
        assert any("backend-active=0; total-active=0" in line for line in lines)
        print(f"PASS: {alg}: probes={selections}; active={dict(sorted(total_active.items()))}; "
              f"9 sessions byte-exact", flush=True)
        return selections, total_active
    finally:
        for sock, _ in held + probes:
            sock.close()
        if proxy is not None:
            proxy.close()


class ComparisonProxy(Proxy):
    """Phase 17 logging collector, but with the --algorithm=NAME CLI form."""
    def __init__(self, addresses, algorithm):
        import threading
        self.proc = subprocess.Popen(
            [str(PROXY_BIN), f"--algorithm={algorithm}", "0", *addresses],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.lines = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        wait_until(lambda: any("Listening on" in s for s in self.snapshot()))
        first = self.snapshot()[0]
        match = re.search(r"Listening on 127\.0\.0\.1:(\d+) ->", first)
        assert match, first
        self.port = int(match.group(1))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", type=Path, help="write per-probe choices to an external CSV file")
    args = ap.parse_args()
    check_cli()
    backends = []
    try:
        for index in range(3):
            srv, _ = start_backend(0, index)
            backends.append(srv)
        addresses = [f"127.0.0.1:{srv.server_address[1]}" for srv in backends]
        rr, rr_active = run_scenario(addresses, "round-robin", "space")
        lc, lc_active = run_scenario(addresses, "least-connections", "equals")
        assert max(lc_active.values()) - min(lc_active.values()) < \
               max(rr_active.values()) - min(rr_active.values())
        print("PASS: Least Connections improves active-load spread over Round Robin under skew", flush=True)
        if args.csv is not None:
            with args.csv.open("w", newline="", encoding="utf-8") as out:
                writer = csv.writer(out)
                writer.writerow(["algorithm", "probe", "selected_backend"])
                for alg, seq in (("round-robin", rr), ("least-connections", lc)):
                    for probe, backend in enumerate(seq):
                        writer.writerow([alg, probe, backend])
            print(f"Wrote CSV: {args.csv}", flush=True)
    finally:
        for srv in backends:
            srv.shutdown()
            srv.server_close()
    print("PASS: Phase 18 configuration and algorithm comparison test suite", flush=True)


if __name__ == "__main__":
    main()
