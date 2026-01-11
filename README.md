# Phase 27 - Connection Limits, Operational Logs, and Graceful Shutdown

Fedora Linux | C11 | `epoll` + `splice()` | **apply on top of Phase 26**.

## Changes

- Introduce `--max-connections` (1..100000, default **10000**), a cap on **published active connections** (connecting and established). Extra accepted TCP sockets are closed immediately. A completed/failed connection frees its slot through the existing centralized cleanup path.
- Introduce `--shutdown-grace-ms` (100..120000, default **2000**). On SIGINT or SIGTERM, stop accepting new clients and keep forwarding existing sessions until both TCP directions finish or the grace deadline expires. At expiry, close remaining connections using centralized cleanup, without replaying any data.
- Receive SIGINT/SIGTERM through Linux `signalfd` registered in `epoll` (not an asynchronous signal handler). Events from signalfd are handled before other readiness events in the same batch. A **second** shutdown signal expires the grace interval immediately.
- Emit timestamped `LOG ts=<epoch.seconds> level=INFO|WARN event=...` records for startup, connection-limit rejection, and shutdown stages/summary. Preserve the earlier detailed per-connection selection/close lines: duplicating them would add avoidable stdout blocking under heavy tests.
- Track totals accepted, rejected, closed and graceful. Counts describe this proxy process only, not persistent monitoring/metrics.
- Retain signal-derived process exit statuses after cleanup (`SIGTERM` -> subprocess `-15`, `SIGINT` -> `-2`) for compatibility with earlier phase tests. Resource release and shutdown logging occur **before** the conventional signal termination status is restored.
- Test the cap, capacity recovery, delayed in-flight response, force close after grace, idle SIGINT and invalid CLI options. The Phase 24 test-only `epoll_ctl` injector now ignores the `signalfd` control registration. Prior `epoll` tests count the control registration explicitly.

## Fedora verification

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase27_changes.zip
unzip -o ~/Downloads/phase27_changes.zip -d .
make clean
make
make test-operations
make test
```

Expected focused ending: `PASS: Phase 27 connection limits, logging, and graceful shutdown test suite`.
Run the full suite as a separate regression pass. Keep `phase27_learning_guide.pdf` outside Git.

## Manual demonstration (three terminals)

Terminal 1:

```bash
make backend-rr
```

Terminal 2:

```bash
./build/splice-lb --algorithm=least-connections \
  --max-connections=2 --shutdown-grace-ms=2000 \
  9090 127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093
