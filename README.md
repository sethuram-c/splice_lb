# Splice Load Balancer - Phase 17: Least Connections

Fedora Linux / C11 / Linux `epoll` and `splice()`. **This changed-files ZIP applies on top of Phase 16.**

## New in Phase 17

- **Least Connections** uses the current number of active TCP proxy sessions per backend, not historical selections or bytes transferred.
- A session is counted as active after both endpoints/pipes and epoll registrations have succeeded, including while its backend handshake is `CONNECTING`; it stops counting when the connection is removed. Incomplete setup paths never increment the counters.
- On equal loads a rotating cursor breaks ties rather than favoring backend 0.
- Round Robin remains the default, and the old three-argument single-backend command still works. The new `--algorithm` prefix selects `least-connections` or `round-robin`.
- A failed backend connection is closed with its active slot released. Phase 19 adds backend health/failover policy; this phase does not retry failed targets.
- Existing nonblocking connect, `epoll`, two-pipe `splice()` paths, byte-integrity accounting, and half-closes are unchanged.

## Commands

```bash
# Existing commands remain valid (default Round Robin):
./build/splice-lb 9090 127.0.0.1 9091
./build/splice-lb 9090 127.0.0.1:9091 127.0.0.1:9092

# Select Least Connections, with three local backends:
./build/splice-lb --algorithm least-connections 9090 \
  127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093

# Explicit Round Robin is also supported:
./build/splice-lb --algorithm round-robin 9090 \
  127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093
```

## Apply and test on Fedora

```bash
cd /path/to/splice-load-balancer
unzip -o ~/Downloads/phase17_changes.zip -d .
make clean
make
make test
python3 tests/test_least_connections.py
```

The Phase 17 test uses three identifiable backend servers on available ephemeral loopback ports. It keeps clients connected while verifying 2/2/2 and 7/7/7 balances, disconnects selected clients to create unequal loads, and confirms that new clients choose the least-loaded backend. It also verifies accurate decrement-on-teardown and refused-connect handling. Earlier tests (Phases 2-16) are still run by `make test`.

### Manual three-terminal demonstration

```bash
# Terminal 1: three tagged backend echo servers
make backend-rr

# Terminal 2: proxy
./build/splice-lb --algorithm least-connections 9090 \
  127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093

# Terminal 3: sample TCP client (repeat)
./build/backend-client 127.0.0.1 9090 "hello phase 17"
```

Note: these short sequential connections may rotate exactly as Round Robin; the distinctive Least Connections behavior requires **different numbers of simultaneously open clients**. Use `python3 tests/test_least_connections.py` to prove that difference. No root permissions are required for the local test.

### Reading logs

- `Selected backend[1]=...; selections=N; backend-active-before=M; algorithm=least-connections`: the backend had M live sessions immediately before the new selection.
- `Released backend[1]=...; backend-active=M; total-active=T`: the connection released its target's active count, exactly once.
- `transfer-integrity=verified` is emitted only when both directions drain and close cleanly; incomplete/abrupt connections are not counted as integrity successes.

### Inspection and debugging

```bash
ss -ltn '( sport = :9090 or sport = :9091 or sport = :9092 or sport = :9093 )'
ss -tnp '( sport = :9090 or dport = :9090 )'
strace -f -e trace=connect,getsockopt,epoll_ctl,epoll_wait,splice \
  ./build/splice-lb --algorithm least-connections 9090 \
  127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093
```

`ss` shows kernel TCP sockets but **not the proxy's own backend counters**; consult selection/release logs and the automated tests for the scheduling policy. Do not put the separate `phase17_learning_guide.pdf` in the Git repository.

## Changed/new files

- `src/main.c` - per-backend active counts, least-connections selection, rotating tie-break, CLI switch, logs.
- `tests/test_least_connections.py` - held-open connection assignment, concurrency, refusal, teardown and invalid-argument tests.
- `Makefile` - run the focused test in `make test`.
- `README.md` and `progress.md` - phase documentation.

---

# Splice Load Balancer - Phase 16: Round Robin Backends

Fedora Linux / C11 / Linux `epoll` and `splice()`.
**This is a changed-files patch to apply over Phase 15** - do not start a new repository.

## New in Phase 16

- The proxy now supports up to **32 IPv4:port backend targets** in a fixed backend table.
- TCP clients are assigned **once on acceptance** using a round-robin cursor:
  `selected = next_backend; next_backend = (next_backend + 1) % count`.
