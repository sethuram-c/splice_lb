# Splice Load Balancer - Phase 6

This phase advances a Fedora/Linux C TCP proxy from **one session per execution** to
**multiple simultaneously active client/backend pairs**, driven by one level-triggered
`epoll` instance. The source remains an **8 KiB buffered copy relay**; `splice()` arrives
in Phase 10. This is a learning implementation and currently listens on loopback only.

## Requirements (Fedora)

```bash
sudo dnf install gcc make python3 git unzip
# Optional tools for Linux socket inspection:
sudo dnf install iproute strace lsof nmap-ncat
```

## Apply the Phase 6 patch to Phase 5

```bash
cd /path/to/splice-load-balancer
unzip -o ~/Downloads/phase06_changes.zip -d .
make clean
make
make test
```

Use the downloaded ZIP's actual path. It contains only added/modified files; don't
remove previous phase source files. Expected final line:

```text
PASS: Phase 6 multi-client connection tracking test suite
```

The suite also executes previous listener, echo, relay, and epoll regression tests.
`make test` uses the Python standard library and a locally bound threaded backend.

## Manual demonstration: keep both servers running

Terminal 1: start a multi-client Python echo backend (testing utility, not the proxy):

```bash
make backend-multi
# Concurrent echo backend listening on 127.0.0.1:9091
```

Terminal 2: start the persistent event-driven proxy:

```bash
make run
# Listening on 127.0.0.1:9090 -> 127.0.0.1:9091
```

Terminal 3: run different clients in succession; the proxy should remain alive:

```bash
./build/backend-client 127.0.0.1 9090 'first message'
./build/backend-client 127.0.0.1 9090 'second message'
```

Expected client stdout: `first message` and `second message` respectively.
The proxy prints an `active=N` count while connections are attached and when they
complete. Stop the proxy/backend with Ctrl+C. The current proxy uses default
SIGINT behavior; **graceful signal-driven shutdown is Phase 27**.

Automated simultaneous-client proof:

```bash
python3 tests/test_multiclient.py
# PASS: 2 simultaneous clients, independent buffers, fd cleanup
# PASS: 48 concurrent requests while another client stays idle
# PASS: 24 sequential reconnects, descriptor reuse, no fd leaks
# PASS: Phase 6 multi-client connection tracking test suite
```

## CLI and limitations

```text
./build/splice-lb <listen-port> <backend-IPv4> <backend-port>
```

- `listen-port` ranges 0..65535; 0 selects an available local port. Backend port 1..65535.
- Listener binds `127.0.0.1` only; one configured numeric IPv4 backend.
- `epoll` remains **level-triggered**. Each connection owns two sockets and two
  8192-byte pending-data buffers. File descriptors are dynamically indexed.
- Each kernel event carries an `(fd,generation)` token. If a closed descriptor is
  reused, an old event cannot accidentally target the new socket.
- A failed backend connection closes that client but does not kill the listener.
- Backend `connect()` is **blocking** during acceptance. A stalled connect can
  delay unrelated clients; this is addressed in Phase 7.
- No connection limits, idle timeouts, zero-copy `splice()`, graceful shutdown,
  or thousands-of-connections benchmarks yet.

## Inspect with Linux tools

```bash
ss -ltn '( sport = :9090 or sport = :9091 )'
ss -tn
pgrep -af splice-lb
ls -l /proc/<PID>/fd
cat /proc/<PID>/fdinfo/<EPOLL_FD>
# Look for tfd: lines - one listener + two endpoints per active connection.
strace -f -e trace=epoll_wait,epoll_ctl,accept,connect,close \
  ./build/splice-lb 9090 127.0.0.1 9091
```

Replace `<PID>` and `<EPOLL_FD>` with actual numbers (without the angle brackets).
Do not measure performance while tracing: `strace` significantly changes timing.

## Phase 6 changed/new files

- `src/main.c`: connection objects, dynamic fd table, generation-tagged epoll
  tokens, multi-client accept loop, individual relay states, local cleanup.
- `tests/test_multiclient.py`: overlap, concurrent progress, FD cleanup/reuse.
- `tests/backend_multi.py`: persistent threaded echo backend for manual testing.
- `tests/test_proxy.py`: older relay assertions updated for persistent listener.
- `tests/test_epoll.py`: counts listener in epoll and tests persistent behavior.
- `Makefile`: adds tests and `backend-multi` command.
- `README.md`, `progress.md`: updated.

**Personal PDF study material is kept outside the Git repository.**
