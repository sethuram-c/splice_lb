#!/usr/bin/env python3
"""Integration tests for Phase 16 connection-level Round Robin selection."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from pathlib import Path
import re
import socket
import subprocess
import sys

from rr_backends import start_backend

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "build" / "splice-lb"


def one_client(port, payload):
    with socket.create_connection(("127.0.0.1", port), timeout=4) as sock:
        sock.settimeout(15)
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        data = bytearray()
        while True:
            part = sock.recv(65536)
            if not part:
                return bytes(data)
            data.extend(part)


def test_invalid_config():
    bad = [
        ["0"],
        ["0", "127.0.0.1"],
        ["0", "127.0.0.1:0"],
        ["0", "127.0.0.1:65536"],
        ["0", "999.0.0.1:9091"],
        ["0", "127.0.0.1:notaport"],
        ["0", "127.0.0.1:9091:9092"],
        ["0", ":9091"],
        ["0", "[::1]:9091"],
        ["0"] + ["127.0.0.1:9091"] * 33,
    ]
    for args in bad:
        result = subprocess.run([str(PROXY), *args], timeout=3,
                                capture_output=True)
        assert result.returncode != 0, args
        assert b"Usage:" in result.stderr, (args, result.stderr)
    print(f"PASS: {len(bad)} invalid backend configurations rejected")


def main():
    test_invalid_config()
    servers = []
    proc = None
    try:
        for index in range(3):
            server, _ = start_backend(0, index)
            servers.append(server)
        backend_specs = [f"127.0.0.1:{server.server_address[1]}" for server in servers]
        proc = subprocess.Popen([str(PROXY), "0", *backend_specs],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                bufsize=0)
        first = proc.stdout.readline().decode()
        match = re.fullmatch(r"Listening on 127\.0\.0\.1:(\d+) -> round-robin \(3 backends\)\n", first)
        assert match, first
        port = int(match.group(1))

        # Single-client phase: the assignment sequence is deterministic.
        for i in range(12):
            payload = (bytes(range(256)) * 512) if i == 8 else f"request-{i}".encode()
            response = one_client(port, payload)
            assert response == f"backend-{i % 3}|".encode() + payload, (i, len(response))
        print("PASS: 12 sequential clients selected backends 0,1,2 in exact rotation")

        # Scheduling changes the order for parallel clients, not overall fairness.
        # Each of the next 30 connections must be assigned to one of the 3 tags,
        # and each backend must receive exactly 10.
        def concurrent_request(i):
            payload = f"parallel-{i}".encode()
            response = one_client(port, payload)
            assert response.endswith(payload), response
            prefix = response[:-len(payload)]
            assert prefix in (b"backend-0|", b"backend-1|", b"backend-2|"), prefix
            return prefix

        with ThreadPoolExecutor(max_workers=15) as pool:
            tags = list(pool.map(concurrent_request, range(30)))
        counts = Counter(tags)
        assert counts == {b"backend-0|": 10,
                          b"backend-1|": 10,
                          b"backend-2|": 10}, counts
        print("PASS: 30 concurrent TCP clients distributed 10/10/10")

        # A later client must still work; the proxy does not exit after a batch.
        assert one_client(port, b"after-test") == b"backend-0|after-test"
        print("PASS: persistent listener serves additional clients without data loss")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate(timeout=4)
            if not out:
                out = b""
            if not err:
                err = b""
            assert b"splice accounting invariant failed" not in err, err
            # 43 selections: 12 + 30 + 1. Check logs independently.
            selections = re.findall(rb"Selected backend\[(\d+)\]", out)
            assert len(selections) == 43, (len(selections), out[-1500:], err)
            assert list(map(int, selections)) == [i % 3 for i in range(43)]
            assert out.count(b"transfer-integrity=verified") == 43, out[-1500:]
            print("PASS: 43 selections logged in rotation; 43 clean integrity completions")
        for server in servers:
            server.shutdown()
            server.server_close()
    print("PASS: Phase 16 backend list and round-robin test suite")


if __name__ == "__main__":
    main()
