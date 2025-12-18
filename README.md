# Splice Load Balancer (Linux / C)

**Phase 4:** a **single-client blocking, bidirectional TCP proxy** for Fedora
Linux. The proxy listens on IPv4 loopback, accepts one client, opens one
outbound connection to a configured backend, copies raw bytes both ways, and
exits after both directions reach EOF. **This is not yet a concurrent,
nonblocking, or `splice()`-based load balancer.**

## Fedora dependencies

```bash
sudo dnf install gcc make python3 git
# Optional debugging tools:
sudo dnf install nmap-ncat iproute strace lsof
```

## Build and regression tests

```bash
make clean
make
make test
```

`make` builds three programs:

- `build/splice-lb` - new Phase 4 blocking proxy.
- `build/echo-backend` - preserved Phase 3 echo server for demos/regressions.
- `build/backend-client` - preserved Phase 3 TCP test client.

`make test` runs the Phase 2 listener tests and Phase 3 echo tests **against
`echo-backend`**, then new Phase 4 end-to-end tests **through `splice-lb`**.
It tests binary data, 256 KiB requests, reverse-direction responses,
backend-first messages, independent half-closes, CLI validation, refused
backend connections, and successful descriptor cleanup. Expected final line:

```text
PASS: Phase 4 blocking proxy and resource cleanup test suite
```

## Manual demo: three Fedora terminals

**Terminal 1** (single-use TCP echo backend on port 9091):

```bash
make backend
# Listening on 127.0.0.1:9091
```

**Terminal 2** (single-client proxy on port 9090):

```bash
make run
# Listening on 127.0.0.1:9090 -> 127.0.0.1:9091
```

**Terminal 3** (client sending data via proxy):

```bash
./build/backend-client 127.0.0.1 9090 "hello through proxy"
# stdout: hello through proxy
```

Terminals 1 and 2 exit after the session, by design. For another request,
restart **both**. Use nonconflicting ports when 9090/9091 are in use.

## CLI

```text
./build/splice-lb <listen-port: 0-65535> <backend-IPv4> <backend-port: 1-65535>
./build/echo-backend [port: 0-65535]
./build/backend-client <IPv4-address> <port: 1-65535> <payload>
```

Example: `./build/splice-lb 9090 127.0.0.1 9091`. Listen port `0` requests
an ephemeral port; the allocated port is printed. Only numeric IPv4 backend
addresses are accepted. The proxy listens only on `127.0.0.1` during these
local-only learning phases.

## Design

```text
client  <---- TCP connection #1 ---->  proxy  <---- TCP connection #2 ---->  backend
               127.0.0.1:9090               127.0.0.1:9091
```

After `accept()` the proxy uses `connect()` to establish a distinct backend
connection. `select()` watches readability of both sockets. For each readable
side, `recv()` obtains up to 8192 bytes and `send()` forwards **every byte**
(with partial-send and `EINTR` handling). On `recv()==0`, the proxy calls
`shutdown(other, SHUT_WR)` but continues forwarding in the reverse direction.
This is essential for request/response protocols that signal request end
with a write half-close. `MSG_NOSIGNAL` prevents peer disconnects from
terminating the process with `SIGPIPE`.

**Limitations of this baseline:** `connect()`, `recv()`, and `send()` are still
blocking. A stalled `send()` can pause forwarding in both directions even
though `select()` is used. Only one client is served per run; connection
timeouts, retry/failover, multiple backends, epoll, backpressure and splice
arrive in later phases. `select()` imposes `FD_SETSIZE` constraints. This
baseline is for correctness and comparative performance measurements, not
for production traffic.

## Optional Fedora diagnostics

```bash
ss -ltn '( sport = :9090 or sport = :9091 )'  # listener ports
ss -tn                                       # established TCP connections
strace -f -e trace=network,select,close ./build/splice-lb 9090 127.0.0.1 9091
lsof -nP -iTCP:9090                           # owning process, if installed
```

`strace` and `lsof` are optional packages. The proxy closes its listener,
client, and backend descriptors on the success and error paths.

## Repository layout

- `src/main.c` - blocking TCP proxy (Phase 4).
- `src/echo_backend.c` - previous echo server retained as a test backend.
- `src/backend_client.c` - Phase 3 standalone client (unchanged).
- `tests/test_listener.py` - Phase 2 regression against `echo-backend`.
- `tests/test_echo.py` - Phase 3 regression against `echo-backend`.
- `tests/test_proxy.py` - Phase 4 end-to-end proxy scenarios.
- `tests/test_socket.sh` - compatibility test wrapper.
- `Makefile` - `all`, `clean`, `run`, `backend`, `test`.
- `progress.md` - phase-by-phase record.

**Learning PDFs are personal study material. Do not add them to Git.**
The ZIP contains changed/new project files only; extract it over Phase 3.
