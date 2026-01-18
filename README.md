# Phase 33 - Sanitizer Checks and Resource Leak Detection

**Fedora Linux | C11/GCC | Linux epoll/splice | apply over Phase 32**

## Changes

- `src/main.c`: a compile-time **test-only** `SANITIZER_TEST_EXIT` branch lets the
  instrumented proxy exit normally *after* full shutdown cleanup so
  LeakSanitizer can run its process-exit hooks. The production binary retains
  the original SIGINT/SIGTERM exit status and behavior.
- `Makefile`: `sanitize-build`, `test-sanitizers` and `test-fd-leaks`. The
  instrumented executables are isolated under `build/sanitize/` and never
  replace `build/splice-lb`.
- `tests/test_sanitizers.py`: ASan/UBSan-instrumented `splice` demo and a real
  TCP proxy test (16 concurrent binary sessions, 12 resets, recovery, clean
  SIGTERM drain, LeakSanitizer check on normal return).
- `tests/test_fd_leaks.py`: 72 successful sessions plus 36 intentional TCP
  resets; compare `/proc/<pid>/fd` totals/socket/pipe counts after each wave,
  and verify another request succeeds.
- `tests/harness.py`: treats expected `ConnectionResetError`/`BrokenPipeError`
  from deliberately reset test clients as normal fixture termination.
- The regular `make test` includes the lightweight descriptor-lifetime test.
  Sanitizers remain an explicit opt-in because they slow execution and require
  compiler runtime libraries.

## Fedora setup and verification

```bash
cd /path/to/splice-load-balancer
unzip -o ~/Downloads/phase33_changes.zip -d .
make clean
make
make test-fd-leaks
make sanitize-build
make test-sanitizers
make test
```

If packages are missing, on Fedora use `sudo dnf install gcc make python3`
(and `sudo dnf install valgrind` optionally). `make test-sanitizers` uses GCC's
`-fsanitize=address,undefined` and tests the *sanitized build*, not the regular
production executable. It turns on LeakSanitizer via `ASAN_OPTIONS`.

Expected focused endings:

```
PASS: Phase 33 /proc file descriptor lifetime suite
PASS: sanitizer-instrumented splice demo 512 KiB+, byte exact
PASS: 16 concurrent ASan/UBSan TCP forwarding sessions, byte exact
PASS: 12 reset clients; sanitized proxy remains operational
PASS: sanitizer proxy returned cleanly; ASan/UBSan/LSan reported no errors
PASS: Phase 33 ASan/UBSan/LeakSanitizer suite
```

The C proxy still requires Linux. Tests create loopback TCP endpoints and
require no root privileges. Tests write temporary files under `/tmp` and
remove them. Keep the PDF **outside the repository**.

## Interpreting the result

- **ASan** catches many out-of-bounds and use-after-free errors along exercised
  code paths. **UBSan** checks certain undefined C operations. **LSan** checks
  reachable allocation leaks at normal program exit. Zero reports do not prove
  absence of memory bugs or data races.
- Sanitized builds in this phase use `-fno-pie`/`-no-pie` to avoid shadow-memory
  mapping collisions observed in some ASan Linux/container environments. This
  is **test-only**: the regular production build is unchanged.
- LeakSanitizer does **not** check file-descriptor ownership. `/proc/<pid>/fd`
  snapshot checks test descriptor counts after connections drain. Stable counts
  are valuable evidence but cannot prove there are no short-lived or offsetting
  leaks. The 64 KiB application/pipe queue budget is not the same as memory
  held in TCP kernel socket buffers.
- Phase 33 is **correctness and resource safety**, not a throughput benchmark.
  Never compare sanitized speed with the optimized production binary.

## Troubleshooting

```bash
ulimit -n
ls -l /proc/$(pidof splice-lb)/fd
ss -tan '( sport = :9090 )'
ASAN_OPTIONS=help=1 ./build/sanitize/splice-lb --help
```

ASan/UBSan failures cause nonzero test status and show diagnostics in the
report. If `make test` hits the surrounding execution time limit, run its
individual Python suites or rerun on Fedora. Do not claim an uninterrupted
suite pass unless it completed.

**Commit date:** 2026-12-17 21:47:26 +05:30.
**Changed files:** `src/main.c`, `tests/harness.py`, `tests/test_sanitizers.py`,
`tests/test_fd_leaks.py`, `Makefile`, `README.md`, `progress.md`.

---

