# Tests and measurements

This page records what the test suite checks and what the available measurements
actually say. The scripts are under `tests/` and `benchmarks/`; most tests
start temporary loopback backends rather than requiring a service to be running
already.

## Run on Fedora

```bash
sudo dnf install gcc make python3 git
make clean && make
make test-release
make test
make test-sanitizers
make test-fd-leaks
make test-benchmark
```

The full suite is long and some tests are sensitive to scheduling under heavy
host load. If one fails, run that Python test directly and inspect its logs
before assuming a real regression. An interrupted suite is not a pass.

`make verify-release` runs the release test, full regression suite, sanitizers,
and benchmark smoke test in sequence.

## Correctness coverage

The tests exercise binary payloads (including NUL bytes), large streaming
uploads and downloads, short writes, blocked pipes, half-closes in either
direction, TCP resets, failed backend handshakes, connection timeouts, and
resource cleanup. Separate tests inject failures into socket and pipe setup.

The file-descriptor checks look at `/proc/<pid>/fd` before and after a workload.
Returning to the baseline is useful evidence, but doesn't prove that every
possible leak is absent. Similarly, sanitizer runs cover the paths exercised
by those tests, not all possible program behavior.

## Connection scale

A prior Linux test held 1,000 client connections open at once, sent and checked
payloads, and then released them. One run reported roughly 12.5 MiB resident
memory for the proxy. I have not reproduced that number on Fedora yet.

The 5,000 and 10,000 targets are unverified. Each proxy session owns two TCP
sockets and two pipe pairs, or about six FDs; 5,000 sessions therefore require
roughly 30,000 proxy descriptors before counting the listener and other FDs.
The client generator and backend have their own limits.

```bash
ulimit -Sn
ulimit -Hn
make test-scale
# Only if your limits and available resources permit:
make test-scale-5k
make test-scale-10k
```

Record the kernel version, `ulimit -n`, available memory, and actual pass/fail
result when running these on Fedora. Don't describe the configured 10,000
connection limit as measured capacity.

## Localhost benchmark

The benchmark runner compares the event-driven proxy against a simple blocking
reference. These are medians from three repetitions in the earlier Linux
execution environment, with a loopback backend and Python load generator:

| Clients | `epoll` + `splice()` MiB/s | Blocking MiB/s | `splice()` p99 | Blocking p99 |
| ---: | ---: | ---: | ---: | ---: |
| 1  | 292.1 | 323.4 | 0.286 ms | 0.163 ms |
| 8  | 292.0 | 277.9 | 0.640 ms | 0.566 ms |
| 32 | 288.0 | 231.7 | 2.267 ms | 2.433 ms |

The results are mixed. The event-driven version did better at 32 clients in
this run, but the blocking reference did better for one client. These numbers
are useful for identifying further experiments, not for claiming a guaranteed
speedup. They are not Fedora results.

```bash
make test-benchmark
make benchmark-quick
make benchmark
python3 -m json.tool /tmp/phase34-benchmark.json | less
```

Benchmark an ordinary optimized build, not a sanitizer build or a process under
`strace`. For a comparison worth publishing, use more repetitions, keep the
load generator separate from the proxy if possible, and record CPU, memory,
request sizes, connection counts, and latency percentiles.

## Remaining validation

- Complete one uninterrupted `make test` on the Fedora host.
- Repeat ASan/UBSan and descriptor checks there.
- Rerun the 1,000-connection test and record machine details.
- Attempt 5,000 and 10,000 only if system limits allow it.
- Repeat the benchmark with stable conditions before quoting a speedup.

The earlier execution environment ran every regression suite, sometimes in
separate batches because a single command exceeded the execution limit. That
is not the same as a fully completed single-run Fedora release check.
