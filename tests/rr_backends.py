#!/usr/bin/env python3
"""Identifiable, concurrent TCP echo backends for Phase 16.

Start manually: python3 tests/rr_backends.py 9091 9092 9093
Each server sends a single ASCII 'backend-N|' prefix before echoing bytes.
"""
import socket
import socketserver
import sys
import threading


class TaggedHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(15)
        try:
            self.request.sendall(self.server.tag)
            while True:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                self.request.sendall(chunk)
            self.request.shutdown(socket.SHUT_WR)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class TaggedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port, tag):
        super().__init__(("127.0.0.1", port), TaggedHandler)
        self.tag = tag


def start_backend(port, index):
    server = TaggedServer(port, f"backend-{index}|".encode("ascii"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python3 tests/rr_backends.py <port> [port ...]")
    servers = []
    try:
        for index, raw in enumerate(sys.argv[1:]):
            port = int(raw)
            if not 1 <= port <= 65535:
                raise ValueError("port must be 1..65535")
            server, _ = start_backend(port, index)
            servers.append(server)
            print(f"backend-{index} listening on 127.0.0.1:{port}", flush=True)
        threading.Event().wait()  # run until Ctrl+C
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
