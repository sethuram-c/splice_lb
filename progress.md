# Phase 31 - Algorithm Distribution Stress Tests

**Scheduled commit:** 2026-12-15 21:48:27 +05:30. **Environment:** Fedora Linux / C11 / Python 3.

- [x] Use the production proxy unchanged; add a Python integration stress test.
- [x] Run Round Robin and Least Connections in separate, fresh proxy processes against three real tagged TCP servers.
- [x] Hold sessions open across a controlled sequence of uneven session closures and subsequent arrivals.
- [x] Verify binary echo correctness for 132 connections per algorithm.
- [x] Exercise a simultaneous wave of 30 incoming TCP connections; peak 102 held-open connections per algorithm.
- [x] Confirm exact backend counts at six checkpoints and all 132 active slots released per algorithm.
- [x] Demonstrate smaller active-connection spread for Least Connections at four skewed checkpoints.
- [x] Add optional JSON and CSV exports outside the repository.
- [x] Add `make test-algorithm-stress` and integrate with `make test`.
- [x] Run focused Phase 31 test repeatedly; pass old Phase 16/17/18 algorithm tests.
- [ ] Run entire `make test` suite on Fedora after extracting Phase 31 ZIP and verify there are no local timing-sensitive regressions.
- [x] Prepare a separate personal PDF guide that is not committed to Git.

---

# Phase 28 - Test Harness and Basic Payload Tests

**Scheduled commit:** 2026-12-12 21:44:19 +05:30. **Environment:** Fedora Linux; C11/GCC; Python 3 standard library.

- [x] Add reusable loopback backend and proxy subprocess context managers.
- [x] Use kernel-assigned ephemeral ports instead of hard-coded test ports.
- [x] Avoid stdout pipe deadlock by logging proxy output to a temporary regular file.
- [x] Guarantee proxy shutdown/reap and backend shutdown/join during test cleanup.
- [x] Provide deterministic byte generation, fragmented duplex exchange, exact comparison and SHA-256 diagnostics.
- [x] Prove the oracle rejects intentionally truncated and modified response data.
- [x] Verify 16 sequential payload patterns and 12 concurrent byte-exact clients.
- [x] Add `make test-basic` and register both new suites in `make test`.
- [x] Keep personal learning PDF separate from Git and the source ZIP.
- [x] Focused Phase 28 self-tests and payload matrix passed in the Linux test environment.
- [x] Phase 2-21 regression run passed; Phase 22 had a cleanup-timing failure on one run and passed on a later individual rerun.
- [x] Phase 23-27 regression suites passed separately. Full uninterrupted run remains unverified.
- [ ] Rerun `make test-basic && make test` on Fedora before the scheduled commit.

---

# Phase 27 progress - Connection limits, logging, and graceful shutdown

- [x] Accept `--max-connections 1..100000` and `--shutdown-grace-ms 100..120000` with strict parser and help.
- [x] Reject excess TCP clients without selecting a backend, blocking the loop, or leaking descriptors.
- [x] Record operational events and per-process accepted/rejected/closed/graceful counts.
- [x] Capture SIGINT/SIGTERM using signalfd, prioritize shutdown readiness before accepts in a single epoll batch.
- [x] Stop listener, drain established connections during the grace period; force centralized teardown at deadline.
- [x] Preserve earlier test-harness signal exit status after safe cleanup, log completion before terminating.
- [x] Update old epoll-count assertions and test-only fault injector to include control-plane signalfd.
- [x] Add deterministic Phase 27 automated tests for limit, recovery, drain, force close, idle signal, and invalid flags.
- [x] Store personal learning PDF separate from source repository.

---

# Phase 26 - Backend Connection and Idle Timeouts

**Planned commit:** 2026-12-10 21:58:34 +05:30. **Status:** implemented and tested on Linux; verify on Fedora before committing.