- Each connection holds its chosen backend index; a backend TCP connection can
  be pending, and selection does not change when it finishes or disconnects.
- The existing old command (`./build/splice-lb 9090 127.0.0.1 9091`) still
  works, retaining Phase 2-15 regression compatibility.
- A new command specifies one or more backends:

```bash
./build/splice-lb 9090 127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093
```

- New per-accept logs show `Selected backend[0]=127.0.0.1:9091` and the
  cumulative selection count. These counts are selections, **not** active
  backend connections (Least Connections is Phase 17).
- **No retry/failover yet:** if a selected backend is down, that client fails;
  the next accepted client still rotates onward. Failure policy is Phase 19.
- The live binary transfer path is still two-hop `splice()` with independent
  pipes in both directions; no application HTTP parsing is introduced.

## Apply and verify on Fedora

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase16_changes.zip
unzip -o ~/Downloads/phase16_changes.zip -d .
make clean
make
make test
python3 tests/test_round_robin.py
```

The new tests spin up three ephemeral tagged TCP backends, validate 12
sequential exact round-robin assignments, exercise 30 concurrent clients
(10 selections each), verify 43 connection-integrity logs, and reject invalid
backend configurations. **All earlier tests run in `make test`.**

## Manual check - three terminals

```bash
# Terminal 1 - start three identifiable local TCP backends
make backend-rr

# Terminal 2 - proxy listening on loopback 9090
./build/splice-lb 9090 127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093

# Terminal 3 - run multiple times, once per connection
./build/backend-client 127.0.0.1 9090 'hello phase 16'
./build/backend-client 127.0.0.1 9090 'hello phase 16'
./build/backend-client 127.0.0.1 9090 'hello phase 16'
```

Expected client responses start with `backend-0|`, `backend-1|`, and
`backend-2|` respectively; every response includes your original message.
If ports 9090-9093 are occupied, stop the other services or choose free ports
for the backends and corresponding proxy command.

Debug with `ss -ltn '( sport = :9090 or sport = :9091 or sport = :9092 or sport = :9093 )'`
or use `strace -f -e trace=connect,epoll_wait,splice ./build/splice-lb 9090 ...`
to observe nonblocking backend connection setup and live forwarding.
`python3 tests/test_round_robin.py` uses random free test ports to avoid
collisions. No root privileges are required for these local tests.

## Files changed in this phase

- `src/main.c`
- `tests/rr_backends.py`
- `tests/test_round_robin.py`
- `Makefile`
- `README.md`
- `progress.md`

**Keep `phase16_learning_guide.pdf` outside the Git repository.**

---

# Splice Load Balancer - Phase 15

**Phase 15: integrated bidirectional `splice()` with end-to-end byte-integrity verification.** Fedora Linux / C11. This patch applies **over Phase 14**; it is not a standalone project archive.

## What changed

- The established client -> pipe -> backend and backend -> pipe -> client transfer paths remain the primary ready-state relay. Early client bytes still use the preconnect 8 KiB `recv()/send()` staging path, preserving TCP ordering.
- Added `staged_sent` per direction; `forwarded` now has a verifiable decomposition: `staged_sent + spliced_from_pipe == forwarded`.
- Check the live pipe-occupancy equation at each event dispatch: `spliced_to_pipe - spliced_from_pipe == pipe_pending`. A failed check closes only that connection and prints a diagnostic.
- Print `transfer-integrity=verified` only for cleanly completed full-duplex sessions after both EOFs, both write-side shutdowns, fully drained buffers/pipes, and balanced counters. Aborted/incomplete sessions print `transfer-integrity=incomplete` **without** claiming successful delivery.
- Added `tests/test_splice_integrity.py`: 12 concurrent binary TCP streams (0 to 3 MiB+), intentionally irregular fragmentation, simultaneous upload/echo, exact payload comparison, SHA-256 equality, and accounting/log assertions.
- Kept every test from Phases 2-14 and included the Phase 15 test in `make test`.

**Important:** A passing *counter invariant* is a consistency check, **not a cryptographic proof** that content is correct. The separate end-to-end payload comparisons establish byte integrity in the tests. `splice()` reduces userspace copying but does not promise an entirely copy-free physical TCP/NIC data path on every system.

## Apply this changed-files ZIP over Phase 14

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase15_changes.zip
unzip -o ~/Downloads/phase15_changes.zip -d .
make clean
make
make test
```

