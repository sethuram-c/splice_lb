# Splice Load Balancer (Linux / C)

A phase-by-phase educational TCP load balancer project. **Phase 2:** a
single-client **blocking IPv4 TCP listener** on the loopback interface.
It creates a listening socket, binds a port, listens for a connection,
accepts one client, and closes both descriptors. **No echo or proxying yet.**

## Fedora prerequisites

```bash
sudo dnf install gcc make python3 git
# Optional tools for manual checks:
sudo dnf install nmap-ncat iproute strace
```

## Build and automated verification

```bash
make clean
make
make test
```

Expected last line: `PASS: Phase 2 listener test suite`. The test uses port
`0`, so the OS chooses an unused port, avoiding hard-coded-port collisions.

## Manual two-terminal check

Terminal A (waits until one client connects):

```bash
make run
# Listening on 127.0.0.1:9090
```

Terminal B:

```bash
ss -ltn '( sport = :9090 )'    # confirm a listening TCP socket
nc -vz 127.0.0.1 9090         # connect once (requires nmap-ncat)
```

After Terminal B connects, Terminal A reports `Accepted client ...` and
`Closed client and listening sockets`, then exits. If port 9090 is already
busy, use `./build/splice-lb 0` and connect to the printed port.

## Program behavior

```text
Usage: ./build/splice-lb [port: 0-65535]
```

Default port: `9090`. Port `0` chooses an available local port.
Only `127.0.0.1` is bound intentionally: the listener is not exposed to the
LAN. It handles exactly **one** client, synchronously, then exits. Phase 3
will introduce data exchange. Later phases will add backend proxying and
nonblocking/epoll support.

## Project structure

- `src/main.c`: blocking TCP listen/accept and socket cleanup.
- `tests/test_listener.py`: end-to-end listener and failure tests.
- `tests/test_socket.sh`: compatibility launcher for the current tests.
- `Makefile`: build, run, test, clean targets.
- `progress.md`: phase log.

Technical notes and Linux command explanations are delivered as a **separate
PDF** and are deliberately not committed to the repository.