- [x] Parse and validate `--connect-timeout-ms` and `--idle-timeout-ms` with defaults 5000 and 60000 ms.
- [x] Track monotonic handshake start separately from last successful byte progress.
- [x] Compute nearest session deadline and feed it to epoll_wait without busy polling when idle.
- [x] Retire stalled handshakes (backend cooldown) and idle sessions (connection-only cleanup).
- [x] Preserve Phase 23/24 centralized resource teardown and fd-generation protection.
- [x] Add test-only EPOLLOUT suppression to verify deterministic connection timeout.
- [x] Test idle expiration, periodic traffic, successful recovery, strict CLI bounds.
- [x] Pass Phase 2-26 tests in batches with strict C11 warnings; repeat `make test` on Fedora.
- [x] Keep the in-depth learning PDF outside the source ZIP and Git repository.

---

# Phase 25 - Command-Line Configuration and Validation

**Planned commit:** 2026-12-09 21:52:18 +05:30. **Status:** implemented and validated in Linux test environment; verify on Fedora before committing.

- Added configurable listen IPv4, listen backlog, backend retry cooldown; preserved algorithm selection and legacy CLI.
- Strict numeric, IPv4, repeated-flag, duplicate-backend, and self-routing validation.
- Build: `make clean && make`. Focused tests: `make test-config`.
- Verified 29 invalid cases, `127.0.0.2` bind isolation, legacy API, binary integrity, and 200 ms recovery.
- Earlier regression suites Phase 2-23 passed in batches. Phase 24 fault injection passed after `make build/setup-faults.so`, and Phase 25 tests passed. Run `make test` on Fedora.
- Learning guide PDF is personal learning material outside the repository.

---

## Phase 22 - Backend half-close and connection completion (2026-12-06)

- Distinguish observed backend EOF from FIN forwarded to the client. Treat `EPOLLRDHUP` only as a readiness hint; `splice() == 0` confirms source EOF.
- Preserve an active client upload after the backend closes its write half; never close the entire session on a single FIN.
- Enforce graceful teardown only after both FINs propagate, pending data drains in both directions, and forwarding/accounting invariants are satisfied.
- Add three deterministic backend EOF tests: 530-byte response then 2 MiB late upload; empty response then 2 MiB late upload; 4 MiB backend response to a delayed reader after a 1 MiB client upload.
- Verified clean C11 build, Phases 2-17 regression tests, and Phase 18-22 focused suites; combined full run stopped at time limit during Phase 18.
- Commit (scheduled): `feat: complete backend half-close and TCP session teardown`.

# Project progress

- [x] Phase 1 - Project setup and TCP socket creation
  - [x] Strict GCC build flags and reproducible Makefile
  - [x] `socket(AF_INET, SOCK_STREAM, 0)` and descriptor cleanup
  - [x] Initial socket lifecycle smoke test
- [x] Phase 2 - Bind, listen, and accept a client
  - [x] Bind an IPv4 TCP listener on `127.0.0.1` (default port `9090`)
  - [x] Support port `0` for OS-selected ephemeral ports
  - [x] Enter `LISTEN` state and accept one TCP client
  - [x] Report connecting client's address and close both descriptors
  - [x] Validate CLI port input, occupied port errors, and normal operation
  - [x] Automated real-socket tests via `make test`
- [x] Phase 3 - TCP echo and backend client
  - [x] Echo arbitrary byte payloads until TCP write-side EOF
  - [x] Handle interrupted syscalls and partial sends
  - [x] Use `MSG_NOSIGNAL` to avoid socket-send SIGPIPE termination
  - [x] Add a separate IPv4 backend client with `connect()` and half-close
  - [x] Verify empty, text, binary and 128 KiB messages end to end
  - [x] Preserve and rerun the Phase 2 listener regression suite
- [x] Phase 4 - First blocking proxy and resource cleanup
  - [x] Preserve previous echo program as `build/echo-backend`
  - [x] Add client -> proxy -> backend -> proxy -> client forwarding
  - [x] Use `select()` for bidirectional read readiness with blocking sockets
  - [x] Implement send loops and propagate EOF with `shutdown(SHUT_WR)`
  - [x] Centralize socket cleanup after success or setup errors
  - [x] Test binary integrity, 256 KiB, half-closes, backend-first traffic
  - [x] Verify refused backend and invalid arguments; retain Phases 2-3 tests