```

Terminal 3:

```bash
./build/backend-client 127.0.0.1 9090 'hello phase 27'
```

Expected response starts `backend-0|`, `backend-1|`, or `backend-2|`. Stop Terminal 2 with **Ctrl+C**; it should log `event=shutdown_started` and a `event=shutdown_drained` (or `shutdown_forced`) record, then `event=server_stopped`.

For the connection cap, start three **held-open** TCP sockets and inspect logs. A one-shot client is too short-lived to demonstrate the limit reliably; use `make test-operations` for the deterministic scenario.

## Commands and troubleshooting

- `make clean`: delete compiled build products only.
- `make test-operations`: run the four operational behavior scenarios plus CLI validation.
- `make test`: run all regression and integration tests, including Phase 24/26 test-only fault injectors.
- `ss -ltn '( sport = :9090 )'`: confirm listening socket; should disappear after shutdown begins.
- `PID=$(pidof splice-lb); ls -l /proc/$PID/fd`: view the server's open sockets, pipes, epoll fd and signalfd.
- `cat /proc/$PID/fdinfo/FD`: inspect epoll watched descriptors (`FD` is the epoll file descriptor).
- `strace -e trace=signalfd4,rt_sigprocmask,epoll_wait,close -f ./build/splice-lb ...`: inspect signal/event and teardown syscalls, not throughput.
- `kill -TERM "$PID"`: request graceful shutdown; `kill -INT "$PID"` is equivalent to Ctrl+C.
- `ulimit -n`: display shell's open file descriptor ceiling; it may limit practical capacity below the configured cap.

### Important design boundaries

This cap limits active **sessions**, not the number of file descriptors (each session requires two sockets plus four pipe endpoints), nor memory committed by the kernel TCP stacks. The default limit **does not establish** that 10,000 connections have been measured; Phase 30 will stress-test capacity. On a forced shutdown, pending data can be discarded. To protect service continuity across restarts, an external supervisor and coordinated restart strategy would be needed; Phase 27 implements per-process graceful draining only.

**Changed files:** `src/main.c`, `tests/setup_faults.c`, `tests/test_epoll.py`, `tests/test_multiclient.py`, `tests/test_bidirectional_buffers.py`, `tests/test_operational_controls.py`, `Makefile`, `README.md`, `progress.md`.

---

# Phase 26 - Backend Connection and Idle Timeouts

Fedora Linux | C11 | nonblocking TCP | Linux `epoll` + `splice()` | apply **over Phase 25**.

## What changed

- Add `--connect-timeout-ms` (100..120000, default **5000**) and `--idle-timeout-ms` (100..3600000, default **60000**); both support `--name=N` and `--name N`. Defaults avoid disrupting earlier regression scenarios.
- Store two monotonic timestamps per session: backend connect start and last successful byte movement. A pending TCP handshake is governed **only by its connection deadline**, so clients cannot extend it by sending preconnect bytes.
- Refresh the ready-session idle clock on successful preconnect `recv()`, buffered `send()`, socket-to-pipe `splice()`, and pipe-to-socket `splice()`; `EPOLLIN`, `EPOLLOUT`, `EAGAIN`, and `EPOLLRDHUP` **are not activity** by themselves.
- Pass the nearest deadline as the `epoll_wait()` timeout; when no clients are connected, keep `epoll_wait(..., -1)` to sleep until a real event. Use `CLOCK_MONOTONIC` so wall-clock adjustments cannot shift deadlines.
- On connect timeout, mark the backend unhealthy with `ETIMEDOUT` and reuse Phase 19's cooldown/probe mechanism. On idle timeout, close only that connection. Both use centralized Phase 23/24 cleanup, preserving the listener and releasing active connection counters, sockets, pipes and epoll registrations.
- Include an **integration-only** `LD_PRELOAD` shim (`tests/timeout_inject.c`) that suppresses one pending backend's `EPOLLOUT` readiness. A silent local backend then makes a real repeatable handshake-timeout test possible without internet routing assumptions. The shim is never part of the production executable.

## Fedora verification

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase26_changes.zip
unzip -o ~/Downloads/phase26_changes.zip -d .
make clean
make
make test-timeouts
make test
```

Expected focused ending: `PASS: Phase 26 backend connection and idle timeouts test suite`.
Run `make test` for all prior phases; it builds both test-only shared libraries before running the tests. Large suites can take several minutes. Focused command tests: invalid CLI values, an idle connection closed after roughly 300 ms, ongoing traffic renewing its deadline, clean reconnection, a deliberately suppressed backend writable notification causing a 240 ms connect timeout, and re-probing that backend after 100 ms cooldown.

## Manual test (three terminals)

Terminal 1:

```bash
make backend-rr
```

Terminal 2:

```bash
./build/splice-lb --algorithm=least-connections \
  --connect-timeout-ms 2500 --idle-timeout-ms 2000 \
  9090 127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093
```

Terminal 3:

```bash
./build/backend-client 127.0.0.1 9090 'hello phase 26'
```

Expected reply: `backend-0|hello phase 26`, or `backend-1|...` / `backend-2|...` depending on selection. The startup `Configuration` line prints both timeout values. The small example checks normal forwarding, **not** idle expiry; use `make test-timeouts` to see both timeout cases reliably.

## Commands explained

- `make clean`: delete the `build` directory (no source changes).
- `make`: compile strict-warning C11 proxy and helper programs.
- `make test-timeouts`: build the test-only shared object and execute deterministic integration cases.
- `make test`: run the full phase-by-phase integration suite.
- `ss -tan '( sport = :9090 or dport = :9090 )'`: list TCP sockets associated with the proxy port and their states.
- `PID=$(pidof splice-lb)` followed by `ls -l /proc/$PID/fd`: inspect live process descriptors, including sockets, pipes, and the epoll instance.
- `strace -f -e trace=epoll_wait,connect,getsockopt,shutdown,close ./build/splice-lb ...`: inspect timer wakeups and setup/teardown. `strace` affects timing; do not use it for performance measurements.
- `date -Ins` reports wall clock time but **must not** drive timeout logic; `CLOCK_MONOTONIC` does.

## Important limits and semantics

