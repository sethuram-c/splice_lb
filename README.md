# Splice Load Balancer - Phase 11

**Phase 11: direction state and client-to-pipe transfer.** Target: Fedora Linux.
This is a partial kernel-pipe data path, **not a complete zero-copy proxy**.

## What changed

- The client-to-backend `struct direction` has explicit `input_mode` state:
  `INPUT_SPLICE_TO_PIPE`; backend-to-client retains `INPUT_COPY`.
- After backend readiness, the client input path calls Linux `splice()` from
  the nonblocking TCP socket into the dedicated `pipe_to_backend` write end.
- The proxy immediately drains the pipe read end with `read()` into the
  existing bounded 64 KiB queue and sends via nonblocking `send()`.
- `pipe_pending` counts kernel-pipe bytes separately from `pending`, which
  counts user-space bytes. EOF-to-FIN propagation waits for both to drain.
- During `CONNECTING`, the existing 8 KiB `recv()` staging behavior remains
  unchanged: no splice() is attempted before the backend is ready.
- The reverse direction retains `recv()` -> buffered `send()`. Phase 12 will
  implement the pipe-to-backend stage and the reverse splice path.
- A test-only LD_PRELOAD interposer proves the actual running proxy calls
  `splice(socket, pipe)`; the tests also verify data and half-close integrity.

## Fedora prerequisites

```bash
sudo dnf install gcc make python3 git unzip
# Optional inspection commands:
sudo dnf install strace iproute nmap-ncat
```

## Apply Phase 11 over Phase 10

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase11_changes.zip
unzip -o ~/Downloads/phase11_changes.zip -d .
make clean
make
make test
```

Expected last line:

```text
PASS: Phase 11 direction state and client-to-pipe splice test suite
```

Focused testing (after `make`):

```bash
python3 tests/test_client_splice.py
```

## Manual forwarding test - three terminals

```bash
# Terminal A: test echo backend
make backend-multi
# Terminal B: proxy
make run
# Terminal C: client
./build/backend-client 127.0.0.1 9090 "hello phase 11"
```

The client prints `hello phase 11`. The proxy remains running after the
connection ends and its log includes a `client-to-pipe=N bytes` counter.
For tiny requests, the count may be **0** because Phase 8's preconnect buffer
can absorb the full request before the backend connection is ready. The
focused 512 KiB automated test guarantees that live `splice()` occurs.

To see the exact kernel calls, run the proxy through `strace` in Terminal B:

```bash
strace -e trace=splice,read,sendto,epoll_ctl,epoll_wait \
  ./build/splice-lb 9090 127.0.0.1 9091
```

`strace` writes syscall traces to stderr. Look for `splice(client_fd, NULL,
pipe_write_fd, NULL, count, SPLICE_F_MOVE|SPLICE_F_NONBLOCK)` followed by
`read(pipe_read_fd, ...)` and `sendto(backend_fd, ...)`. Use a large client
payload to bypass preconnect staging. `strace` affects scheduling; do not use
it for performance benchmarks. The focused test uses `LD_PRELOAD` to audit
successful `splice()` calls reliably without relying on trace timing.

## Data-path design and limitations

```text
client socket --splice()--> [kernel pipe] --read()--> [64 KiB queue]
                                                     --send()--> backend socket
backend socket --recv()--> [64 KiB queue] --send()--> client socket
```

`pipe_pending` tracks bytes in the kernel pipe; `pending` tracks bytes in the
application queue. Total delivery accounting increments only after successful
`send()` to the backend. `EPOLLOUT` remains demand-driven. The test shims are
not linked into the proxy; they are compiled only inside the Python test.

Phase 11 is a correctness checkpoint, not a throughput improvement claim:
there is still a kernel-to-user copy and user-to-kernel send. Full `splice()`
forwarding, zero-copy claims, two-way backpressure, and performance
benchmarking belong to later phases.

## Source changes and personal learning notes

- `src/main.c`: state, socket-to-pipe splice, pipe-to-buffer drain, EOF gating.
- `tests/splice_observer.c`: test-only syscall audit shim.
- `tests/test_client_splice.py`: 512 KiB integrity, delayed connect and EOF.
- `Makefile`: include the Phase 11 tests.
- `README.md`, `progress.md`: instructions and learning checklist.

`phase11_learning_guide.pdf` is kept **outside this repository and ZIP**.

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
