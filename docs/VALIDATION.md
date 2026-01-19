# Validation, Benchmarks and Evidence

## Correctness versus performance

**Correctness:** exact-byte responses, source EOF propagation, half-close behavior, cleanup after RST, algorithm distribution and resource counts. **Performance:** measured payload goodput and latency on a specific host under a specific concurrent workload. These must never be conflated.

## Final release verification (Fedora)

```bash
sudo dnf install gcc make python3 git
make clean && make
make test-release
make test-basic
make test-large
make test-algorithm-stress
make test-adversarial
make test-fd-leaks
make test-sanitizers
make test-benchmark
make test
```

For the complete automated sequence, `make verify-release` runs the release, regression, sanitizer and benchmark smoke tests. `make demo` starts a tagged three-backend scenario, demonstrates the algorithms, checks shutdown and writes `/tmp/phase35-demo.json`.

After `make test` completes, run the host-specific capacity tests:

```bash
ulimit -Sn
ulimit -Hn
ulimit -Sn 65535  # only if the system's hard limit allows it
make test-scale
make test-scale-5k
make test-scale-10k
```

Each active proxy session consumes multiple file descriptors (two sockets plus two pipe pairs). **5,000 sessions can require roughly 30,000 proxy FDs** in addition to listener, `epoll`, backend and test-client FDs. `ulimit -n` applies per process, while memory, ephemeral ports and kernel limits also matter. Do not increase global limits blindly; record each change and restore them after the experiment if needed.

## Evidence so far

| Workload | Observation | Evidence status |
|---|---|---|
| Basic binary payload suite | Boundary lengths and concurrent sessions byte exact | Previously verified in Linux execution environment |
| 32 MiB+ streaming | Exact streamed byte equality and SHA-256 | Previously verified in Linux execution environment |
| 1,000 active TCP clients | Held live; resumed; cleaned up; 12.5 MiB proxy RSS in one local run | **Verified locally**, not yet repeated on Fedora |
| 5,000 and 10,000 clients | Test targets exist | **Not verified**; file descriptor hard limit on prior environment was 16,384 |
| RR vs LC uneven lifetimes | LC reduced active-load spread in crafted scenarios | Algorithm distribution result, not a throughput result |
| ASan/UBSan/LSan + FD tests | No faults reported in tested paths | Does not prove absence of bugs |
| Blocking vs `epoll`+`splice()` | Mixed results on localhost | Limited repetitions and platform-specific |

## Phase 34 short-loopback benchmark

Median goodput from three iterations in the previous Linux execution environment:

| Clients | `epoll` + `splice()` | Blocking reference | `splice()` p99 | Blocking p99 |
|---|---:|---:|---:|---:|
| 1 | 292.1 MiB/s | 323.4 MiB/s | 0.286 ms | 0.163 ms |
| 8 | 292.0 MiB/s | 277.9 MiB/s | 0.640 ms | 0.566 ms |
| 32 | 288.0 MiB/s | 231.7 MiB/s | 2.267 ms | 2.433 ms |

These are **illustrative local application-path measurements**. A Python load generator, loopback networking, scheduling jitter and cache effects influence results. The blocking comparator is a simple reference, not a tuned production proxy. Do not claim a universal speedup based on this table.

Repeat on Fedora:

```bash
make test-benchmark
make benchmark-quick
make benchmark
python3 -m json.tool /tmp/phase34-benchmark.json | less
```

Record Fedora version, CPU, Linux kernel, RAM, compiler and flags, soft/hard file descriptor limits, background processes, number of repetitions, payload sizes, p50/p95/p99 and aggregate throughput. Export JSON only outside the repository. Never benchmark sanitizer builds or under `strace`.

## Resume wording: measured and unmeasured

Supported implementation claims: “Built a nonblocking, Linux `epoll` TCP load balancer in C with bidirectional `splice()` forwarding, Round Robin and Least Connections routing, bounded backpressure and graceful shutdown.”

Supported measured claim: “Validated 1,000 simultaneously active TCP connections in a controlled Linux test environment with binary integrity checks.” This is stronger when independently repeated on Fedora. 

**Hold back** “10,000+ concurrent connections” until it actually passes on your host; similarly, do not publish a throughput improvement percentage without a controlled, repeatable measurement.

## Phase 35 release-check note

The Phase 35 focused demo, sanitizer suite, descriptor-leak suite and benchmark smoke all passed on the current Linux execution environment. The monolithic `make test` command was externally interrupted while starting the Phase 15 test. The remaining Phase 15–35 suites then passed as separate runs. Consequently, **every suite was exercised**, but a single uninterrupted `make test` success is **not asserted**. Repeat `make test` on Fedora as part of release sign-off.