- [x] Phase 5 - Nonblocking sockets and `epoll` setup
  - [x] Set `O_NONBLOCK` on listener, accepted client, and connected backend
  - [x] Create `epoll` instance and register the listener for `EPOLLIN`
  - [x] Use level-triggered `epoll_wait()` to drive one-client bidirectional relay
  - [x] Buffer partial nonblocking writes and arm `EPOLLOUT` on demand
  - [x] Preserve TCP half-closes and forward buffered bytes before FIN
  - [x] Inspect `/proc/<pid>/fdinfo` to confirm epoll watches and nonblocking flags
  - [x] Re-run Phase 2-4 tests; verify 1 MiB binary payload integrity
- [x] Phase 6 - Accept multiple clients and track connections
  - [x] Preserve the epoll listener and drain accept() until EAGAIN
  - [x] Give each accepted client a connection object and two relay buffers
  - [x] Route epoll readiness using a dynamically grown fd table
  - [x] Tag epoll events with descriptor generations to ignore stale events
  - [x] Finish, count, and close one session without terminating other clients
  - [x] Keep listener alive when an individual backend connect() fails
  - [x] Verify two concurrent clients, 48 overlapping requests, 24 reconnects
  - [x] Rerun prior listener, echo, proxy, and epoll regression tests
- [x] Phase 7 - Client events and nonblocking backend connections
  - [x] Set O_NONBLOCK before every outbound backend connect()
  - [x] Differentiate immediate success, EINPROGRESS, and hard failure
  - [x] Track CONNECTING/READY states for each backend connection
  - [x] Subscribe to EPOLLOUT to learn when a connection attempt finishes
  - [x] Verify completion with getsockopt(SOL_SOCKET, SO_ERROR)
  - [x] Defer client reads until backend is ready and rearm EPOLLIN afterward
  - [x] Prevent connection failure from terminating the persistent listener
  - [x] Prove nonblocking flag before connect() with test-only LD_PRELOAD
  - [x] Verify 36 concurrent early-send messages and 8 backend refusals
  - [x] Re-run Phase 2-6 regression suites
- [x] Phase 8 - Complete backend connections and buffer client data
  - [x] Read client EPOLLIN while backend is still CONNECTING
  - [x] Stage up to 8 KiB before backend readiness, without calling send()
  - [x] Disable client reads when the staging buffer is occupied
  - [x] Preserve early client EOF and defer forwarding FIN until READY
  - [x] Validate SO_ERROR, drain staged bytes and rearm normal epoll interests
  - [x] Keep preconnect staging separate per client and isolate failed connects
  - [x] Test-only LD_PRELOAD shim defers backend readiness deterministically
  - [x] Verify 256 KiB, 22-byte and zero-byte requests with delayed readiness
  - [x] Verify 24 simultaneous >8 KiB requests and backend refusals
  - [x] Re-run Phase 2-7 regression tests
- [x] Phase 9 - Bidirectional buffering and efficient EPOLLOUT
  - [x] Expand independent per-direction application queues to 64 KiB
  - [x] Keep the 8 KiB Phase 8 preconnect staging limit
  - [x] Append new data behind pending data and compact when needed
  - [x] Disable source EPOLLIN at queue capacity, resume when space returns
  - [x] Enable destination EPOLLOUT only while pending bytes exist
  - [x] Flush pending bytes on write readiness; defer FIN until queue empty
  - [x] Confirm idle EPOLLOUT is disarmed via Linux procfs
  - [x] Exercise slow backend and slow client, 8 MiB binary each direction
  - [x] Re-run Phase 2-8 regression suites
- [x] Phase 10 - First `splice()` experiment and per-connection pipes
  - [x] Add `src/splice_demo.c` for regular-file -> pipe -> regular-file `splice()`
  - [x] Handle partial pipe drains, EINTR, file EOF, and error paths
  - [x] Protect the input file against output path aliases/hard links
  - [x] Allocate two dedicated, nonblocking, CLOEXEC pipe pairs per proxy connection
  - [x] Initialize all four pipe descriptors to -1 before setup
  - [x] Release four pipe descriptors on normal cleanup and partial-setup errors
  - [x] Preserve Phase 9 epoll-based buffered TCP forwarding unchanged
  - [x] Test zero-byte, binary, 64 KiB+ and >2 MiB transfers with exact integrity
  - [x] Verify 2 connections = 4 distinct pipe pairs (8 pipe fds)
  - [x] Check `O_NONBLOCK` and `O_CLOEXEC` via /proc fdinfo and check for leaks
  - [x] Re-run Phase 2-9 regression suites