# Phase 31 - Round Robin and Least Connections Stress Tests

**Fedora Linux | Python 3 | C11 proxy, epoll and splice() | apply over Phase 30**

## What's new

- `tests/test_algorithm_stress.py` compares the real TCP load balancer's Round Robin and Least Connections strategies under **the same uneven connection-lifetime schedule**. No production C code is modified.
- Starts three tagged echo backends, uses one fresh proxy process per algorithm, verifies exact binary echoes, then leaves TCP sessions open so they count as active.
- Establishes 24 initial sessions, selectively completes 6/6/6/12 sessions on chosen backends, adds four sequential arrival waves (18/18/18/24), and finishes with **30 concurrent arrivals**.
- Tests **132 actual connections per algorithm** (264 total), reaching **102 simultaneously held-open TCP sessions** for each algorithm. Confirms all 132 sessions complete without missing data and all backend-active slots return to zero.
- Computes `active_spread = max(active connections) - min(active connections)` at each checkpoint. In the controlled workload, Least Connections has a lower spread at four uneven-lifetime checkpoints. This is **not** a proof of better throughput, lower latency, or universal algorithm superiority.
- Adds `make test-algorithm-stress` and includes the new check in the regular `make test` suite. Optional JSON/CSV outputs are written to paths you choose; keep reports outside your Git repository.

## Fedora installation and verification

```bash
cd /path/to/splice-load-balancer
unzip -l ~/Downloads/phase31_changes.zip
unzip -o ~/Downloads/phase31_changes.zip -d .
make clean
make
make test-algorithm-stress
make test
```

Expected focused ending:

```text
PASS: round-robin: 132 byte-exact sessions, 102 held at peak, all slots released
PASS: least-connections: 132 byte-exact sessions, 102 held at peak, all slots released
PASS: Least Connections has smaller active-load spread than Round Robin at four unequal-lifetime checkpoints
PASS: Phase 31 Round Robin and Least Connections stress test suite
```

To save measurements **outside the repository**:

```bash
python3 tests/test_algorithm_stress.py \
  --json /tmp/phase31-distribution.json \
  --csv /tmp/phase31-distribution.csv
column -s, -t /tmp/phase31-distribution.csv | head -15
python3 -m json.tool /tmp/phase31-distribution.json | head -60
```

The test chooses loopback ports automatically. Do **not** run a separate `make backend-rr` or `make run` during automated verification. If the test fails, the process is stopped and backend listener sockets are released.

## Measured scenario from the Linux test environment

| Checkpoint | Round Robin active backend counts | Least Connections counts | Spread RR / LC |
|---|---|---|---|
| 24-connection baseline | 8 / 8 / 8 | 8 / 8 / 8 | 0 / 0 |
| backend 2 loses six sessions, add 18 | 14 / 14 / 8 | 12 / 12 / 12 | 6 / 0 |
| backend 1 loses six sessions, add 18 | 20 / 14 / 14 | 16 / 16 / 16 | 6 / 0 |
| backend 0 loses six sessions, add 18 | 20 / 20 / 20 | 20 / 20 / 20 | 0 / 0 |
| backend 2 loses 12 sessions, add 24 | 28 / 28 / 16 | 24 / 24 / 24 | 12 / 0 |
| 30 concurrent incoming connections | 38 / 38 / 26 | 34 / 34 / 34 | 12 / 0 |

**Interpretation:** connection-level Least Connections responds to *current live connections*, whereas Round Robin rotates over arrival count irrespective of lifetime. Active connection count is an imperfect proxy for work: a single bandwidth-heavy stream may use more resources than many idle streams. Phase 34 handles controlled latency/throughput benchmarking.

## Commands and diagnostics

- `make clean`: deletes generated binaries in `build/`.
- `make`: strict-warning compile of C proxy and helper programs.
- `make test-algorithm-stress`: launches the standalone real-TCP stress comparison.
- `make test`: runs Phase 31 plus all previous regression tests; the complete run can take several minutes.
- `ss -tan '( sport = :9090 or dport = :9090 )'`: inspect manual TCP connections (automated tests use ephemeral ports).
- `ls /proc/$(pidof splice-lb)/fd | wc -l`: count live proxy descriptors for a manually started proxy.
- `strace -f -e trace=connect,epoll_wait,splice ./build/splice-lb ...`: inspect syscalls when debugging; **do not** use `strace` for throughput measurements.
- The JSON/CSV reports are **test artifacts**, not production server metrics.