A connect timeout measures the interval between initiating the backend connection and detecting completion, **not** the health of an already-connected backend. An idle timeout measures absence of *successful byte progress*, including activity in both directions; stalled transfers can expire on purpose. A successful TCP connection is not an application-level health check. The deadline scan is `O(active connections)` per loop iteration for clarity at this stage; later benchmarking must measure its cost at 1,000+ sessions. The test-only shim changes epoll registrations by `LD_PRELOAD`; never set `LD_PRELOAD` globally. Keep `phase26_learning_guide.pdf` outside this repository and out of Git.

**Changed files:** `src/main.c`, `tests/timeout_inject.c`, `tests/test_timeouts.py`, `Makefile`, `README.md`, `progress.md`.

---

# Phase 25 - Command-Line Configuration and Validation

Fedora Linux | C11 | nonblocking TCP | `epoll` | `splice()` | apply on top of **Phase 24**.

## What changed

- Add strict, explicit options before the existing positional listener/backend arguments: `--listen-ip` (default `127.0.0.1`), `--backlog` (1..4096, default 128), and `--backend-retry-ms` (100..60000, default 3000). Keep `--algorithm` from Phase 18.
- Accept `--name=value` and `--name value`; reject duplicates, unknown options, missing values, signs/whitespace, overflows, bad IPv4 addresses, duplicate backend addresses and self-routing to the local listener.
- Preserve the legacy CLI: `./build/splice-lb 9090 127.0.0.1 9091`; Round Robin remains default and listen port `0` requests an ephemeral port.
- Bind to the specified local IPv4 address, pass the selected backlog to `listen()`, and use the selected cooldown for the monotonic-clock backend quarantine implemented in Phase 19.
- Print the effective options at startup without changing the original `Listening on ...` line used by existing test harnesses.
- Add a focused test verifying 29 rejected inputs, option syntax, loopback address isolation, binary integrity, the old CLI, and real recovery after a 200 ms failure cooldown.

## Fedora verification

Apply the Phase 25 ZIP **over Phase 24**, not a new empty directory. Keep `phase25_learning_guide.pdf` outside Git.

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase25_changes.zip
unzip -o ~/Downloads/phase25_changes.zip -d .
make clean
make
make test-config
make test
```

Expected focused ending: `PASS: Phase 25 command-line configuration and validation test suite`.
`make test` includes all earlier suites, and builds the Phase 24 test-only fault-injection library before running them.

## Manual test (three terminals)

Terminal 1, identifiable backend:

```bash
make backend-rr
```

Terminal 2, configurable proxy:

```bash
./build/splice-lb \
  --algorithm=least-connections \
  --listen-ip 127.0.0.1 \
  --backlog=256 \
  --backend-retry-ms 1200 \
  9090 127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093
```

Terminal 3, client:

```bash
./build/backend-client 127.0.0.1 9090 'hello phase 25'
```

Expected response begins `backend-0|`, `backend-1|`, or `backend-2|` depending on selection. Proxy prints `Configuration listen-ip=127.0.0.1; backlog=256; backend-retry-ms=1200; algorithm=least-connections`.

## Available flags

| Flag | Default | Range / values |
|---|---|---|
| `--algorithm` | `round-robin` | `round-robin` or `least-connections` |
| `--listen-ip` | `127.0.0.1` | numeric IPv4 address |
| `--backlog` | `128` | integer 1..4096 |
| `--backend-retry-ms` | `3000` | integer 100..60000 |
| positional listen port | required | 0..65535 (`0` = OS-selected) |
| positional backends | required | 1..32 unique `IPv4:port` |

**Important:** `--listen-ip 0.0.0.0` makes the listener reachable on all IPv4 interfaces, subject to firewall rules. For development, prefer `127.0.0.1`. Linux may cap the requested listen backlog using `/proc/sys/net/core/somaxconn`. The retry interval is a quarantine after TCP connection failure, not a connection timeout (Phase 26 adds timeouts).

## Inspection commands and interpretation

```bash
./build/splice-lb --help
ss -ltn '( sport = :9090 )'
sysctl net.core.somaxconn
strace -f -e trace=socket,bind,listen,connect,getsockopt \
  ./build/splice-lb --backlog 256 9090 127.0.0.1:9091
