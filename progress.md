# Current state / follow-up work

The main event-driven proxy, both backend-selection policies, and the test
harness are implemented. See [docs/PHASE_HISTORY.md](docs/PHASE_HISTORY.md) for
the chronological development log and [docs/VALIDATION.md](docs/VALIDATION.md)
for the measurements gathered so far.

## Working features

- Nonblocking TCP listener and upstream connections through `epoll`.
- Bidirectional forwarding through Linux pipes and `splice()`.
- Bounded pending data, `EAGAIN` handling, and per-direction FIN propagation.
- Round Robin and Least Connections, with cooldown after failed handshakes.
- Timeouts, connection limits, `signalfd` shutdown, and centralized cleanup.
- Tests for binary integrity, long transfers, resets, fault injection,
  algorithm distribution, sanitizer checks, and descriptor counts.

## Before calling it fully verified on Fedora

- [ ] Run `make clean && make && make test` end to end on Fedora.
- [ ] Run `make test-sanitizers` and `make test-fd-leaks` there.
- [ ] Reproduce the 1,000-connection test with system details recorded.
- [ ] Evaluate the 5,000 and 10,000 connection targets after raising resource
      limits if needed. Neither target has passed in the earlier environment.
- [ ] Repeat the benchmark with recorded CPU, kernel, payload sizes, and run
      variance. Don't quote the localhost comparison as a universal speedup.
- [ ] Recheck the docs, add a license if appropriate, and tag the release.

## Notes worth keeping

- There is still a userspace preconnect buffer. `splice()` handles the normal
  connected data path, not necessarily all early bytes.
- A half-close isn't a disconnect: EOF from one side cannot discard data still
  queued in the opposite direction.
- Stale `epoll` events are guarded by generation tags; partial setup requires
  rollback of every opened descriptor.
- The proxy manages a 64 KiB queue per direction, but TCP socket memory is
  accounted for by the kernel separately.
- The original local test setup passed 1,000 simultaneous sessions; the target
  Fedora host has not been measured yet.
- `src/main.c` is larger than I'd like. Splitting event-loop, routing, and
  forwarding modules would make future changes easier.
