# Splice Load Balancer - Phase 9

Phase 9 introduces **independent 64 KiB user-space queues for each TCP direction**
and enforces demand-only `EPOLLOUT` registration. The proxy remains an
`epoll`-driven, nonblocking *buffered* TCP relay; zero-copy `splice()` starts
in Phase 10. Backend connection setup remains asynchronous (Phase 7).

## Fedora dependencies

```bash
sudo dnf install gcc make python3 git unzip
# Optional for manual inspection:
sudo dnf install iproute strace lsof nmap-ncat
```

## Apply over Phase 8

```bash
cd /path/to/splice-load-balancer
unzip -o ~/Downloads/phase09_changes.zip -d .
make clean
make
make test
```

The **expected final line** is:

```text
PASS: Phase 9 bidirectional buffering and efficient EPOLLOUT test suite
```

The full `make test` runs the Phase 2-9 regression suites. To run only Phase 9:

```bash
python3 tests/test_bidirectional_buffers.py
```

The Phase 9 tests use Linux `/proc/<pid>/fdinfo/<epoll-fd>` to confirm that
idle connections have no `EPOLLOUT` interest, then test **8 MiB of binary
traffic** toward each direction independently with deliberately slow readers.
Both stress tests observe `EPOLLOUT` become enabled when real kernel TCP
backpressure leaves data in the proxy's application queue. A slow peer is
expected to delay, not lose or corrupt, bytes.

## Manual test (three terminals)

Terminal 1 (multi-client backend):

```bash
make backend-multi
```

Terminal 2 (proxy):

```bash
make run
# Listening on 127.0.0.1:9090 -> 127.0.0.1:9091
```

Terminal 3 (client, expected to print the input string):

```bash
./build/backend-client 127.0.0.1 9090 "phase nine"
ss -ltn '( sport = :9090 or sport = :9091 )'
```

Inspect the epoll watch flags while a client is connected and idle:

```bash
PID=$(pidof splice-lb)
ls -l /proc/$PID/fd
# Find the descriptor shown as anon_inode:[eventpoll], then:
cat /proc/$PID/fdinfo/<EPOLL_FD>
```

Replace `<EPOLL_FD>` with that decimal descriptor number. An `events:`
mask in `/proc/.../fdinfo` is printed in hexadecimal; the `EPOLLOUT` bit is
`0x004`. Each watched socket may also carry automatically added `EPOLLERR`
and `EPOLLHUP` flags. The focused Python test parses and checks this correctly.

Tracing calls (restart the proxy in Terminal 2 using this command):

```bash
strace -f -e trace=epoll_ctl,epoll_wait,recvfrom,sendto,shutdown \
  ./build/splice-lb 9090 127.0.0.1 9091
```

`strace` changes scheduling; do not use it for benchmarking.

## Phase 9 internals

A connection has two **separate** `struct direction` objects: client-to-backend
and backend-to-client. Each has a `buffer[65536]`, an `offset` into the first
unsent byte, a `pending` count, and EOF/shutdown flags. Reads append to the
tail and can continue even while some unsent bytes remain; when the tail has
no room, `memmove()` compacts the queue. A destination write advances the
prefix; it never drops a partially sent suffix. Reads pause when `pending`
reaches 64 KiB, so kernel TCP receive-window control supplies backpressure.

`update_interests()` arms the destination's `EPOLLOUT` **only when `pending > 0`**
and arms the source's `EPOLLIN` only when `pending < 65536` and EOF has not
occurred. The backend's `EPOLLOUT` is also needed during `CONNECTING` to
complete nonblocking `connect()`; this is a *different* use of the flag.

During `CONNECTING`, the Phase 8 limit is unchanged: only the **first 8192
bytes** may be staged before `SO_ERROR` confirms the backend is connected.
Once READY, the 64 KiB direction queue is available. EOF is forwarded with
`shutdown(SHUT_WR)` **only when that direction's queue drains completely**.
This preserves TCP half-close semantics and the earlier regression tests.

## Files changed in this phase

- `src/main.c` - independent 64 KiB byte queues and conditional readiness.
- `tests/test_bidirectional_buffers.py` - idle-interest and two slow-peer tests.
- `Makefile` - run Phase 9 suite with the regressions.
- `README.md`, `progress.md` - phase instructions and checklist.

The companion `phase09_learning_guide.pdf` is personal study material and is
**not** part of the source ZIP or Git commit.

## Current limits

Buffers are fixed-size and are allocated per connection: 2 x 65536 bytes
(~128 KiB), excluding sockets, bookkeeping, and kernel buffers. Ten thousand
such objects alone would use ~1.22 GiB, *not* a scalable final memory budget.
Later phases introduce `splice()` pipes and resource limits; do not make
10,000-connection or throughput claims before benchmarking.
EOF and abrupt connection failures still need additional corner-case testing
in their later scheduled phases.