```

Run `strace` instead of a second proxy instance; only one proxy should bind port 9090 at a time. `ss` shows local listener and queue information; effective queue capacity can differ from user-space configured backlog. `sysctl` reports the kernel's listen-backlog cap (not a command to change it).

**New/changed files:** `src/main.c`, `tests/test_cli_config.py`, `Makefile`, `README.md`, `progress.md`. No PDF in the ZIP or Git repository.

---

# Phase 24 - Safe Connection Lifetime and Partial Setup Failures

Fedora Linux | C11 | nonblocking TCP | epoll | splice() | apply on top of **Phase 23**.

## What changed

- Add an explicit `CONN_INITIALIZING -> CONN_ACTIVE -> CONN_CLOSING` ownership lifecycle.
- Keep a new connection private until **both** socket registrations have succeeded. Only then does it enter the live linked list and claim a backend-active slot.
- Reject queued/stale epoll events unless the fd+generation token matches a registered endpoint **and** the owner is still `CONN_ACTIVE`.
- Mark teardown `CONN_CLOSING` before unregistration, clearing fd lookup slots and closing sockets and both pipe pairs.
- Log the failing setup stage and roll back an unpublished candidate without ever decrementing an active slot it did not acquire.
- Introduce a test-only `LD_PRELOAD` interposer injecting one `EMFILE` at one of five stages: backend socket, first pipe, second pipe, client epoll registration, or backend epoll registration. The fault shim is **never** linked into the production proxy.
- Verify the proxy keeps listening and forwards the next 12 KiB client request exactly after each injected failure. Check descriptor return to baseline using `/proc/<pid>/fd` and verify graceful cleanup.

## Fedora verification

Extract into your **Phase 23** repository, keeping the personal PDF outside Git:

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase24_changes.zip
unzip -o ~/Downloads/phase24_changes.zip -d .
make clean
make
make test-setup-failures
make test
```

Expected focused test output:

```text
PASS: backend-socket rollback releases fds and recovers on next client
PASS: pipe-first rollback releases fds and recovers on next client
PASS: pipe-second rollback releases fds and recovers on next client
PASS: epoll-client rollback releases fds and recovers on next client
PASS: epoll-backend rollback releases fds and recovers on next client
PASS: Phase 24 safe connection lifetime and partial setup test suite
```

`make test` builds the test-only `build/setup-faults.so` and executes all Phase 2-24 suites. The normal `make` target builds only the production proxy and its demo utilities, not the interposer.

## Manual verification - three terminals

```bash
# Terminal 1: persistent backend
make backend-multi
# Terminal 2: proxy
make run
# Terminal 3: ordinary client
./build/backend-client 127.0.0.1 9090 "hello phase 24"
```

Expected echoed reply: `hello phase 24`. A normal completion logs `connection-completion=graceful` and `transfer-integrity=verified`. Use `make test-setup-failures` to prove actual injected errors, since the ordinary demo does not intentionally fail setup.

## How the failure test works

The `tests/setup_faults.c` shared library is loaded **only in the test proxy subprocess** through `LD_PRELOAD`. It intercepts Linux `socket()`, `pipe2()`, and `epoll_ctl(EPOLL_CTL_ADD)` and fails one call with `EMFILE` (simulated fd exhaustion); every later call passes through to the kernel. An isolated test process is created per stage so counters cannot carry over. `tests/test_setup_failures.py` confirms injection actually occurred through a temporary marker, then checks that the proxy returns to its idle fd baseline and successfully serves the next client.

`LD_PRELOAD` usually works in a normal unprivileged Fedora shell. It is ignored in some secure-execution contexts; tests will fail explicitly if the injection marker is absent. Do **not** globally set `LD_PRELOAD` in your interactive shell.

## Useful Linux commands

```bash
PID=$(pidof splice-lb)
ls -l /proc/$PID/fd
ss -tan '( sport = :9090 or dport = :9090 )'
strace -f -e trace=socket,pipe2,epoll_ctl,close \
  ./build/splice-lb 9090 127.0.0.1 9091
```

Trace the proxy **instead of** `make run`, not simultaneously, to avoid port conflicts. `strace` changes timing and is for debugging, not performance measurement.

**Changed/new files:** `src/main.c`, `tests/setup_faults.c`, `tests/test_setup_failures.py`, `Makefile`, `README.md`, `progress.md`.

---