Focused run (after building):

```bash
python3 tests/test_splice_integrity.py
```

Expected concluding test lines:

```text
PASS: 12 concurrent streaming TCP sessions; 4677783 binary bytes each direction, byte-exact SHA-256
PASS: 0/1/8192/65536-byte boundaries and 3 MiB+ payload preserved
PASS: 12 completed sessions have consistent pipe/staged/forwarded accounting
PASS: Phase 15 splice integration and byte-integrity test suite
```

Manual check in three terminals:

```bash
# Terminal A
make backend-multi
# Terminal B
make run
# Terminal C
./build/backend-client 127.0.0.1 9090 'hello phase 15'
```

Terminal B should print `transfer-integrity=verified` when the client finishes normally.
To inspect kernel syscalls, stop the normal proxy and run:

```bash
strace -f -e trace=splice,epoll_ctl,epoll_wait,shutdown \
  ./build/splice-lb 9090 127.0.0.1 9091
```

Potential test/environment constraints: local loopback bind must be allowed; Linux `/proc` must be mounted for earlier introspection tests. No root access is required for normal tests. The new test is a correctness exercise, **not** proof of 1,000+ supported clients, comparative latency, or end-to-end zero CPU copies; those appear in later phases.

## Phase 15 changed files

- `src/main.c`
- `tests/test_splice_integrity.py`
- `Makefile`
- `README.md`
- `progress.md`

**Keep `phase15_learning_guide.pdf` outside the Git repository.**

---

# Splice Load Balancer - Phase 14

**Phase 14: pipe-drain `EAGAIN` and demand-driven `EPOLLOUT`.** Fedora Linux, C11.
This is a change-only update to Phase 13; apply it over your current repository.

## What changed in Phase 14

- After `splice(pipe -> socket)` returns positive but fewer bytes than requested,
  count the real partial drain and update `pipe_pending` by the returned amount.
- After the same splice returns `EAGAIN`, **keep pending bytes in the pipe**,
  mark that direction `drain_blocked`, and increment `pipe_drain_eagain`.
  These events are distinct from Phase 13's `socket -> pipe` fill-side `EAGAIN`.
- Explicitly compute `EPOLLIN` and `EPOLLOUT` interests for each socket:
  source `EPOLLIN` stops when its outbound pipe/queue is occupied; destination
  `EPOLLOUT` is enabled only while queued output needs another opportunity.
- On a writable event, retry the blocked drain; clear `drain_blocked` when
  progress occurs; once all data drain, remove `EPOLLOUT` and resume source
  `EPOLLIN` without busy-spinning or treating `EAGAIN` as fatal.
- Continue delaying a TCP half-close until **both** the preconnect staging
  queue and the direction pipe are empty.
- Add targeted tests that hold a real TCP peer's receive side idle, verify a
  **real pipe-drain `EAGAIN`**, watch EPOLLOUT through `/proc/<pid>/fdinfo`,
  resume delivery, confirm EPOLLOUT is disarmed while the sockets stay open,
  and compare 8 MiB binary transfers byte-for-byte in **both** directions.

## Install and verify on Fedora

```bash
sudo dnf install gcc make python3 git unzip
# Optional diagnostics: sudo dnf install strace iproute nmap-ncat

cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase14_changes.zip
unzip -o ~/Downloads/phase14_changes.zip -d .
make clean
make
make test
```

Expected Phase 14 tail:

```text
PASS: stalled client -> real pipe-drain EAGAIN -> EPOLLOUT retry -> idle disarm; 8 MiB intact
PASS: stalled backend -> real pipe-drain EAGAIN -> EPOLLOUT retry -> idle disarm; 8 MiB intact
PASS: Phase 14 pipe-drain EAGAIN and event-interest updates test suite
```

Focused run: `python3 tests/test_pipe_drain.py` (requires `make` first).

## Manual three-terminal test

```bash
# Terminal 1
make backend-multi
# Terminal 2
make run
# Terminal 3
./build/backend-client 127.0.0.1 9090 'hello phase 14'
```

Expected client output: `hello phase 14`.

## Inspect runtime readiness and system calls

