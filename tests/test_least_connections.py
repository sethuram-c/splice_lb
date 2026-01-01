#!/usr/bin/env python3
"""Phase 17: prove least-active assignment with held-open TCP sessions.

Uses real tagged backends.  A held-open socket counts as active even when it
has not sent a request; returned tag identifies the selected backend.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import socket
import subprocess
import threading
import time

from rr_backends import start_backend

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "build" / "splice-lb"


def wait_until(check, timeout=6):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.012)
    raise AssertionError("Timed out waiting for proxy accounting/event log")


class Proxy:
    def __init__(self, addresses, algorithm="least-connections"):
        self.proc = subprocess.Popen(
            [str(PROXY), "--algorithm", algorithm, "0", *addresses],
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

    def _read(self):
        for line in self.proc.stdout:
            with self._lock:
                self.lines.append(line.decode(errors="replace").strip())

    def snapshot(self):
        with self._lock:
            return list(self.lines)

    def releases(self):
        return sum(line.startswith("Released backend[") for line in self.snapshot())

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=4)
        self._thread.join(timeout=3)
        assert b"accounting underflow" not in self.proc.stderr.read(), self.snapshot()[-10:]


def open_held(port):
    sock = socket.create_connection(("127.0.0.1", port), timeout=4)
    sock.settimeout(7)
    tag = b""
    while not tag.endswith(b"|"):
        chunk = sock.recv(32)
        if not chunk:
            raise AssertionError("Backend closed before sending identifier")
        tag += chunk
    match = re.fullmatch(rb"backend-(\d+)\|", tag)
    assert match, tag
    return sock, int(match.group(1))


def finish(sock):
    try:
        sock.shutdown(socket.SHUT_WR)
        while sock.recv(65536):
            pass
    finally:
        sock.close()


def main():
    invalid = [
        ["--algorithm", "random", "0", "127.0.0.1:9091"],
        ["--algorithm", "least-connections"],
        ["--algorithm", "least-connections", "0"],
        ["--algorithm", "least-connections", "0", "bad"],
        ["--algorithm", "round-robin", "0", "127.0.0.1:0"],
    ]
    for args in invalid:
        result = subprocess.run([str(PROXY), *args], capture_output=True, timeout=3)
        assert result.returncode != 0 and b"Usage:" in result.stderr, args
    print(f"PASS: {len(invalid)} invalid selection configurations rejected", flush=True)

    backends = []
    proxy = None
    held = []
    try:
        for index in range(3):
            backend, _ = start_backend(0, index)
            backends.append(backend)
        addrs = [f"127.0.0.1:{server.server_address[1]}" for server in backends]
        proxy = Proxy(addrs)
        for expected in (0, 1, 2, 0, 1, 2):
            client = open_held(proxy.port)
            assert client[1] == expected, (expected, client[1])
            held.append(client)
        print("PASS: 6 held-open sessions start balanced at 2/2/2", flush=True)

        finished = proxy.releases()
        finish(held[0][0])
        held[0] = (None, 0)
        wait_until(lambda: proxy.releases() > finished)
        added = open_held(proxy.port)
        assert added[1] == 0, added[1]
        held.append(added)

        finished = proxy.releases()
        finish(held[1][0])
        held[1] = (None, 1)
        wait_until(lambda: proxy.releases() > finished)
        added = open_held(proxy.port)
        assert added[1] == 1, added[1]
        held.append(added)
        print("PASS: freeing backend 0 then 1 routes new clients to the least loaded backend", flush=True)

        for client, _ in held:
            if client is not None:
                finish(client)
        held.clear()
        wait_until(lambda: proxy.releases() >= 8)
        assert any("backend-active=0; total-active=0" in line
                   for line in proxy.snapshot()), proxy.snapshot()[-10:]
        print("PASS: 8 clean teardowns decrement active counters exactly once", flush=True)

        with ThreadPoolExecutor(max_workers=21) as pool:
            held = list(pool.map(lambda _: open_held(proxy.port), range(21)))
        counts = Counter(index for _, index in held)
        assert counts == {0: 7, 1: 7, 2: 7}, counts
        print("PASS: 21 concurrently opened, held sessions balance 7/7/7", flush=True)

        for client, _ in held:
            finish(client)
        held.clear()
        wait_until(lambda: proxy.releases() >= 29)
        selected = [line for line in proxy.snapshot() if line.startswith("Selected backend[")]
        assert len(selected) == 29, len(selected)
        assert all("algorithm=least-connections" in line for line in selected)
        assert sum("transfer-integrity=verified" in line for line in proxy.snapshot()) == 29
        print("PASS: 29 selections and full-duplex completions audited", flush=True)
    finally:
        for client, _ in held:
            if client is not None:
                client.close()
        if proxy is not None:
            proxy.close()
        for backend in backends:
            backend.shutdown()
            backend.server_close()
    # A refused nonblocking backend connect must not leave the per-target
    # active counter inflated; the next accepted client still works.
    healthy, _ = start_backend(0, 1)
    refusal_proxy = None
    try:
        with socket.socket() as temporary:
            temporary.bind(("127.0.0.1", 0))
            refused_port = temporary.getsockname()[1]
        specs = [f"127.0.0.1:{refused_port}",
                 f"127.0.0.1:{healthy.server_address[1]}"]
        refusal_proxy = Proxy(specs)
        with socket.create_connection(("127.0.0.1", refusal_proxy.port), timeout=4) as client:
            client.settimeout(4)
            try:
                assert client.recv(1) == b""
            except ConnectionResetError:
                pass
        wait_until(lambda: refusal_proxy.releases() == 1)
        assert any("Released backend[0]=" in line and "backend-active=0" in line
                   for line in refusal_proxy.snapshot())
        working, selected = open_held(refusal_proxy.port)
        assert selected == 1, selected
        finish(working)
        wait_until(lambda: refusal_proxy.releases() == 2)
        assert any("Released backend[1]=" in line and "backend-active=0" in line
                   for line in refusal_proxy.snapshot())
        print("PASS: refused backend connect releases its active slot; healthy peer remains available", flush=True)
    finally:
        if refusal_proxy is not None:
            refusal_proxy.close()
        healthy.shutdown()
        healthy.server_close()
    print("PASS: Phase 17 Least Connections active-load test suite", flush=True)


if __name__ == "__main__":
    main()