# Phase 23 - Abrupt Disconnects and Centralized Cleanup

Fedora Linux | C11 | epoll + splice | apply on top of **Phase 22**.

## What changed

- Treat fatal `EPOLLERR` as a connection-local failure and examine `SO_ERROR` on ready TCP sockets. **Do not** treat `EPOLLHUP` or `EPOLLRDHUP` alone as fatal: clean FINs must still drain through the existing half-close state machine.
- Record a named teardown reason (`graceful`, `client-abort`, `backend-abort`, `backend-connect-failure`, `internal-error`, `server-shutdown`). The original Phase 22 accounting and completion logs are retained for regression compatibility.
- Route all *listed* connection termination paths through `remove_connection(server, connection, reason)` and share `release_connection_resources()` with partial setup failure cleanup.
- Unregister both endpoints from epoll, clear fd lookup slots, close both sockets and both pipe pairs, decrement per-backend and total active counts once, and free the connection.
- Retain the fd+generation event token check: `epoll_wait()` can return a batch containing events referring to a connection already freed by a previous event in the batch.
- Add real TCP RST tests using `SO_LINGER` with zero timeout on loopback, verify `/proc/<pid>/fd` returns to baseline, check listener survival, and perform a byte-exact healthy request afterward.

## Fedora verification

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase23_changes.zip
unzip -o ~/Downloads/phase23_changes.zip -d .
make clean
make
python3 tests/test_abrupt_disconnects.py
make test
```

Expected focused output:

```text
PASS: client-rst closes one session, releases all fds, listener serves next client
PASS: backend-rst closes one session, releases all fds, listener serves next client
PASS: repeated abrupt disconnects, stale events ignored, recovery byte-exact
PASS: Phase 23 abrupt disconnects and centralized cleanup test suite
```

The prior Phase 2-18 suites passed in the execution environment before the combined command hit a time limit; the Phase 19-23 suites were subsequently run separately and passed. Always rerun `make test` on Fedora.

## Manual test - three terminals

```bash
# Terminal 1: persistent backend
make backend-multi
# Terminal 2: multi-client proxy
make run
# Terminal 3: ordinary request
./build/backend-client 127.0.0.1 9090 'phase 23 healthy request'
```

Expected echoed reply: `phase 23 healthy request`. Healthy completion prints `connection-close-reason=graceful` and `transfer-integrity=verified`. To force actual client/backend RST (rather than an orderly FIN), use `python3 tests/test_abrupt_disconnects.py`.

## Inspect Linux behavior

```bash
# Inspect TCP socket states (while the process is running)
ss -tan '( sport = :9090 or dport = :9090 )'
# Inspect a running proxy's file descriptors
PID=$(pidof splice-lb)
ls -l /proc/$PID/fd
# Restart the proxy under strace to see errors and resource cleanup
strace -f -e trace=epoll_wait,epoll_ctl,splice,getsockopt,close \
  ./build/splice-lb 9090 127.0.0.1 9091
```

`pidof` finds the running process id; `/proc/$PID/fd` lists open descriptor symlinks. Run the traced proxy *instead of* `make run` (the same listen port cannot normally be bound twice). `strace` perturbs timings and is a debugging aid, not a benchmark. The accompanying learning PDF is personal study material and is not part of the source ZIP or Git repository.

**Changed/new files:** `src/main.c`, `tests/test_abrupt_disconnects.py`, `Makefile`, `README.md`, `progress.md`.

---

# Phase 22 - Backend Half-Close and Connection Completion

Fedora Linux | C11 | Linux `epoll` and `splice()` | apply on top of **Phase 21**.

## What changed

- Add backend-side lifecycle states `BACKEND_FIN_OPEN`, `BACKEND_FIN_EOF_OBSERVED`, and `BACKEND_FIN_FORWARDED`. These are independent of `CLIENT_FIN_*` states introduced in Phase 21.
- Observe `EPOLLRDHUP` for either endpoint as a *hint*, but only regard `splice(socket -> pipe) == 0` as an actual source EOF.
- Defer forwarding backend FIN to the client (`shutdown(client_fd, SHUT_WR)`) until every queued backend response byte has drained from both the in-memory staging queue and the kernel pipe.
- Introduce `ready_for_clean_completion()`: clean teardown requires **both FINs forwarded**, **both source EOFs**, **both queues empty**, and passing accounting invariants. Backend FIN alone never discards an unfinished client upload.
- Print `backend-eof-state=observed`, `backend-eof-state=forwarded`, `backend-eof-final`, and `connection-completion=graceful` diagnostics; preserve all earlier diagnostic lines.
- Add `tests/test_backend_eof.py` for an early backend FIN followed by a 2 MiB client upload (with empty and nonempty backend replies) and for 4 MiB response delivery to a delayed client reader.

## Fedora verification

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase22_changes.zip
unzip -o ~/Downloads/phase22_changes.zip -d .
make clean
make
make test
python3 tests/test_backend_eof.py
```

