# Splice Load Balancer (Linux / C)

A step-by-step educational TCP load balancer. **Phase 3** implements a
single-client **blocking TCP echo server** and a separate **backend TCP
client**. The server accepts one connection, echoes arbitrary received bytes,
waits for the peer's write-side EOF, then closes and exits. This is not yet a
proxy or concurrent server.

## Fedora prerequisites

```bash
sudo dnf install gcc make python3 git
# Optional manual diagnostics:
sudo dnf install nmap-ncat iproute strace
```

## Build and test

```bash
make clean
make
make test
```

Both `build/splice-lb` (server) and `build/backend-client` (client) should
compile cleanly. `make test` first reruns the Phase 2 listener regression
suite and then checks empty, text, binary and 128 KiB TCP echo payloads,
plus the separate client program. The last line should be:

```text
PASS: Phase 3 echo and backend client test suite
```

## Manual two-terminal verification

Terminal A (blocks until one client has completed its request):

```bash
make run
# Listening on 127.0.0.1:9090
```

Terminal B:

```bash
./build/backend-client 127.0.0.1 9090 "hello TCP"
# stdout: hello TCP
```

Terminal A then logs the accepted client and its socket cleanup. Run `make
run` **again** before making another client request; this server handles one
client per run. If 9090 is busy, run `./build/splice-lb 0` and use its
printed port in the client command.

To inspect the socket before connecting, use:

```bash
ss -ltn '( sport = :9090 )'
```

## Why the half-close matters

The client calls `shutdown(fd, SHUT_WR)` after sending the payload. This
sends a TCP FIN in its write direction while keeping the receive direction
open. The server's `recv()` returns 0 only after all preceding bytes are
received; the server finishes echoing, closes the connection, and the client
reads until its own `recv()` reaches EOF. This is a basic example of TCP's
independent send and receive directions, used again in later proxy phases.

The code loops on partial `send()` and `write()` results and on `EINTR`. It
uses `MSG_NOSIGNAL` for Linux socket sends to report peer disconnects as
errors instead of being terminated by `SIGPIPE`.

## Command-line interfaces

```text
./build/splice-lb [port: 0-65535]
./build/backend-client <IPv4-address> <port: 1-65535> <payload>
```

The server only binds `127.0.0.1`. The client accepts numeric IPv4
addresses, not hostnames. The client sends a text argument (not embedded NUL);
the server is binary-safe and tests verify arbitrary bytes separately.

## Project structure

- `src/main.c` - one-client blocking TCP echo server.
- `src/backend_client.c` - standalone TCP client using connect/send/recv.
- `tests/test_listener.py` - retained Phase 2 listener regression checks.
- `tests/test_echo.py` - Phase 3 byte-integrity and client tests.
- `tests/test_socket.sh` - compatibility launcher for listener tests.
- `Makefile` - `all`, `run`, `test`, and `clean` targets.
- `progress.md` - phase log.

The separate learning PDF belongs **outside this repository**. Phase 4
combines both directions to make a first blocking proxy.