- [x] Phase 11 - Direction state and client-to-pipe transfer
  - [x] Introduce explicit direction input modes (COPY and SPLICE_TO_PIPE)
  - [x] Track client-to-backend kernel pipe pending and successfully spliced bytes
  - [x] Use splice(client TCP socket -> pipe write end) after backend readiness
  - [x] Drain pipe with read() into the Phase 9 bounded user-space queue
  - [x] Preserve 8 KiB preconnect recv() staging and client half-closes
  - [x] Defer FIN until both pipe and byte queue are empty
  - [x] Leave reverse direction buffered and retain demand-only EPOLLOUT
  - [x] Audit successful live splice calls using a test-only LD_PRELOAD shim
  - [x] Test 512 KiB binary under immediate and delayed connection readiness
  - [x] Preserve empty-request EOF semantics and run Phase 2-10 tests
- [x] Phase 12 - Complete client-to-backend and reverse splice paths
  - [x] Replace Phase 11 client pipe read/send with pipe-to-backend splice()
  - [x] Enable the independent backend-to-client two-hop splice() path
  - [x] Track per-direction pipe input, pipe output, and destination delivery
  - [x] Preserve preconnect recv/send staging ahead of subsequent splice bytes
  - [x] Gate EPOLLIN while the corresponding direction still has pending output
  - [x] Register EPOLLOUT only when staged or pipe bytes need draining
  - [x] Delay write-side shutdown until both kinds of pending bytes drain
  - [x] Ignore SIGPIPE and handle EPIPE as a per-session failure
  - [x] Audit both splice() hop directions with test-only LD_PRELOAD
  - [x] Verify full 1 MiB binary data in both directions, delayed backend, FIN
  - [x] Re-run all Phase 2-11 tests and test the Phase 11 ZIP merge sequence
- [x] Phase 13 - Partial transfers and pipe-fill EAGAIN
  - [x] Fill each nonblocking direction pipe until EAGAIN, EOF or 64 KiB budget
  - [x] Account successful partial splice calls using actual return values
  - [x] Preserve pipe occupancy, never deliver FIN with pending bytes
  - [x] Retry EINTR, handle EAGAIN without treating it as a fatal error
  - [x] Track per-direction partial-splice and EAGAIN counters
  - [x] Use test-only pipe2() interposer to force 4 KiB kernel pipe capacity
  - [x] Prove real pipe-full EAGAIN with input socket FIONREAD > 0
  - [x] Verify 1 MiB exact binary transfers in both directions
  - [x] Re-run and pass every Phase 2-12 regression test
- [x] Phase 14 - Pipe-drain EAGAIN and event-interest updates
  - [x] Preserve pending kernel pipe bytes on pipe-to-socket EAGAIN
  - [x] Count destination-side partial drains and actual EAGAIN returns
  - [x] Track per-direction drain-blocked state independently of pipe fill
  - [x] Arm destination EPOLLOUT only while staged or pipe data remain
  - [x] Pause source EPOLLIN during blocked drains, resume after pipe empties
  - [x] Verify EPOLLOUT is removed once data drains while the session stays open
  - [x] Test stalled backend and stalled client, 8 MiB exact binary each way
  - [x] Rerun complete Phase 2-13 test suite after changing event handling
- [ ] Phase 15 - Integrate splice and verify byte integrity

- [x] Phase 15 - Integrate splice() and verify byte integrity
  - [x] Preserve two independent socket -> pipe -> socket splice paths
  - [x] Track preconnect staged bytes separately from pipe-forwarded bytes
  - [x] Check runtime pipe occupancy and forwarded-byte accounting equations
  - [x] Mark only fully drained, bidirectionally closed sessions as verified
  - [x] Compare exact contents and SHA-256 digests for twelve concurrent streams
  - [x] Cover boundary sizes 0/1/8192/65536 and a 3 MiB+ request
  - [x] Run complete Phase 2-15 regression suite with strict GCC warnings
  - [x] Keep private learning PDF outside the repository