Expected focused test output:

```text
PASS: backend FIN first, reply=530 bytes, then 2 MiB upload intact
PASS: backend FIN first, reply=0 bytes, then 2 MiB upload intact
PASS: delayed client receives full 4 MiB response after upload FIN
PASS: Phase 22 backend half-close and graceful connection completion test suite
```

The existing Phase 2-17 suites and focused Phase 18-22 tests were verified in the execution environment; the uninterrupted `make test` run was stopped by its execution time limit, so also run it directly on Fedora.

## Manual test - three terminals

```bash
# Terminal 1
make backend-multi
# Terminal 2
make run
# Terminal 3
./build/backend-client 127.0.0.1 9090 'hello phase 22'
```

Expected client reply: `hello phase 22`. The proxy completion log should include `client-eof-final=forwarded`, `backend-eof-final=forwarded`, `connection-completion=graceful`, and `transfer-integrity=verified`. For backend-FIN-first ordering use `python3 tests/test_backend_eof.py` instead of the ordinary echo backend.

## Observe Linux behavior

```bash
ss -tn '( sport = :9090 or dport = :9090 )'
strace -f -e trace=splice,shutdown,epoll_ctl,epoll_wait \
  ./build/splice-lb 9090 127.0.0.1 9091
```

`ss` shows TCP connection states (including half-close transitions). `strace` observes actual system calls but changes timing; it is not a performance benchmark. Keep the accompanying PDF outside the Git repository.

**Phase 22 changed/new files:** `src/main.c`, `tests/test_backend_eof.py`, `Makefile`, `README.md`, `progress.md`.

---

# Phase 21 - EOF Tracking and Client Half-Close

Fedora Linux / C11 / `epoll` / `splice()`.
Apply the Phase 21 changed-files ZIP **on top of Phase 20**. This ZIP contains only the new/changed repository files. Keep the companion PDF outside Git.

## What changed

- Track the TCP client half-close with distinct `CLIENT_FIN_OPEN`, `CLIENT_FIN_EOF_OBSERVED`, and `CLIENT_FIN_FORWARDED` states. A `recv()`/`splice()` return value of zero establishes a logical EOF. `EPOLLRDHUP` is a hint, not proof that all queued data has been consumed.
- Monitor `EPOLLRDHUP` only while the input side is eligible for reading. Avoid registering input readiness while the per-direction staged/pipe queue is backpressured.
- Do not forward the client FIN until all staged bytes and kernel-pipe bytes have drained and the backend TCP connection is `READY`. Forward FIN using `shutdown(backend_fd, SHUT_WR)`, retaining the backend-to-client receive path.
- Log each client EOF transition exactly once and expose whether FIN forwarding had to wait for a backend connection or outstanding data. Log `client-eof-final=forwarded` on a cleanly finished connection.
- `tests/test_client_eof.py` covers an immediate zero-byte half-close before the backend handshake is confirmed, an 8 KiB staged upload and FIN during connection setup, and a 4 MiB upload to a delayed reader. Each tests complete response delivery and correct final state.
- Existing load-balancing choices, the 64 KiB directional queue limit, and `splice()` forwarding semantics are unchanged.

