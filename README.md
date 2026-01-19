# Splice TCP Load Balancer

A Linux IPv4 TCP load balancer written in C, built around **nonblocking sockets**, **`epoll`**, and bidirectional **`splice()`** forwarding through per-connection kernel pipes. Routes TCP connections using **Round Robin** or **Least Connections**.

> **Status:** Phase 35 release candidate. Local Linux tests have verified **1,000 simultaneously open connections**, but the intended **5,000/10,000** test targets are **not yet verified** on the target Fedora workstation. Benchmark results are localhost measurements, not universal performance claims.

## Quick start (Fedora)

```bash
sudo dnf install gcc make python3 git
make clean && make
make demo             # starts three loopback backends and runs both routing demos
make test-release     # checks the demo, graceful shutdown and JSON report
```

No root permissions or external network access are required for the default demo. `make demo` creates `/tmp/phase35-demo.json`. Generated learning PDFs do **not** belong in this repository.

### Manual TCP proxy demo (3 terminals)

```bash
# Terminal 1: three tagged backends
make backend-rr
```

```bash
# Terminal 2: load balancer
./build/splice-lb --algorithm=round-robin 9090 \
  127.0.0.1:9091 127.0.0.1:9092 127.0.0.1:9093
```

```bash
# Terminal 3: repeat to observe cyclic backend labels
./build/backend-client 127.0.0.1 9090 'hello splice'
```

Run `./build/splice-lb --help` for the complete and validated CLI. `--algorithm=least-connections` selects the backend with the fewest active connections, including pending backend handshakes. The default bind address is loopback only. Only use `--listen-ip=0.0.0.0` after reviewing firewall and exposure implications.

## Architecture

```
client TCP socket       Linux pipe        backend TCP socket
        |                 |                        |
        +---- splice() -->+---- splice() --------->+ 
        +<--- splice() ---+<--- splice() -----------+
                      epoll readiness
```

Every accepted TCP connection has two independent unidirectional forwarding states and nonblocking pipes. Preconnect payload staging can use ordinary userspace buffering; after backend establishment, forwarding uses `splice()` where applicable. `EPOLLOUT` is armed only with pending destination writes. Each direction handles partial transfers, backpressure and FIN independently.

More details: [architecture](docs/ARCHITECTURE.md), [testing and benchmarks](docs/VALIDATION.md), [release checklist](docs/RELEASE_CHECKLIST.md).

## Test commands

| Command | Purpose |
|---|---|
| `make test-release` | Fast end-to-end final demo and release smoke |
| `make test-basic` | Binary payload boundary cases |
| `make test-large` | 16/32 MiB transfers and exact integrity |
| `make test-algorithm-stress` | Round Robin vs Least Connections with uneven lifetimes |
| `make test-adversarial` | Slow peers, half-closes, resets, backend recovery |
| `make test-fd-leaks` | Repeated connections, stable descriptor counts |
| `make test-sanitizers` | AddressSanitizer + UBSan and leak checks |
| `make test-benchmark` | Validated benchmark smoke; no arbitrary speed thresholds |
| `make test-scale` | 1,000 live connections (resource limits apply) |
| `make test-scale-5k` / `make test-scale-10k` | **Not yet certified** on Fedora; require substantial file descriptor limits |
| `make test` | Entire regression suite; potentially lengthy |
| `make verify-release` | Release demo, full suite, sanitizer, benchmark smoke |

Benchmark reports and per-host evidence should live under `/tmp` or a separate private notes folder. See [validation and performance evidence](docs/VALIDATION.md) for interpretation.

## Design and limitations

- **Linux-only**, IPv4 and TCP; no TLS termination, HTTP parsing, or UDP support.
- Balances **connections**, not individual HTTP requests or bytes transferred. Least Connections does not measure CPU or per-backend processing cost.
- Backend failure suppression is cooldown-based; recovering backends are probed by subsequent traffic, not a continuous external health agent.
- Failed TCP sessions are **not replayed** to a different backend, to avoid duplicating application side effects.
- A configurable **10,000-connection limit is not proof of 10,000 concurrent connections**. Test with suitable `ulimit -n`, memory and kernel limits before making that claim.
- The provided reference baseline is a **blocking comparison implementation** for learning; performance results depend strongly on host and workload.

## Repository map

- `src/main.c`: production event loop, scheduling, forwarding and cleanup.
- `src/backend_client.c`, `src/echo_backend.c`, `src/splice_demo.c`: learning and manual test helpers.
- `demo/final_demo.py`: self-contained Round Robin + Least Connections demonstration.
- `tests/`: correctness, concurrency, fault-injection, resource safety and release tests.
- `benchmarks/`: blocking baseline plus repeatable benchmark runner.
- `docs/`: architecture, validation and original phase-by-phase README history.
- `progress.md`: 35-phase roadmap and remaining host-specific verification tasks.

**Licensing:** no license is asserted by this release guide. Add an appropriate `LICENSE` file before advertising the repository as open source.
