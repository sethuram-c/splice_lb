# Release checklist

A few checks I want completed on the Fedora machine before treating a tag as a
verified release.

## Build and tests

- [ ] `make clean && make` completes with `-Wall -Wextra -Wpedantic -Werror`.
- [ ] `make test-release` and `make demo` pass.
- [ ] `make test` finishes in one run; investigate any intermittent failures.
- [ ] `make test-sanitizers` and `make test-fd-leaks` pass.
- [ ] `make test-benchmark` passes; retain the output of `make benchmark`.
- [ ] `make test-scale` passes on Fedora. Record system limits before trying 5k/10k.
- [ ] Inspect `git diff --check` and `git status --short` before committing.

## Manual checks

- [ ] Try both `--algorithm=round-robin` and `--algorithm=least-connections`.
- [ ] Stop a backend and confirm the listener still accepts healthy clients.
- [ ] Send Ctrl+C during a live connection and inspect graceful shutdown.
- [ ] Review open descriptors with `/proc/<pid>/fd` after clients disconnect.

## Repo housekeeping

- [ ] Ensure `build/`, bytecode, benchmark JSON, and learning PDFs aren't staged.
- [ ] Check documentation examples against `./build/splice-lb --help`.
- [ ] Add a license if I decide to publish under an open-source license.
- [ ] Review repository for machine-specific paths or anything private.
- [ ] Tag only after the above checks are complete.

Not currently implemented: TLS termination, HTTP parsing, IPv6, UDP, active
backend health checks, or failover of an already-established TCP stream.
