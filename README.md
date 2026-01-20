# splice_lb

A TCP load balancer for Linux, written in C. It accepts client connections with
`epoll`, connects to a selected backend without blocking the event loop, and
forwards data in both directions with `splice()` and a pair of pipes per
connection.

I started with a blocking TCP relay and worked toward an event-driven version.
The main things I wanted to get right were partial transfers, backpressure,
half-closed sockets, and cleaning up after failed connections.

## Build and try it

On Fedora:

```bash
sudo dnf install gcc make python3 git
make clean && make
make demo
```

`make demo` starts local backends, runs both routing policies, and writes a
report to `/tmp/phase35-demo.json`. It doesn't need root access.

For a manual run, open three terminals:

```bash
# Terminal 1: three backends that identify themselves in replies
make backend-rr
```

```bash
# Terminal 2: load balancer
./build/splice-lb --algorithm=round-robin 9090 \
  127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093
```

```bash
# Terminal 3: run this a few times
./build/backend-client 127.0.0.1 9090 'hello'
```

The backend prefix in the reply should rotate. To try the other policy, restart
the proxy with `--algorithm=least-connections`. Its choice depends on how many
sessions are still open, so the automated load-distribution test is a better
example than one-off requests.

`./build/splice-lb --help` shows all CLI options. The listener binds to
`127.0.0.1` by default; `--listen-ip=0.0.0.0` exposes it on other interfaces
when the host firewall permits that.

## How it works

Each accepted connection has a client socket, an upstream socket, and two
nonblocking pipes (one for each direction):

```text
client --splice--> pipe --splice--> backend
client <--splice-- pipe <--splice-- backend
```

`epoll` drives socket readiness. Pending backend connections are checked with
`SO_ERROR`. The proxy temporarily stages a small amount of client data before
the upstream connection completes; that early path uses `recv()`/`send()`.
Once connected, the main forwarding path uses `splice()`.

When a destination is slow, the corresponding pipe fills and the proxy stops
reading from that source until it can make progress. `EPOLLOUT` is requested
only while a write is pending. EOF is tracked independently in both directions:
one side can finish sending without losing a response from the other side.

Backend selection supports Round Robin and Least Connections. Failed TCP
handshakes put a backend on a short cooldown; subsequent traffic can probe it
again. An existing TCP stream is not replayed on another backend, since doing
so could duplicate an application request.

More detail: [architecture](docs/ARCHITECTURE.md).

## Tests

```bash
make test-release          # small end-to-end check
make test-basic            # payload lengths, binary data, concurrent clients
make test-large            # 16/32 MiB streaming transfers
make test-algorithm-stress # uneven connection lifetimes
make test-adversarial      # slow peers, FIN, RST, backend failure
make test-fd-leaks         # /proc descriptor checks
make test-sanitizers       # separate ASan/UBSan build
make test-benchmark        # checks the benchmark tool
make test-scale            # 1,000 simultaneous connections
make test                 # full regression suite (takes longer)
```

For the larger concurrency targets, use `make test-scale-5k` or
`make test-scale-10k` after checking the file-descriptor limit. A proxied
connection uses approximately six descriptors in the proxy alone, so the
higher tests may require changes to `ulimit -n` and enough memory.

## Results so far

- A 1,000-live-connection run passed in the original Linux test environment.
  I still need to reproduce that result on the target Fedora machine.
- Large binary transfers and deliberately slow peers passed integrity tests.
- Least Connections reduced active-connection imbalance in tests with uneven
  session lifetimes; this is not a claim that it has better throughput.
- Localhost benchmarks against a simple blocking reference showed mixed
  results. The methodology and numbers are in [validation](docs/VALIDATION.md).

**The 5,000- and 10,000-connection targets are not verified.** A configured
maximum of 10,000 is only a limit, not a measured capacity.

## Boundaries

This is a TCP/IPv4 connection-level proxy. It does not parse HTTP, terminate
TLS, support UDP or IPv6, or run active application-health probes. It doesn't
attempt seamless failover for an already established stream. There are also
places where the single-file event loop in `src/main.c` could use further
refactoring.

Source is in `src/`, integration tests in `tests/`, benchmark tools in
`benchmarks/`, and the demo in `demo/`. I kept the implementation history in
[development notes](docs/PHASE_HISTORY.md). Known verification work is tracked
in [progress.md](progress.md).

No open-source license has been selected yet.
