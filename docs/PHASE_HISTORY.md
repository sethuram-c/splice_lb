# Build log

This is the short version of how the proxy came together. The commit history
has the individual changes; this page records the steps that mattered and the
problems each one exposed.

## Getting the relay working (1–9)

1. Created the C/Makefile project and checked the socket open/close lifecycle.
2. Added a listening socket, one `accept()`, and a small local test.
3. Built an echo server and standalone client. This exposed the difference
   between messages and a TCP byte stream.
4. Added the first blocking relay and explicit cleanup for its sockets.
5. Replaced the blocking relay loop with nonblocking sockets and `epoll`.
6. Kept the listener alive across clients. Added per-connection state and
   generation-tagged event tokens for reused descriptors.
7. Changed upstream `connect()` to nonblocking and checked completion through
   `EPOLLOUT` and `SO_ERROR`.
8. Staged early client bytes during a pending backend handshake. The staging
   buffer has its own limit rather than growing without bound.
9. Made writes readiness-driven and kept independent queues for both directions.

## Moving data through pipes (10–15)

10. Prototyped `splice()` with files and created two pipes per connection.
11. Moved client bytes into a pipe, initially draining back to userspace so
    the existing relay could keep working during the transition.
12. Added pipe-to-backend `splice()` and the reverse data path. Early staged
    bytes still use the ordinary socket APIs.
13. Tracked short `splice()` results and the `EAGAIN` case when filling a pipe.
14. Handled a blocked pipe-to-socket transfer by watching destination write
    readiness; removed the watch once the pending bytes drained.
15. Added larger byte-integrity tests and cross-checked staged, piped, and
    forwarded byte counts.

## Backend selection (16–19)

16. Added multiple backend addresses and connection-level Round Robin.
17. Added Least Connections. Pending handshakes count toward active load, and
    cleanup must decrement the selected backend exactly once.
18. Made the policy selectable on the CLI and compared both algorithms with
    deliberately uneven connection lifetimes.
19. Added a cooldown after failed backend handshakes. New sessions can try a
    recovered backend later; existing TCP streams are not replayed elsewhere.

## Handling the difficult connections (20–27)

20. Exercised simultaneous backpressure in both directions and added queue
    occupancy and pause/resume checks.
21. Separated client EOF detection from forwarding FIN. Buffered bytes have
    to leave before `shutdown(SHUT_WR)`.
22. Completed the same logic for backend EOF and required both directions to
    finish before graceful connection teardown.
23. Added reset tests and centralized cleanup. A failed session must not take
    down the listener.
24. Tightened connection initialization and rollback, with fault injection at
    pipe/socket/`epoll` setup points.
25. Added parsing and validation for the listen address, backlog, retry delay,
    and backend addresses.
26. Integrated handshake and idle timeouts using monotonic deadlines in the
    `epoll_wait()` loop.
27. Added a connection cap and handled SIGINT/SIGTERM through `signalfd`. The
    listener stops first; existing sessions get a configurable drain window.

## Testing and checking performance (28–35)

28. Built a reusable Python backend/proxy harness, binary payload generator,
    and checks that deliberately reject corrupted replies.
29. Tested 16/32 MiB streams, small fragments, and concurrent transfers.
30. Added a selector-based backend and concurrency tests. A 1,000-live-session
    run passed in the earlier Linux environment; 5k/10k remain unverified.
31. Compared routing policies while holding connections open and closing
    selected sessions to create uneven load.
32. Combined slow peers, half-closes, resets, backend failure, and recovery in
    one adversarial test workload.
33. Built separate ASan/UBSan binaries and checked descriptor counts after
    both normal and abrupt disconnects.
34. Added a blocking reference and measured local goodput and p99 latency.
    Results were mixed, so the benchmark is evidence to investigate, not proof
    of a general speedup.
35. Added a self-contained demo, release checks, and architecture/validation
    notes. The last host-specific scale and benchmark checks are still open.

## Bugs and tradeoffs that stood out

- A write-ready notification doesn't promise that all bytes will fit. Pending
  bytes must survive partial transfers and be retried later.
- An `EPOLLRDHUP` notification is not a reason to throw away what is left in a
  pipe. The real EOF and FIN-forwarded states need separate tracking.
- Linux can reuse an FD quickly. Tagging an `epoll` event with the descriptor
  alone is not enough for safe connection lifetime handling.
- Heavy logging to a pipe without a reader can block the event loop. The test
  harness writes process output to ordinary temporary files instead.
- The largest connection tests depend as much on file descriptor limits and
  available memory as on the `epoll` implementation itself.

The code is intentionally Linux-specific. It is a useful low-level networking
project, but it isn't an HTTP-aware reverse proxy or a drop-in production edge
service.
