# Architecture and Operating Invariants

## 1. Connection-level routing

The process accepts on a nonblocking TCP listen socket and holds an `epoll` file descriptor containing registered client/backend endpoints and a Linux `signalfd`. Multiple backends are represented by IPv4 addresses, ports, selection counters, active-connection counts and a recovery timestamp.

- **Round Robin** chooses the next selectable backend in a rotation. It does not react to connection lifetimes.
- **Least Connections** chooses the fewest active connections, counting backend handshakes that have started. Rotating tie-breaking prevents perpetually preferring backend zero when equal.
- A backend whose TCP connect failed enters a configured cooldown. A subsequent request may probe it after that delay. Application-level health is **not** checked.
- Accepted connections cannot be transparently retried after arbitrary forwarding has started: TCP is an arbitrary byte stream and the application may have performed irreversible work.

## 2. Socket and direction lifecycle

Each proxy connection owns:

- a client socket and an outbound backend socket (initially `CONNECTING`, then `READY`);
- two independent `direction` objects with separate read/write bookkeeping;
- two `pipe2(O_NONBLOCK|O_CLOEXEC)` pipe pairs, one for each forwarding direction;
- preconnect userspace buffering, counters and EOF/FIN states;
- a generation-based event token used with the endpoint lookup table.

A candidate begins `INITIALIZING` and becomes `ACTIVE` only after socket/pipe setup and `epoll` registration succeed. Cleanup moves it to `CLOSING`, removes registration/lookup state, closes every owned descriptor, and releases its backend active count exactly once. Fault injection tests verify partial initialization rollback.

## 3. Bidirectional `splice()` forwarding

```
client socket -> splice(socket, pipe write) -> pipe read -> splice(pipe, socket) -> backend
backend socket -> splice(socket, pipe write) -> pipe read -> splice(pipe, socket) -> client
```

`splice()` is Linux-specific, can transfer fewer bytes than requested, and may return `EAGAIN`. The proxy tracks actual bytes in the pipe, and never assumes that one read/write notification implies a complete transfer. The `epoll` write interest is enabled when there are pending bytes and removed after draining. A full direction queue pauses source reads; successful destination progress resumes it.

During a backend handshake, the proxy may stage a limited amount of client bytes via `recv()` and later drain them with `send()`. Therefore *not every byte* necessarily uses zero-copy `splice()`. `splice()` also does not guarantee zero physical CPU or kernel-copy cost on all Linux configurations.

## 4. TCP half-close and completion

TCP is full-duplex: the client may `shutdown(SHUT_WR)` and continue reading while the backend still sends. A source EOF is observed only when a `recv()` or `splice()` indicates no more input. `EPOLLRDHUP` is a notification hint, not permission to drop pending data.

Each direction must drain its staged and pipe-resident data before forwarding FIN with `shutdown(destination, SHUT_WR)`. Connection completion requires **both** directions to finish, not simply the first FIN or HUP event. Abrupt TCP reset (`RST`) is a fatal session error; only that connection is cleaned up.

## 5. Timeouts and operations

- Backend handshakes have a configurable timeout; idle byte-progress has an independent timeout based on `CLOCK_MONOTONIC`.
- `epoll_wait()` timeout follows the nearest relevant deadline; deadline expiry cleans up a session without requiring more network traffic.
- The active connection cap defaults to 10,000 but must be verified against OS limits and actual workload.
- SIGINT/SIGTERM are consumed via `signalfd`; the listener stops accepting and active sessions are given a configurable drain interval before forced cleanup.
- Avoid capturing verbose proxy logs to a pipe that no process reads. Under load, a full log pipe can stall the event loop. Tests capture logs to regular files.

## 6. Not included

IPv6, UDP, TLS termination, HTTP-aware routing, dynamic service discovery, backend weights, application health checks, process supervision, authentication, distributed consensus and automatic replay of failed requests are not implemented. The project demonstrates foundational Linux network and event-loop engineering rather than claiming to be an out-of-the-box production ingress service.