## Fedora verification

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase21_changes.zip
unzip -o ~/Downloads/phase21_changes.zip -d .
make clean
make
make test
python3 tests/test_client_eof.py
```

Expected focused output:

```text
PASS: client FIN deferred safely (delayed-connect, payload=0, slow-backend=False)
PASS: client FIN deferred safely (delayed-connect, payload=8192, slow-backend=False)
PASS: client FIN deferred safely (normal, payload=4194304, slow-backend=True)
PASS: Phase 21 client EOF and deferred FIN lifecycle test suite
```

The full Phase 2-21 regression suite also passed. The test uses only Python 3, a C compiler, Linux loopback, and local sockets.

## Manual verification - 3 terminals

```bash
# Terminal 1
make backend-multi
# Terminal 2
make run
# Terminal 3
./build/backend-client 127.0.0.1 9090 'hello phase 21'
```

The client prints `hello phase 21`. After cleanup the proxy log should contain `client-eof-state=observed`, `client-eof-state=forwarded`, `client-eof-final=forwarded`, and `transfer-integrity=verified`. The small interactive test does not guarantee the deferred path; run `python3 tests/test_client_eof.py` for that.

## Linux inspection and debugging

```bash
ss -tn '( sport = :9090 or dport = :9090 )'
strace -f -e trace=splice,recvfrom,shutdown,epoll_ctl,epoll_wait \
  ./build/splice-lb 9090 127.0.0.1 9091
```

Only run one proxy listening on port 9090 at once. `strace` affects timing and is useful for correctness inspection, not throughput benchmarking. The companion learning PDF explains the half-close TCP state machine, `EPOLLRDHUP`, EOF, `shutdown()`, and every command.

**Files changed in Phase 21:** `src/main.c`, `tests/test_client_eof.py`, `Makefile`, `README.md`, and `progress.md`. Do not add the PDF to the repository.

---

# Splice Load Balancer - Phase 20: Bidirectional Backpressure

Fedora Linux | C11 | Linux `epoll` + `splice()` | Apply this changed-files archive over **Phase 19**.

## What changed in Phase 20

- Independently track each forwarding direction's backpressure (`source_paused`) and pause/resume transitions.
- Track maximum queued bytes in either direction, including transient pipe occupancy before an immediate drain.
- Enforce a **64 KiB max managed queue per direction** (`pending` preconnect bytes + `pipe_pending`). TCP kernel send/receive buffers are outside this limit.
- Preserve the Phase 14 policy: turn **source EPOLLIN off** while any byte remains queued; arm **destination EPOLLOUT** only until that queue drains. The event loop never waits on a slow socket.
- New `tests/test_backpressure_duplex.py` stalls both receivers simultaneously, verifies both endpoint `EPOLLOUT` masks appear at once, then unblocks them and checks 6 MiB of binary data in *each* direction, plus backpressure metrics and clean teardown.
- No changes to balancing algorithms, backend recovery policy, TCP half-close semantics, or the steady-state `splice()` path.

## Apply and verify on Fedora

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase20_changes.zip
unzip -o ~/Downloads/phase20_changes.zip -d .
make clean
make
make test
# Focused test (also included in make test):
python3 tests/test_backpressure_duplex.py
```

Expected focused output:

```text
PASS: simultaneous two-way backpressure (6 MiB per direction), both EPOLLOUT watches armed
PASS: independent pause/resume counters, bounded queues <= 65536 bytes, and byte-exact delivery
PASS: Phase 20 bidirectional backpressure test suite
```

A successful connection teardown now prints a diagnostic line resembling:

```text
backpressure-pauses=.../...; backpressure-resumes=.../...; backpressure-peak=.../... bytes; queue-cap=65536 bytes
transfer-integrity=verified; ...
```

The counter order is **client-to-backend / backend-to-client**. Counts vary with timing, CPU scheduling, and socket buffer sizes. The stress test asserts positive pauses/resumes in both directions and verifies each peak is within the configured cap, but never assumes one fixed count.

## Manual three-terminal loopback check

```bash
# Terminal A
make backend-multi
# Terminal B
make run
# Terminal C
./build/backend-client 127.0.0.1 9090 "hello phase 20"
```

Expected client output: `hello phase 20`. This tiny request validates the baseline relay but is **not** a backpressure test; run the 6 MiB focused test to induce both directions of stalled output.

## Fedora inspection commands

```bash
ss -ltn '( sport = :9090 or sport = :9091 )'
PID=$(pgrep -n splice-lb)
ls -l /proc/$PID/fd
strace -f -e trace=splice,epoll_ctl,epoll_wait,shutdown \
  ./build/splice-lb 9090 127.0.0.1 9091
```

Do not run `make run` and `strace` on the same listening port at the same time. Trace changes timing; **do not** benchmark while tracing. The personal learning PDF describes commands, TCP flow control, the source/pipe/destination state machine, buffering and limitations. Do not commit the PDF.