```bash
ss -ltn '( sport = :9090 or sport = :9091 )'
PID=$(pidof splice-lb)
ls -l /proc/$PID/fd
# Identify the eventpoll fd in the listing, then:
cat /proc/$PID/fdinfo/EPOLL_FD

# Restart the proxy under strace if desired:
strace -f -e trace=splice,epoll_ctl,epoll_wait,shutdown \
  ./build/splice-lb 9090 127.0.0.1 9091
```

`/proc/<pid>/fdinfo` epoll masks are in hexadecimal; `EPOLLOUT` is bit `0x4`.
The targeted test checks masks while sockets stay open, avoiding the false
positive of a watch disappearing simply because a connection closed.

**Correctness invariants:** `spliced_to_pipe - spliced_from_pipe == pipe_pending`;
no source read while that direction has undelivered data; do not send FIN until
`pending == 0 && pipe_pending == 0`. `EAGAIN` is not TCP EOF. A writable
notification is not permission to write forever: stop again at `EAGAIN`.

**Limitations:** This phase is still a single-backend learning proxy. It does
not yet add Round Robin/Least Connections, prove thousands of connections, or
claim measured latency improvement. Phase 15 strengthens overall splice
integration and systematic byte-integrity verification.

## Files changed in Phase 14

- `src/main.c`
- `tests/test_pipe_drain.py`
- `Makefile`
- `README.md`
- `progress.md`

**Keep the personal `phase14_learning_guide.pdf` outside the Git repository.**

---

## Previous phases (archived)

# Splice Load Balancer - Phase 13

**Phase 13: partial transfers and pipe-fill `EAGAIN`.** Fedora Linux / C11.
This is a change-only phase over Phase 12: two-hop `splice()` forwarding,
nonblocking TCP connections, and all previous tests remain supported.

## Changes introduced in Phase 13

- Use a **bounded pipe-fill loop**, requesting at most the remaining amount
  of a 64 KiB per-dispatch limit. Each positive `splice()` return is counted
  as that many bytes, **not** the requested byte count.
- Retry interrupted transfers (`EINTR`), distinguish stream EOF (`0`),
  and treat `EAGAIN` as a nonfatal readiness condition.
- If the pipe still contains bytes when socket-to-pipe `splice()` stops,
  attempt to drain it to the destination socket; stop reading from the source
  while pipe bytes are pending. The destination `EPOLLOUT` interest resumes
  draining if the socket cannot accept data now.
- Add per-direction counters: `partial-pipe-writes` and `pipe-write-EAGAIN`.
  The EAGAIN counter increments when a socket-to-pipe call returns EAGAIN
  with bytes already in the pipe; it alone does **not** prove the pipe is full.
- Add a **test-only** LD_PRELOAD probe that reduces each pipe to 4096 bytes
  using `F_SETPIPE_SZ` and records real short `splice()` returns and EAGAIN
  while `FIONREAD` confirms unread source-socket bytes.
- Confirm 1 MiB of exact binary data in **both directions**, plus all old
  test suites. `src/main.c` has no dependency on the test probe.

## Fedora prerequisites

```bash
sudo dnf install gcc make python3 git unzip
# Optional command-line inspection:
sudo dnf install strace iproute nmap-ncat
```

## Apply only the changed files over your Phase 12 repository

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase13_changes.zip
unzip -o ~/Downloads/phase13_changes.zip -d .
make clean
make
make test
```

Expected last line:

```text
PASS: Phase 13 partial transfers and pipe-fill EAGAIN test suite
```

Focused test, with the production server already built:

```bash
python3 tests/test_pipe_fill.py
```

To inspect your actual TCP listener and `splice()` kernel calls:

```bash
# In three different terminals:
make backend-multi
make run
./build/backend-client 127.0.0.1 9090 'hello phase 13'

# Optionally restart proxy under a syscall tracer:
strace -f -e trace=splice,epoll_ctl,epoll_wait,shutdown \
  ./build/splice-lb 9090 127.0.0.1 9091