- [x] Phase 16 - Backend list and Round Robin selection
  - [x] Maintain 1-32 IPv4 backend addresses and per-backend selection counts
  - [x] Preserve three-argument legacy proxy startup for existing tests
  - [x] Parse new `IPv4:port` backend list with strict error handling
  - [x] Select next target once per accepted TCP connection
  - [x] Keep backend choice stable throughout nonblocking connect and splice forwarding
  - [x] Reject malformed addresses, invalid ports, and oversized backend lists
  - [x] Verify deterministic 0/1/2 cycling over 12 sequential connections
  - [x] Verify 30 concurrent sessions distribute 10/10/10 and preserve bytes
  - [x] Audit logged choices and completed-session integrity counters
  - [x] Pass all previous Phase 2-15 regression tests
  - [x] Keep personal learning PDF outside repo

- [x] Phase 17 - Least Connections load balancing
  - [x] Preserve Round Robin as default and legacy CLI compatibility
  - [x] Parse an optional `--algorithm least-connections` selector
  - [x] Maintain active connection count for every backend, including pending connects
  - [x] Select least-loaded backend with a rotating tie breaker
  - [x] Increment only after successful connection setup and decrement once in centralized cleanup
  - [x] Release the active slot after backend connect refusal
  - [x] Verify six held-open sessions at 2/2/2, then prefer an underloaded backend
  - [x] Verify twenty-one parallel held sessions at 7/7/7 and all counters return to zero
  - [x] Keep all earlier tests in `make test` and keep learning PDF outside the repository

- [x] Phase 18 - Configurable algorithms and distribution verification
  - [x] Support `--algorithm=NAME` in addition to Phase 17's `--algorithm NAME`
  - [x] Add successful `--help` output, reject malformed/repeated algorithm choices
  - [x] Preserve legacy positional CLI and Round Robin as the default
  - [x] Start three real tagged backends and establish a controlled 2/2/0 load imbalance
  - [x] Confirm Round Robin probe IDs 0/1/2 versus Least Connections 2/2/0
  - [x] Verify exact echo payloads, nine completed sessions per algorithm and cleanup
  - [x] Export optional comparison CSV outside the repo
  - [x] Run strict GCC build, focused algorithm tests, and previous regressions
  - [x] Keep the personal Phase 18 learning PDF outside Git


- [x] Phase 19 - Failed backend handling (previous phase)
  - [x] Quarantine refused backend TCP handshakes with a monotonic cooldown
  - [x] Skip down targets in Round Robin and Least Connections selection
  - [x] Keep healthy connections active; allow safe one-at-a-time recovery probing

- [x] Phase 20 - Backpressure in both directions
  - [x] Track independent client-to-backend and backend-to-client paused states
  - [x] Count backpressure pause/resume transitions separately per direction
  - [x] Measure peak queue occupancy including transient socket-to-pipe splices
  - [x] Enforce a 64 KiB bounded application/pipe queue per direction
  - [x] Preserve EPOLLIN pause / EPOLLOUT resume without busy loops
  - [x] Confirm both destination sockets need EPOLLOUT at the same time
  - [x] Transfer 6 MiB in each direction with exact binary comparison
  - [x] Run Phase 2-17 regressions and Phase 18-20 focused tests
  - [x] Keep personal learning PDF outside Git and source ZIP

- [x] Phase 21 - EOF tracking and client half-close
  - [x] Track client input OPEN -> EOF_OBSERVED -> FIN_FORWARDED once per TCP session
  - [x] Treat EPOLLRDHUP as a hint; only a zero-byte read/splice confirms EOF
  - [x] Drain staged and pipe bytes before backend shutdown(SHUT_WR)
  - [x] Preserve backend-to-client replies after client half-close
  - [x] Verify delayed-connect 0 B and 8 KiB uploads plus 4 MiB slow-backend upload
  - [x] Run Phase 2-21 regression suite with strict C build
  - [x] Keep the personal learning PDF outside Git and source ZIP

