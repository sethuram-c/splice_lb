# Splice Load Balancer - Phase 8

Phase 8 adds **bounded preconnect client buffering** on top of Phase 7's
nonblocking backend `connect()`. A client may send bytes before the backend
handshake completes. We now read at most **8 KiB into the connection's
client-to-backend buffer**, temporarily turn off client `EPOLLIN`, and only
send the staged bytes after `getsockopt(SO_ERROR)` reports a successful
connection. The forwarding path remains buffered and nonblocking; `splice()`
will be introduced in Phase 10.

## Fedora prerequisites

```bash
sudo dnf install gcc make python3 git unzip
# Optional diagnostics and a manual TCP client:
sudo dnf install iproute strace lsof nmap-ncat
```

The automated preconnect delay test compiles a small **test-only**
`LD_PRELOAD` shared object. It is not linked into the production proxy.

## Apply Phase 8 to Phase 7

```bash
cd /path/to/splice-load-balancer
unzip -o ~/Downloads/phase08_changes.zip -d .
make clean
make
make test
```

Expected final test line:

```text
PASS: Phase 8 client preconnect buffering test suite
```

Run only the targeted suite:

```bash
python3 tests/test_preconnect_buffer.py
```

The test holds backend `EPOLLOUT` readiness for 250 ms in a test-only shim,
so client `EPOLLIN` gets processed while the connection is still CONNECTING.
It verifies **exactly 8192 staged bytes** for a 256 KiB request, a small
22-byte request, an empty half-closed request, 24 concurrent larger-than-buffer
requests, and failure isolation for refused backends. Previous Phase 2-7
regression suites are also run by `make test`.

## Manual demonstration (three terminals)

Terminal 1 - concurrent echo backend:

```bash
make backend-multi
```

Terminal 2 - proxy:

```bash
make run
# Listening on 127.0.0.1:9090 -> 127.0.0.1:9091
```

Terminal 3 - example clients:

```bash
./build/backend-client 127.0.0.1 9090 "hello before backend ready"
./build/backend-client 127.0.0.1 9090 "second client"
ss -ltn '( sport = :9090 or sport = :9091 )'
```

Expected client outputs are their original strings. A pending backend may
print `Backend connection pending; active=N`, followed by
`Connected to backend; preconnect-buffered=X bytes; active=N`.
The `X` value depends on kernel event timing in normal operation and may
be zero. The delay-shim test is what guarantees an early-read window.

For syscall inspection (with backend still running):

```bash
strace -f -e trace=connect,getsockopt,epoll_ctl,epoll_wait,recvfrom,sendto \
  ./build/splice-lb 9090 127.0.0.1 9091
```

`strace` modifies timing and is not a throughput benchmark.

## Algorithm: CONNECTING -> READY

1. `accept()` creates a nonblocking client and backend socket.
2. If `connect()` returns `EINPROGRESS`, watch both client `EPOLLIN` and
   backend `EPOLLOUT`.
3. A readable client stages at most 8192 bytes in `to_backend.buffer`;
   no send to the unconnected backend is attempted.
4. While that staging buffer is occupied, remove client `EPOLLIN` to
   prevent overwriting data or repeatedly reading. Kernel receive buffering
   provides backpressure to larger client uploads.
5. An early client EOF is recorded, **not** forwarded to the unfinished
   backend socket.
6. On backend completion, check `SO_ERROR`. If zero, switch to READY,
   flush staged data (possibly partially), then continue ordinary relay.
   If EOF was already recorded, `shutdown(SHUT_WR)` occurs *after* draining.
7. Any setup or connect error closes only the affected connection.

This design uses **one bounded preconnect buffer per connection**, not an
unbounded queue. While the Phase 8 tests cover concurrency and correctness,
they are not performance benchmarks and do not demonstrate 1,000+ sessions.

## New or modified files in the ZIP

- `src/main.c` - early read staging, readiness gating, deferred FIN, flushing.
- `tests/delay_backend_ready.c` - test-only readiness delay via `LD_PRELOAD`.
- `tests/test_preconnect_buffer.py` - deterministic and concurrent integration tests.
- `Makefile` - runs the new suite in `make test`.
- `README.md`, `progress.md` - usage and completion notes.

Keep `phase08_learning_guide.pdf` outside Git. Other project source and tests
from earlier phases remain in your repository unchanged.