```

**Test-only versus product behavior:** The probe is compiled as a temporary
`.so` by `tests/test_pipe_fill.py` and is loaded **only into the test proxy**.
It changes pipe sizes to reproduce the edge case reliably. Normal `make run`
uses whatever pipe capacity the Linux kernel chooses. To prove a *pipe-full*
EAGAIN rather than just an empty input socket, the probe checks that unread
bytes remain in the input socket via `ioctl(FIONREAD)`.

**Invariants:** Each direction has its own pipe. Bytes in `pipe_pending` are
not counted as forwarded. Preconnect `recv()` bytes are delivered first; TCP
FIN is propagated only after the pending user queue and pipe are empty.
The source `EPOLLIN` stays disabled while a pipe is occupied.

**Remaining work:** Phase 14 refines pipe-drain `EAGAIN` and event-interest
updates. This phase does not claim higher throughput, 10,000 clients, or
complete elimination of all possible internal kernel copies.

## Files changed in this phase

- `src/main.c`
- `tests/pipe_fill_probe.c` (test-only)
- `tests/test_pipe_fill.py`
- `Makefile`
- `README.md`
- `progress.md`

Keep `phase13_learning_guide.pdf` **outside the repository**.

---

## Earlier phase notes (historical)

# Splice Load Balancer - Phase 12

**Phase 12: complete client-to-backend and reverse `splice()` paths.**
Target: Fedora Linux. The live relay now transfers TCP traffic in both directions
using Linux `splice()` through separate nonblocking kernel pipes.

## What changed

- Client-to-backend now forwards using `splice(client socket -> pipe)` followed
  by `splice(pipe -> backend socket)`; it no longer drains its pipe using
  `read()` into a C buffer during normal forwarding.
- Backend-to-client now uses the independent second pipe and the same two-hop
  `splice()` transfer logic.
- Phase 8's **8 KiB preconnect staging queue** remains a deliberate exception:
  data received before the backend is READY still uses `recv()` and `send()`.
  Staged bytes are always sent **before** subsequently spliced bytes.
- Per-direction `pipe_pending`, `spliced_to_pipe`, `spliced_from_pipe`, and
  `forwarded` counters distinguish kernel pipe occupancy from data accepted by
  destination sockets. Pipe bytes are NEVER treated as delivered on input.
- `EPOLLIN` pauses while that direction has bytes in its pipe or preconnect
  queue; destination `EPOLLOUT` is registered only for pending output.
- TCP FIN (`shutdown(SHUT_WR)`) is delayed until both preconnect data and pipe
  bytes have drained, preserving full-duplex and half-close behavior.
- The proxy ignores `SIGPIPE` so a peer closing during `splice(pipe -> socket)`
  does not terminate the entire multi-client process; `EPIPE` is handled locally.
- A test-only syscall observer now audits both `socket -> pipe` and
  `pipe -> socket` calls; tests validate byte integrity and accounting.

## Fedora prerequisites

```bash
sudo dnf install gcc make python3 git unzip
sudo dnf install strace iproute nmap-ncat # optional debugging
```

## Apply the ZIP over your Phase 11 checkout

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase12_changes.zip
unzip -o ~/Downloads/phase12_changes.zip -d .
make clean
make
make test
```

Expected final line:

```text
PASS: Phase 12 complete bidirectional splice transfer test suite
```

Focused new test:

```bash
python3 tests/test_full_splice.py
```

The test runs a real proxy + backend and uses a test-only `LD_PRELOAD` library
that passes every call to the **real Linux syscall**. It verifies both hop
families, preconnect staging, two 1 MiB binary transfers, and a backend that
half-closes its write direction before the client finishes sending.

## Manual smoke test (three terminals)

```bash
# Terminal A: backend
make backend-multi
# Terminal B: proxy
make run
# Terminal C: client
./build/backend-client 127.0.0.1 9090 "hello phase 12"
```

Expected client output: `hello phase 12`. A small message might be handled
entirely by preconnect staging: use the 1 MiB focused test to **prove** real
`splice()` calls. The proxy remains listening after a session ends.

Inspect real splice calls (restart Terminal B with this command):

```bash
strace -f -e trace=splice,epoll_ctl,epoll_wait,shutdown \
  ./build/splice-lb 9090 127.0.0.1 9091
```

Use `ss -ltn '( sport = :9090 )'` to confirm the TCP listener exists. `strace`
adds syscall overhead: do **not** use its timings as throughput numbers.

## Current data paths

```text
client socket --splice()--> [pipe_to_backend] --splice()--> backend socket
backend socket --splice()--> [pipe_to_client]  --splice()--> client socket

Before backend READY only:
client socket --recv()--> [max 8 KiB preconnect buffer] --send()--> backend
```

A successful first splice only means bytes have entered the kernel pipe;
`forwarded` increments when the second splice successfully passes bytes to the
destination kernel socket. This avoids routine userspace payload copies during
READY-state forwarding; it does **not** prove that the kernel made absolutely
zero internal copies, nor that throughput increased. Profile and benchmark
later in the roadmap.

