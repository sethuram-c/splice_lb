# Splice Load Balancer - Phase 7

Phase 7 makes outbound backend **connection establishment nonblocking**. The
Phase 6 proxy already accepts multiple TCP clients using level-triggered
`epoll`, but a blocking `connect()` inside its accept loop could stall every
client. Now the backend socket has `O_NONBLOCK` **before** `connect()`, and a
per-connection `BACKEND_CONNECTING` / `BACKEND_READY` state tracks completion.

Traffic is still forwarded using per-direction **8 KiB user-space buffers**.
Zero-copy `splice()` starts in Phase 10. The listening interface remains
`127.0.0.1` for safe local development.

## Fedora requirements

```bash
sudo dnf install gcc make python3 git unzip
# Optional socket tracing and manual testing utilities:
sudo dnf install iproute strace lsof nmap-ncat
```

The Linux-specific `LD_PRELOAD` observer used by the Phase 7 tests compiles a
temporary shared object using the installed C compiler. It is **test-only**;
it is never linked into the production proxy and is deleted afterward.

## Apply the patch over Phase 6

```bash
cd /path/to/splice-load-balancer
unzip -o ~/Downloads/phase07_changes.zip -d .
make clean
make
make test
```

Expected last line:

```text
PASS: Phase 7 nonblocking backend connection test suite
```

Tests for Phases 2-6 also execute. The Phase 7 tests prove that `O_NONBLOCK`
was set at the exact outbound `connect()` invocation (through a test-only
`LD_PRELOAD` interceptor), send 36 early client payloads concurrently, verify
byte integrity, and check that eight rejected backend connections do not kill
the listening socket.

## Manual demonstration (three Fedora terminals)

Terminal 1 - persistent multi-client test backend:

```bash
make backend-multi
# Concurrent echo backend listening on 127.0.0.1:9091
```

Terminal 2 - proxy:

```bash
make run
# Listening on 127.0.0.1:9090 -> 127.0.0.1:9091
```

Terminal 3 - sample requests:

```bash
./build/backend-client 127.0.0.1 9090 "hello async connect"
./build/backend-client 127.0.0.1 9090 "a second request"
ss -ltn '( sport = :9090 or sport = :9091 )'
```

Expected client outputs: `hello async connect` and `a second request`. The
proxy stays running and logs `Backend connection pending; active=N` when the
TCP handshake is still underway, followed by `Connected to backend; active=N`
on successful completion. The pending message is not guaranteed because an
outbound connect can also finish immediately. Stop both servers with Ctrl+C;
controlled SIGINT shutdown is planned for Phase 27.

Targeted test and optional trace:

```bash
python3 tests/test_async_connect.py
strace -f -e trace=connect,getsockopt,epoll_ctl,epoll_wait \
  ./build/splice-lb 9090 127.0.0.1 9091
```

Run the `strace` command with `make backend-multi` already running and use the
client in another terminal. Expect `connect(...)= -1 EINPROGRESS` (or sometimes
immediate success), then a `getsockopt(... SOL_SOCKET, SO_ERROR ...)` check.
A refusal often appears as `SO_ERROR=ECONNREFUSED` after an `EPOLLERR` event.
`strace` alters timing and is **not** a benchmark.

## Why this state machine exists

1. Accepted client is nonblocking. A second nonblocking socket is created.
2. `connect()` may succeed immediately, fail immediately, or return -1 with
   `errno=EINPROGRESS`. Only `EINPROGRESS` means an attempt is in progress.
3. In CONNECTING state, the backend is watched for `EPOLLOUT`; the client is
   registered but reading is temporarily paused. Thus no client payload can be
   sent through a socket that is not connected yet.
4. A completion/error event triggers `getsockopt(SOL_SOCKET, SO_ERROR)`. Only
   zero means successful completion; an error closes that particular session.
5. On success, the socket interests switch to ordinary bidirectional relay.
   The client bytes already queued in the kernel receive level-triggered
   `EPOLLIN` once interest is restored.

Do not interpret `EPOLLOUT` as success without checking `SO_ERROR`.
Do not keep `EPOLLOUT` armed after completion unless there is pending data:
an always-writable socket can otherwise spin the event loop.

## CLI and present limitations

```text
./build/splice-lb <listen-port> <backend-IPv4> <backend-port>
```

- Only one numeric backend IPv4 address; Round Robin comes in Phase 16.
- The client socket deliberately waits unread while backend connects. Phase 8
  introduces explicit bounded preconnect buffering and stronger setup handling.
- No connect timeout (Phase 26), idle timeout, connection limit, graceful
  shutdown, or zero-copy `splice()` pipeline yet.
- A failed backend connection is isolated to its client. Existing connections
  continue; the proxy does not retry on a different backend yet.
- The proxy has not been benchmarked for thousands of concurrent sockets.

## Changed or new files in the ZIP

- `src/main.c`: async backend connect states, event masks, `SO_ERROR` check.
- `tests/connect_observer.c`: temporary test-only `connect` audit interposer.
- `tests/test_async_connect.py`: concurrency, early data, refusal tests.
- `tests/test_proxy.py`: permits an environment override for the test process.
- `Makefile`: adds the Phase 7 test to `make test`.
- `README.md`, `progress.md`: documentation and tracked progress.

**Keep `phase07_learning_guide.pdf` outside the Git repo.**