**Changed files:** `tests/test_algorithm_stress.py`, `Makefile`, `README.md`, `progress.md`. Keep `phase31_learning_guide.pdf` outside the repository and out of Git.

---

# Phase 28 - Reusable Test Harness and Basic TCP Payload Tests

Fedora Linux | Python 3 standard library | `epoll` + `splice()` proxy | **apply on top of Phase 27**.

## What is new

- `tests/harness.py`: reusable context-manager fixtures for a loopback TCP echo backend and for the proxy process. Both request kernel-assigned ports so tests can run alongside other services.
- The proxy's stdout/stderr is captured to a *temporary regular file*, not an undrained pipe. This avoids filling a subprocess pipe and stalling the proxy during verbose/concurrent tests.
- Test resources are cleaned up automatically: the backend server is shut down and joined, and the proxy receives SIGTERM, then SIGKILL as a bounded fallback if necessary.
- Add `deterministic_bytes()` for reproducible binary payloads, `assert_bytes_equal()` with first mismatched offset and SHA-256 diagnostics, and a full-duplex `request()` helper with fragmented sends and concurrent receives.
- `tests/test_harness.py`: negative controls deliberately truncate/modify payloads and confirm the comparison helper *rejects* them; verifies fixture startup and teardown.
- `tests/test_basic_payloads.py`: 16 payload cases including empty, single-byte, UTF-8, embedded NUL/0xff, 8 KiB and 64 KiB boundary cases, and a >256 KiB transfer. Adds 12 overlapping clients with exact byte checks.
- Existing C forwarding and load-balancing implementation is unchanged; this phase improves verification infrastructure, not claimed throughput.

## Fedora verification

```bash
cd /path/to/splice-load-balancer
unzip -o ~/Downloads/phase28_changes.zip -d .
make clean
make
make test-basic   # new self-test and basic payload integration suite
make test         # entire regression suite, including Phase 28
```

Expected focused ending:

```text
PASS: Phase 28 reusable harness self-test suite
PASS: 16 payload cases, 492080 bytes, including empty, UTF-8, binary and buffer boundaries
PASS: 12 simultaneous-client workloads, 239538 byte-exact bytes
PASS: Phase 28 basic payload test suite
```

To run the Python tests individually:

```bash
python3 tests/test_harness.py
python3 tests/test_basic_payloads.py
```

Verification in the Linux execution environment: the focused Phase 28 tests passed, the previous Phase 2-21 tests passed in the full run, and Phases 23-27 passed individually. A Phase 22 cleanup-timing assertion failed once in the full run, failed on its first isolated retry, and passed on a later isolated retry. Repeat the uninterrupted suite on Fedora and investigate any recurrence.

The tests are self-contained: each starts its own temporary backend and proxy. Do not start `make backend-rr` or `make run` for these two tests. They use only Python's standard library and ordinary user permissions.

## How the new harness works

1. `echo_backend()` starts an echo server listening on `127.0.0.1:0` in a background thread, yields the allocated port, then shuts down and joins the thread.
2. `proxy_session(backend_port)` starts `build/splice-lb 0 127.0.0.1:PORT`, polls its startup log with `time.monotonic()` for a bounded time, and yields the actual listening port.
3. `request(port, payload, fragment_sizes=...)` opens TCP, writes pieces on a worker thread, calls `shutdown(SHUT_WR)` after the final byte, and concurrently reads the reply until EOF.
4. `assert_bytes_equal()` compares *all bytes* and on failure reports the first different offset plus both lengths and SHA-256 hashes. The negative control verifies the assertion would actually detect corrupted data.

The 12-client parallel test verifies correctness, **not** the resume claim of supporting thousands of concurrent clients. That load target is reserved for Phase 30. Likewise, SHA-256 here helps diagnose corruption but exact byte equality is the primary correctness oracle.

## Debugging and tests

- `make clean`: removes `build/`; no source files are touched.
- `make`: compiles the C binaries with strict warnings.
- `make test-basic`: runs Phase 28 focused verification only.
- `make test`: runs all previous phase regressions plus the two new suites.
- `python3 tests/test_basic_payloads.py`: reruns the basic payload matrix alone.
- `ss -ltn '( sport = :9090 )'`: shows a manually started listener on port 9090; Phase 28's auto-assigned port is different and appears only while the test runs.
- `strace -f -e trace=socket,connect,accept4,splice ./build/splice-lb 9090 127.0.0.1 9091`: inspects network/splice syscalls during a manually started backend+proxy demo.
- If a test fails, read the assertion's label, first mismatch offset, expected/actual lengths, and digests. Do not presume that differences in TCP `recv()` chunk sizes indicate corruption.

