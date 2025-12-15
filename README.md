# Splice Load Balancer (Linux / C)

A phase-by-phase educational TCP load balancer project. **Phase 1 only:**
create and close an IPv4 TCP socket. No listener, proxying, or load balancing yet.

## Fedora prerequisites

```sh
sudo dnf install gcc make bash git
```

## Build and verify

```sh
make clean
make
make run
make test
```

Expected `make run` output (the file descriptor number varies):

```text
TCP socket created successfully (fd=3)
TCP socket closed successfully
```

Expected final line from `make test`: `PASS: TCP socket lifecycle smoke test`.

## Project structure

- `src/main.c`: socket lifecycle demo.
- `tests/test_socket.sh`: minimal smoke test.
- `Makefile`: build, run, test, and clean targets.
- `progress.md`: phase completion log.

Technical notes and Linux command explanations are delivered as a **separate PDF** and are deliberately not committed to the repository.
