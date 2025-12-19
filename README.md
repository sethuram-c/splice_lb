# Splice Load Balancer (Linux / C)

**Phase 5:** a **single-client, nonblocking, level-triggered `epoll` TCP proxy** for Fedora Linux. It forwards arbitrary binary TCP data between a local client and one configured backend, preserves half-closes, and exits after the connection completes. Compared with Phase 4, the listener and both established sockets have `O_NONBLOCK`, and `epoll_wait()` replaces `select()`.

**Scope:** this is the event-loop foundation, **not yet a concurrent load balancer**. Each run accepts one client. The outbound backend `connect()` is still **blocking** until Phase 7, although the backend socket is made nonblocking immediately after connection. The fixed 8192-byte buffer in each direction is an initial correctness mechanism; later phases expand connection state and backpressure handling. `splice()` begins in Phase 10.

## Fedora dependencies

```bash
sudo dnf install gcc make python3 git unzip
# Optional network/system-call diagnostics:
sudo dnf install nmap-ncat iproute strace lsof
```

## Apply Phase 5 and verify

From your existing Phase 4 repository:

```bash
unzip -o ~/Downloads/phase05_changes.zip -d .
make clean
make
make test
```

The ZIP contains **only new/modified files**; retain unchanged Phase 4 files.

`make` produces `build/splice-lb` (Phase 5 proxy), `build/echo-backend` (Phase 3 test server), and `build/backend-client` (Phase 3 client). `make test` runs listener and echo regressions, Phase 4 proxy end-to-end cases, plus a new Phase 5 Linux-specific `/proc/<pid>/fdinfo` inspection and a **1 MiB binary integrity** case. The final line should be:

```text
PASS: Phase 5 nonblocking sockets and epoll setup test suite
```

## Manual demo (three terminal windows)

Terminal 1 - start the one-client echo backend:

```bash
make backend
# Listening on 127.0.0.1:9091
```

Terminal 2 - start the one-client proxy:

```bash
make run
# Listening on 127.0.0.1:9090 -> 127.0.0.1:9091
```

Terminal 3 - send a short request and read its echo:

```bash
./build/backend-client 127.0.0.1 9090 "hello epoll"
# Expected stdout: hello epoll
```

Both server processes exit after this session; restart both for another demo.

## CLI and architecture

```text
./build/splice-lb <listen-port: 0-65535> <backend-IPv4> <backend-port: 1-65535>
./build/echo-backend [listen-port: 0-65535]
./build/backend-client <IPv4-address> <port: 1-65535> <payload>
```

The proxy always binds to `127.0.0.1`, and port `0` requests an available kernel-selected port. Numeric IPv4 backend addresses only.

```text
client <---- TCP A ----> [socket NONBLOCK] PROXY [socket NONBLOCK] <---- TCP B ----> backend
                                  |         |
                                  +--- epoll+
```

The Phase 5 proxy uses a Linux `epoll` instance to wait for listener readiness, accepts a client, completes the backend connection, then watches `EPOLLIN` for data and enables `EPOLLOUT` **only when a direction has pending bytes**. One 8192-byte buffer per direction retains partial writes. It temporarily disables source reads while that buffer is full, so it cannot overwrite unwritten bytes. EOF (`recv()==0`) triggers `shutdown(other, SHUT_WR)` only after buffered bytes drain, while the reverse stream continues independently. `EPOLLHUP`/`EPOLLERR` are investigated through I/O operations rather than ignored.

This is **level-triggered** `epoll`; `EPOLLET`, `EPOLLONESHOT`, multi-client state, nonblocking connect handling, long-term connection limits, true zero-copy forwarding and benchmark claims are out of scope.

## Fedora diagnostics

```bash
ss -ltn '( sport = :9090 or sport = :9091 )'
ss -tn
lsof -nP -iTCP:9090
# Trace in terminal 2, then use terminal 3 to trigger a connection:
strace -f -o /tmp/phase05.trace -e trace=epoll_ctl,epoll_wait,fcntl,accept,recvfrom,sendto,shutdown,close \
  ./build/splice-lb 9090 127.0.0.1 9091
grep -E 'epoll|fcntl|accept|recvfrom|sendto|shutdown' /tmp/phase05.trace
```

`/proc/<pid>/fd` lists live descriptor targets and `/proc/<pid>/fdinfo/<fd>` contains the octal descriptor flags and (for an epoll descriptor) the `tfd:` watched entries. Check them **while the process is alive**; automated tests do this for you. Debugging with `strace` can alter timing substantially; do not benchmark while tracing.

## Repository layout

- `src/main.c` - modified: nonblocking listener/relay and epoll event loop.
- `tests/test_epoll.py` - new: Linux epoll/fdinfo assertions and 1 MiB binary request-response.
- `Makefile` - modified: adds Phase 5 tests to `make test`.
- `README.md`, `progress.md` - updated documentation and learning milestones.
- `src/echo_backend.c`, `src/backend_client.c`, previous `tests/` - unchanged from Phase 4.

**The Phase 5 learning PDF is personal study material; keep it outside the Git repository and never include it in `git add`.**
