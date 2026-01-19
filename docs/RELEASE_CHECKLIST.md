# Final Release Checklist — Phase 35

## Code and correctness

- [x] `make clean && make` compiles the release candidate with strict warnings.
- [x] `make test-release` checks tagged routing, binary integrity and graceful shutdown.
- [x] `make demo` produces a JSON report outside the repository.
- [ ] Run **uninterrupted** `make test` on the target Fedora host.
- [ ] Run `make test-sanitizers` and `make test-fd-leaks` on Fedora.
- [ ] Run `make test-benchmark` and a controlled `make benchmark` on Fedora.
- [ ] Run 1,000 connections on Fedora; optionally test 5,000 and 10,000 with adequate limits.

## Documentation and repository hygiene

- [x] Document architecture, limitations, verification commands and evidence.
- [x] Preserve the full earlier README in `docs/PHASE_HISTORY.md`.
- [x] Keep learning PDFs out of ZIP and source control.
- [x] Use `.gitignore` for generated `build/` binaries, `*.pdf` and `*.zip`.
- [ ] Review `git diff --check`, `git status --short` and exact staged files.
- [ ] Add a license if publishing as open source; no license is inferred.
- [ ] Publish repository only after checking for personal paths, secrets and artifacts.

## Demo presentation (about 3–5 minutes; duration is an agenda, not work estimate)

1. Show `src/main.c` ownership of sockets, two pipes per connection and `epoll` readiness.
2. Run `make demo`, show Round Robin selections and Least Connections held-open test.
3. Explain that EOF in one direction does not end the other stream.
4. Show `make test-release`, one sanitizer result and a high-concurrency JSON report.
5. State the measured 1,000-session result and the unverified 5,000/10,000 targets without exaggeration.
6. End with bottlenecks, architecture tradeoffs and next improvements.

## Git commit

Scheduled December 19, 2026 (India Standard Time). Commit only after verification:

```bash
git add \
  Makefile \
  README.md \
  progress.md \
  demo/final_demo.py \
  tests/test_release_demo.py \
  docs/ARCHITECTURE.md \
  docs/VALIDATION.md \
  docs/RELEASE_CHECKLIST.md \
  docs/PHASE_HISTORY.md

git diff --cached --check
```

The personal learning PDF is separate and should not be staged.