**Known Phase 12 limitations:** the relay permits one outstanding pipe chunk
per direction rather than fully exploiting pipe capacity; Phases 13-14 develop
partial pipe-fill/pipe-drain `EAGAIN` behavior and event-interest transitions.
Resource limits, failures, timeouts, load balancing, and performance measurement
are covered in subsequent phases.

## Files changed in this phase

- `src/main.c`: bidirectional splice datapath and SIGPIPE safety.
- `tests/splice_observer.c`: audit pipe-to-socket as well as socket-to-pipe.
- `tests/test_client_splice.py`: preserve Phase 11 assertions for both paths.
- `tests/test_full_splice.py`: new end-to-end bidirectional syscall/integrity tests.
- `Makefile`: include new test with `make test`.
- `README.md`, `progress.md`: instructions and progress.

Keep `phase12_learning_guide.pdf` **outside the repository**; it is not in the ZIP.

---

## Earlier phase notes (historical)

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

## Phase 10 - first `splice()` experiment and per-connection pipes

**What is new:** `build/splice-demo` copies a regular file through a Linux
pipe using `splice(input -> pipe)` followed by `splice(pipe -> output)`.
The TCP proxy also allocates **two nonblocking pipes per accepted connection**:
`pipe_to_backend[2]` and `pipe_to_client[2]`, created with
`pipe2(O_NONBLOCK | O_CLOEXEC)`. Each pipe has a read fd at index 0 and a
write fd at index 1. All four fds are closed on connection removal and any
partial-setup failure.

**Important:** This phase does **not** use `splice()` inside the TCP proxy.
The live forwarding path still uses Phase 9's bounded `recv()`/`send()`
buffers. We first prove `splice()` independently and ensure pipe resources
are correct; Phase 11 begins client-socket-to-pipe forwarding.

### Build and run on Fedora

```bash
make clean
make
make test
# Focused test only (make must have built the binaries first):
python3 tests/test_splice_pipes.py
```

Experiment with an 800 KiB deterministic input, including NUL bytes:

```bash
python3 - <<'PY'
from pathlib import Path
Path('/tmp/splice-phase10-in.bin').write_bytes(bytes(range(256)) * 3200)
PY
./build/splice-demo /tmp/splice-phase10-in.bin /tmp/splice-phase10-out.bin
cmp /tmp/splice-phase10-in.bin /tmp/splice-phase10-out.bin
sha256sum /tmp/splice-phase10-in.bin /tmp/splice-phase10-out.bin
```

Expected demo line: `splice-demo: transferred 819200 bytes`. `cmp` exits
with status 0 and prints nothing when the files are byte-for-byte equal.
Both `sha256sum` digests should match. The demo safely rejects an input
and output referencing the same inode (including hard links), preventing
accidental truncation of the input.

Optional syscall trace (install `strace` on Fedora if needed):

```bash
strace -e trace=pipe2,splice ./build/splice-demo \
  /tmp/splice-phase10-in.bin /tmp/splice-phase10-out.bin
```

The demo uses a **blocking** pipe to make its first experiment easy to
understand. The proxy's per-session pipes, in contrast, are **nonblocking**.
The `SPLICE_F_MOVE` hint is not a promise that data is never copied; the
actual kernel implementation and descriptors determine transfer behavior.

### Live proxy still works

In three separate terminals:

```bash
make backend-multi
make run
./build/backend-client 127.0.0.1 9090 'hello phase 10'
```

Expected client output: `hello phase 10`. For the per-session pipe
allocation and cleanup test run `python3 tests/test_splice_pipes.py`.
Linux `/proc/<pid>/fd` symlinks expose each live pipe as `pipe:[inode]`;
`/proc/<pid>/fdinfo/<fd>` contains the octal `flags` checked by the test.
Standard process output streams can themselves be pipes; the automated
inspection ignores descriptors 0-2.

### Limits and later work

Phase 10 still allocates 2 x 64 KiB in user-space buffers per session,
**plus** four pipe descriptors and kernel pipe storage. Pipe buffers also
consume kernel resources: thousands of sessions must be verified later
against memory and `ulimit -n` rather than assumed to scale. Pipe-to-socket
forwarding, `EAGAIN` recovery, and pipe occupancy tracking follow in Phases
11-14. The learning guide PDF is personal study material, not a repo file.
