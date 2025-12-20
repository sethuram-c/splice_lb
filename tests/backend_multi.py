#!/usr/bin/env python3
"""Local multi-client test backend, stdlib only. NOT the load balancer."""

from __future__ import annotations

import argparse
import socketserver


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        # Echo bytes as they arrive (unlike the one-shot C echo-backend).
        while True:
            data = self.request.recv(65536)
            if not data:
                break
            self.request.sendall(data)


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Local concurrent echo backend")
    parser.add_argument("port", nargs="?", type=int, default=9091)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    with Server(("127.0.0.1", args.port), Handler) as server:
        print(f"Concurrent echo backend listening on 127.0.0.1:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Stopping concurrent echo backend", flush=True)


if __name__ == "__main__":
    main()