**Phase 20 changed files:** `src/main.c`, `tests/test_backpressure_duplex.py`, `Makefile`, `README.md`, `progress.md`.

---

# Splice Load Balancer - Phase 18: Algorithm Configuration and Distribution Comparison

Fedora Linux / C11 / `epoll` / `splice()`. **Apply this Phase 18 changed-files ZIP over Phase 17.**

## New in Phase 18

- Accept both `--algorithm NAME` and `--algorithm=NAME`, with `round-robin` and `least-connections` as the only supported names; default remains Round Robin.
- `./build/splice-lb --help` prints usage and exits successfully. Malformed options, duplicate algorithm selectors, invalid backend specifications and unknown algorithms fail with `Usage:` on stderr.
- `tests/test_algorithm_comparison.py` starts three real identifiable TCP backends, creates a controlled 2/2/0 active-load imbalance, and compares the **next three** placements under both algorithms.
- Tests assert the selected backend, echoed bytes, completed-session accounting and final active load; `--csv /tmp/phase18-distribution.csv` optionally exports all six probe choices for personal analysis outside the repository.
- Phase 18 does not add health checks, weighted balancing or benchmark throughput. Results are a deterministic demonstration of active-connection fairness on this test workload, not proof of performance wins.

## Fedora instructions

```bash
cd /path/to/splice-load-balancer
unzip -o ~/Downloads/phase18_changes.zip -d .
make clean
make
make test
python3 tests/test_algorithm_comparison.py
python3 tests/test_algorithm_comparison.py --csv /tmp/phase18-distribution.csv
cat /tmp/phase18-distribution.csv
```

Expected focused test output (backend ports are chosen dynamically):

```text
PASS: --help and 7 invalid configuration cases
PASS: round-robin: probes=[0, 1, 2]; active={0: 3, 1: 3, 2: 1}; 9 sessions byte-exact
PASS: least-connections: probes=[2, 2, 0]; active={0: 3, 1: 2, 2: 2}; 9 sessions byte-exact
PASS: Least Connections improves active-load spread over Round Robin under skew
PASS: Phase 18 configuration and algorithm comparison test suite
```

## CLI examples

```bash
./build/splice-lb --help
./build/splice-lb 9090 127.0.0.1 9091
./build/splice-lb 9090 127.0.0.1:9091 127.0.0.1:9092
./build/splice-lb --algorithm round-robin 9090 127.0.0.1:9091 127.0.0.1:9092
./build/splice-lb --algorithm=least-connections 9090 127.0.0.1:9091 127.0.0.1:9092
```

Manual test in three terminals:

```bash
# Terminal 1: start three tagged backend servers
make backend-rr
# Terminal 2: start the load balancer
./build/splice-lb --algorithm=least-connections 9090 \
  127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093
# Terminal 3: issue a TCP request
./build/backend-client 127.0.0.1 9090 'hello phase 18'
```

A short request should be returned with a `backend-N|` prefix, but short sequential requests will often look identical under both algorithms. **Use the focused test to observe how active-load imbalance changes selection.**

## Meaning of the comparison

After six held-open sessions both algorithms have active counts `[2,2,2]`. Closing the two sessions on backend 2 yields `[2,2,0]`. Round Robin cycles independently of active load and selects backend IDs `[0,1,2]`, producing `[3,3,1]`. Least Connections selects `[2,2,0]`, producing `[3,2,2]` with the rotating tie-breaker. The active count spread (`max-min`) is 2 versus 1 in this reproducible scenario. This is a **connection-count** metric, not a latency, request-rate or throughput benchmark.

## Debug commands

```bash
ss -ltn '( sport = :9090 or sport = :9091 or sport = :9092 or sport = :9093 )'
strace -f -e trace=connect,getsockopt,epoll_ctl,epoll_wait,splice \
  ./build/splice-lb --algorithm=least-connections 9090 \
  127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093
```

`ss` shows kernel sockets, not per-backend scheduler state. Use the proxy's `Selected backend[...]` logs and automated tests for selection. `strace` can introduce significant overhead; never benchmark throughput under tracing.

**Changed files:** `src/main.c`, `tests/test_algorithm_comparison.py`, `Makefile`, `README.md`, `progress.md`. The companion learning PDF is personal material: **do not add it to Git**.

---

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
