# Architecture notes

This is a single-process, single-event-loop TCP proxy. There is no worker pool
and no application-level protocol inspection. Each incoming connection is
assigned to one backend, and the proxy moves bytes until both TCP directions
finish or an error closes the session.

## Connection setup

The listening socket and accepted client sockets are nonblocking. A new client
gets a backend selected by Round Robin or Least Connections. The backend socket
is also nonblocking: `connect()` can return `EINPROGRESS`, and a later writable
notification prompts a `getsockopt(SO_ERROR)` check.

A connection is not exposed to the normal event handlers until its sockets,
pipes, lookup entry, and `epoll` registrations are ready. Failures during setup
unwind whatever has already been allocated. Each connection's event token
includes a generation, so an old event cannot be mistaken for a new connection
that happened to reuse the same file descriptor.

The implementation is mostly in `src/main.c`. That's convenient for following
the event loop, but it is large and would benefit from separating socket
lifecycle, forwarding, and backend policy into modules.

## Data path

There are two directions per session:

```text
client fd  -> splice() -> client-to-backend pipe -> splice() -> backend fd
backend fd -> splice() -> backend-to-client pipe -> splice() -> client fd
```

Each pipe has a read and write descriptor created with
`pipe2(O_NONBLOCK | O_CLOEXEC)`. The proxy counts the bytes currently in the
pipe; it cannot assume `splice()` transferred the requested length.

If `splice()` returns `EAGAIN` when filling a pipe, the source must wait. If a
pipe-to-socket transfer blocks, its destination needs `EPOLLOUT`. Source reads
are held back until there is space again. The managed pipe/application queue
budget is 64 KiB per direction; **that is not a bound on Linux TCP socket
buffer memory**.

A client can send some bytes while the backend connection is still pending.
Those bytes go into an 8 KiB preconnect buffer and are drained once `connect()`
completes. Consequently, "zero-copy" describes the main connected path, not
literally every byte handled by the process.

## FIN, reset, and cleanup

TCP's two directions are independent. A `recv()` or `splice()` result of zero
confirms EOF for a source. An `EPOLLRDHUP` notification is useful, but doesn't
allow the proxy to discard data still waiting in a pipe. It forwards FIN with
`shutdown(..., SHUT_WR)` only after that direction has drained.

A connection is done normally when both directions have forwarded FIN and no
pending bytes remain. A reset or unrecoverable socket error follows a separate
abort path. Cleanup removes the `epoll` registrations, closes the sockets and
all pipe descriptors, and releases the selected backend's active count once.
The tests cover partial setup failures, descriptor reuse, half-closes, and RST.

## Backend policy and recovery

Round Robin walks the backend list cyclically. Least Connections counts active
sessions, including pending upstream handshakes, and rotates tie-breaking
between equally loaded backends.

A refused or timed-out handshake puts the backend in a cooldown. When the
cooldown expires, a later connection can try it again. This is a lightweight
passive failure mechanism, **not** continuous health monitoring. The proxy does
not replay an interrupted TCP stream to another backend: an upstream may have
already consumed and acted on part of the request.

## Timers and shutdown

Deadlines are based on `CLOCK_MONOTONIC`, not wall-clock time. The next deadline
also determines the timeout passed to `epoll_wait()`, allowing idle sessions and
stalled handshakes to expire without extra timer threads.

SIGINT and SIGTERM arrive through `signalfd`. Once shutdown begins, the listener
stops accepting clients. Existing sessions can finish during the configured
grace period; anything left at the deadline is closed. This keeps signal
handling in the ordinary event-loop code rather than touching connection memory
from an asynchronous signal handler.

## Things I would revisit

- Split the monolithic `src/main.c` into smaller modules.
- Add IPv6 and an explicit configuration file, if needed.
- Use an independent backend-health monitor only if the target deployment
  needs more than passive handshake failures.
- Benchmark on separate machines with a realistic network and backend rather
  than treating localhost numbers as universal.