The personal `phase28_learning_guide.pdf` is not part of the ZIP or Git repository.

---

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


## Phase 29 - Large transfers and byte integrity

Phase 29 adds multi-megabyte streamed correctness tests **without changing**
`src/main.c`. Run on Fedora after applying the Phase 29 changed-files ZIP:

```bash
make clean
make
make test-large
make test
```

`make test-large` starts an on-demand bounded-memory streaming echo backend
and the real C proxy on automatically selected loopback ports. It verifies:

- A fragmented **16 MiB + 17 B** duplex transfer, including 1 B writes.
- A **32 MiB + 1 B** duplex transfer across repeated socket operations.
- **Four concurrent** connections carrying 8 MiB or more each.
- Independent SHA-256 of all written and returned bytes plus exact byte counts.
- No file-descriptor growth after all sessions complete.

The backend echoes as bytes arrive, rather than accumulating 32 MiB in
Python memory. The client computes SHA-256 incrementally and sends while
receiving to avoid test-induced full-duplex deadlocks. The random payload
generator uses fixed seeds for repeatability; it is not a security PRNG.
Elapsed test time is diagnostic, **not a valid performance benchmark**:
Phase 34 measures sustained throughput, CPU, and latency under controlled
conditions. Phase 30 tests 1,000-10,000 concurrent connections.

The personal learning PDF must be kept outside the repository and ZIP.

## Phase 30 - 1,000 to 10,000 concurrent TCP connections

Phase 30 adds a **test-only** selector/epoll echo backend (`tests/scalable_backend.py`)
and a scale certification harness (`tests/test_concurrency_scale.py`). The
production C proxy is **not changed**. Each client completes a byte-exact echo
probe then remains connected while the other clients are established. When all
N are simultaneously active, the harness sends further probes to four sample
clients, checks the backend's live-connection count, tests a connection above
the `--max-connections` cap, then closes everyone and verifies descriptor cleanup.

On Fedora, start with:

```bash
make clean && make
make test-scale                      # 1,000 live connections
make test-scale-5k                   # optional: 5,000, requires higher fd limits
make test-scale-10k                  # optional: 10,000, requires higher fd limits
# or choose an exact target and write JSON outside the repository:
python3 tests/test_concurrency_scale.py --connections 1000 \
  --timeout 300 --report /tmp/phase30-1k.json
cat /tmp/phase30-1k.json
```

`make test` includes a low-resource 100-client smoke run. `make test-scale`
performs the measured 1,000-client certification separately; 5,000 and 10,000
are **not silently counted as passed** and must be run explicitly on your Fedora
machine. The harness fails fast if the inherited `RLIMIT_NOFILE` soft limit is
less than approximately `6*N + 96`: the proxy uses two TCP sockets and two
pairs of pipe descriptors per active connection. The test driver and backend
need approximately N descriptors each, in their respective processes. Check:

```bash
ulimit -Sn; ulimit -Hn
cat /proc/sys/fs/file-max
cat /proc/sys/net/ipv4/ip_local_port_range
cat /proc/sys/net/core/somaxconn
```

In a shell with a sufficient hard limit, `ulimit -Sn 65535` permits a nominal
10,000-session proxy test (10,000 * 6 plus overhead). If the hard limit is
smaller, configure the user/session limits appropriately; do not silently
lower the test's requested target. Higher scales can also require significant
memory and CPU time. The test-only backend uses one selector thread instead of
one operating-system thread per client. The Python fixture writes proxy logs to
a regular file to avoid blocking the proxy on an undrained stdout pipe.

**Actual observed result in the build/test environment** (not Fedora):

```text
PASS: 1000 simultaneously live client/backend sessions; 1004 byte-exact probes
PASS: connection cap rejected overflow; fds returned to 6
OBSERVED: establish=0.58s, proxy RSS=12.5 MiB, proxy fds=6006, CPU=0.36s
PASS: Phase 30 high-concurrency test suite
```