- [x] Phase 22 - Backend half-close and connection completion
  - [x] Track independent backend FIN states, drain pending responses before forwarding FIN
  - [x] Keep receiving late client uploads after backend FIN and finish only when both streams complete
  - [x] Verify backend-first FIN and 4 MiB slow-reader reply

- [x] Phase 23 - Abrupt disconnects and centralized cleanup
  - [x] Classify fatal socket EPOLLERR using SO_ERROR while preserving orderly EPOLLHUP/EPOLLRDHUP EOF
  - [x] Reuse one connection teardown for graceful, client reset, backend reset, and failure paths
  - [x] Reuse socket/pipe resource teardown during partial connection registration failures
  - [x] Keep fd+generation token validation to guard against stale epoll batch events
  - [x] Release both socket descriptors, both pipe pairs, epoll subscriptions and fd slots once
  - [x] Verify per-backend active counts do not leak when client/backend resets happen
  - [x] Test real TCP resets with SO_LINGER, /proc fd baseline and subsequent healthy forwarding
  - [x] Validate earlier regression tests and keep the separate learning PDF outside the repo


- [x] Phase 24 - Safe connection lifetime and partial setup failures
  - [x] Add initializing/active/closing lifecycle and publish only after complete socket, pipe and epoll setup
  - [x] Reject stale events with fd+generation token and active-lifecycle guard
  - [x] Mark closing before clearing endpoint slots and releasing every socket/pipe fd
  - [x] Roll back unpublished candidates without decrementing unacquired backend active slots
  - [x] Add test-only LD_PRELOAD syscall failure shim (backend socket, two pipe stages, two epoll stages)
  - [x] Verify exactly one injected failure per proxy process, restored fd baseline and byte-exact recovery on the next client
  - [x] Pass all Phase 2-24 regressions in two test batches under strict C compiler flags
  - [x] Deliver personal learning PDF outside source ZIP and Git repository

- [x] Phase 28 - Reusable Python test harness and basic payload cases
  - [x] Deterministic binary correctness oracle, process cleanup, and concurrent requests
  - [x] Keep production C unchanged

- [x] Phase 29 - Large transfers and byte integrity
  - [x] Add streaming echo fixture to avoid holding whole payloads in memory
  - [x] Add 16 MiB + 17 B fragmented and 32 MiB + 1 B large duplex cases
  - [x] Test four simultaneous 8 MiB+ connections with byte-count and SHA-256 validation
  - [x] Check proxy fd baseline after completed sessions
  - [x] Add make test-large and integrate with make test
  - [x] Keep learning PDF outside Git repository

- [x] Phase 30 - High-concurrency capacity and resource tests
  - [x] Add selector-based echo backend without one OS thread per connection
  - [x] Validate N simultaneously live TCP sessions, payload integrity and held-open probes
  - [x] Verify max-connections rejection and subsequent fd cleanup
  - [x] Collect observed proxy fd count, RSS, CPU time and connection setup timing
  - [x] Add 100-client smoke run to make test and explicit 1k/5k/10k make targets
  - [x] Verify 1,000 live connections on the available Linux execution environment
  - [ ] Verify 5,000 live connections on Fedora with sufficient fd and memory limits
  - [ ] Verify 10,000 live connections on Fedora with sufficient fd and memory limits
  - [x] Keep personal PDF study guide and JSON measurements outside the repository

- [x] Phase 32 - Slow peers, half-closes, and failures
  - [x] Add mixed adversarial integration tests without changing production C
  - [x] Run six concurrent throttled full-duplex streams with exact binary comparison
  - [x] Trigger twelve real client RSTs and ensure a fresh healthy request succeeds
  - [x] Verify baseline file descriptor count after adverse workload cleanup
  - [x] Verify four backend-FIN-first sessions accept later client uploads, checked by SHA-256
  - [x] Quarantine an unavailable backend while five concurrent healthy requests succeed
  - [x] Verify recovered backend rejoins the pool after cooldown
  - [x] Add make test-adversarial and keep it in the default make test target
  - [x] Keep the in-depth personal learning PDF outside the repository
