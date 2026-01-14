#!/usr/bin/env python3
"""One-thread, epoll-backed TCP echo fixture for Phase 30.

Unlike ThreadingTCPServer, this fixture scales to many idle peers without
allocating a Python OS thread per backend connection. It is test-only.
"""
from __future__ import annotations

from contextlib import contextmanager
import selectors
import socket
import threading
from typing import Iterator

HOST = '127.0.0.1'


class ScalableEchoBackend:
    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((HOST, 0))
        self.listener.listen(4096)
        self.listener.setblocking(False)
        self.port = self.listener.getsockname()[1]
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.listener, selectors.EVENT_READ, data=None)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name='scalable-backend', daemon=True)
        self.lock = threading.Lock()
        self.accepted = 0
        self.live = 0
        self.error: BaseException | None = None
        self.buffers: dict[socket.socket, bytearray] = {}

    def start(self) -> None:
        self.thread.start()

    def snapshot(self) -> tuple[int, int]:
        with self.lock:
            return self.accepted, self.live

    def _close_peer(self, peer: socket.socket) -> None:
        try:
            self.selector.unregister(peer)
        except (KeyError, ValueError):
            pass
        self.buffers.pop(peer, None)
        peer.close()
        with self.lock:
            self.live -= 1

    def _run(self) -> None:
        try:
            while not self.stop_event.is_set():
                for key, mask in self.selector.select(timeout=0.1):
                    if key.fileobj is self.listener:
                        while True:
                            try:
                                peer, _ = self.listener.accept()
                            except BlockingIOError:
                                break
                            peer.setblocking(False)
                            self.buffers[peer] = bytearray()
                            self.selector.register(peer, selectors.EVENT_READ)
                            with self.lock:
                                self.accepted += 1
                                self.live += 1
                        continue
                    peer = key.fileobj
                    pending = self.buffers[peer]
                    if mask & selectors.EVENT_READ:
                        try:
                            block = peer.recv(4096)
                        except (ConnectionResetError, BrokenPipeError, OSError):
                            self._close_peer(peer)
                            continue
                        if not block:
                            self._close_peer(peer)
                            continue
                        pending.extend(block)
                    if pending:
                        try:
                            sent = peer.send(pending)
                        except (BlockingIOError, InterruptedError):
                            sent = 0
                        except (ConnectionResetError, BrokenPipeError, OSError):
                            self._close_peer(peer)
                            continue
                        if sent:
                            del pending[:sent]
                    # Never subscribe to writable events unless bytes are queued.
                    self.selector.modify(peer, selectors.EVENT_READ |
                                         (selectors.EVENT_WRITE if pending else 0))
        except BaseException as exc:
            self.error = exc
        finally:
            for peer in list(self.buffers):
                self._close_peer(peer)
            self.selector.unregister(self.listener)
            self.listener.close()
            self.selector.close()

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('scalable backend thread did not stop')
        if self.error is not None:
            raise RuntimeError('scalable backend failed') from self.error


@contextmanager
def scalable_echo_backend() -> Iterator[ScalableEchoBackend]:
    server = ScalableEchoBackend()
    server.start()
    try:
        yield server
    finally:
        server.close()