These measurements are environment-specific, not end-to-end throughput or
p99-latency benchmarks. Do not claim 5k or 10k sessions until the corresponding
Fedora run passes. The current timeout-deadline sweep is O(active sessions) per
event-loop iteration, so Phase 34 should investigate scalability before
making any low-latency claim. Save JSON reports under `/tmp` or another
location outside Git, and keep the accompanying learning PDF outside the repo.

## Phase 32 - Adversarial TCP behavior: slow peers, FIN, resets and recovery

Phase 32 adds an **integration-test-only** adversarial suite. No production C
code is modified. Run this phase on Fedora after applying the changed-files
ZIP over Phase 31:

```bash
make clean
make
make test-adversarial      # dedicated mixed-failure test (~seconds)
make test                  # all previous test suites plus Phase 32
```

The new `tests/test_adversarial_peers.py` uses loopback ports selected by the
OS and always reaps spawned processes. It verifies three separate workloads:

1. **Simultaneous slow peers and resets:** six concurrent 1 MiB+ streamed
   requests to a throttled backend, with delayed client reads and twelve
   intentionally reset TCP sockets. After those sessions, an additional
   byte-exact request succeeds and `/proc/<pid>/fd` returns to its baseline.
2. **Backend FIN before client upload:** four parallel sessions receive a
   backend response and FIN *before* uploading between zero and 1 MiB+ bytes.
   The backend independently hashes every late upload and confirms its SHA-256.
3. **Backend loss and recovery:** a refused backend enters cooldown; five
   concurrent requests reach the healthy backend and preserve their bytes.
   When the failed address begins accepting again, the load balancer probes
   and restores it to the selectable pool.

The suite checks actual response bytes and end-state cleanup. It does not
prove freedom from every possible race, nor does a locally induced pause
reproduce arbitrary Internet conditions. For syscall inspection:

```bash
strace -f -e trace=splice,epoll_wait,epoll_ctl,shutdown,getsockopt \
  ./build/splice-lb 9090 127.0.0.1 9091
```

For resource inspection in a separate terminal:

```bash
PID=$(pidof splice-lb)
ls /proc/$PID/fd | wc -l
ss -tan '( sport = :9090 )'
```

Keep generated logs, metrics and the personal Phase 32 learning PDF outside
Git; only the five changed source/project files belong in the commit.


## Phase 34 - Reproducible throughput and latency benchmarking

**What this tests:** localhost end-to-end traffic through either the production
Linux `epoll`/`splice()` proxy or a **benchmark-only** blocking TCP proxy
(`benchmarks/blocking_baseline.c`). The baseline uses one pthread per client
and blocking `select` + `recv`/`send`; it is not part of the production build.
Both targets connect to the same selector-based Python echo backend.

**Fedora setup / verification:**

```sh
make clean
make
make test-benchmark  # smoke: 1 and 4 clients, both implementations
make test            # full regression suite; also runs benchmark smoke
make benchmark-quick # quick JSON: /tmp/phase34-quick.json
make benchmark       # 1, 8, 32 clients; 3 repetitions; JSON at /tmp/phase34-benchmark.json
```

Custom reproducible workloads (reports stay outside the repository):

```sh
python3 benchmarks/bench_forwarding.py \
  --clients 1,8,32 --requests 50 --payload-kib 1024 \
  --repetitions 5 --output /tmp/splice-benchmark.json
python3 -m json.tool /tmp/splice-benchmark.json | less
```

**Metrics:** RTT p50/p95/p99 for 64-byte echo exchanges; request rate;
aggregate application-layer **one-direction echoed goodput** in MiB/s for
byte-exact streaming uploads; per-process CPU seconds used in each workload;
`VmHWM` (peak observed high-water RSS) from `/proc`; OS/kernel and file limit
metadata. The tool checks every response's exact bytes / SHA-256.

**Interpretation:** No latency or throughput threshold is used as a test pass
condition. The Python clients, Python backend, localhost TCP, kernel scheduler,
and machine load all contribute to measured results. The baseline is not a
perfectly matched implementation; do not advertise its numbers as proof of
`splice()` speedup or extrapolate to a production NIC. Rerun on your Fedora
machine after keeping the governor, CPU load, payload, sample count, and
concurrency constant. A result for 32 clients does not certify 10,000 clients.
The test does not measure accept-to-connect latency, packet latency, or
maximum link capacity. Save and publish the Fedora environment and repeated
results before making resume performance claims.

Learning-only PDF: `phase34_learning_guide.pdf` is distributed separately;
**do not add it or benchmark JSON output to the repository.**
