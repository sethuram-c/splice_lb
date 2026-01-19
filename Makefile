CC ?= cc
CFLAGS ?= -std=c11 -O2 -g -Wall -Wextra -Wpedantic -Werror
CPPFLAGS ?=
LDFLAGS ?=
LDLIBS ?=
SERVER := build/splice-lb
CLIENT := build/backend-client
ECHO := build/echo-backend
SPLICE_DEMO := build/splice-demo
SETUP_FAULTS := build/setup-faults.so
TIMEOUT_INJECT := build/timeout-inject.so

.PHONY: all clean test run
all: $(SERVER) $(CLIENT) $(ECHO) $(SPLICE_DEMO)

$(SERVER): src/main.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

$(SPLICE_DEMO): src/splice_demo.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

$(CLIENT): src/backend_client.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

$(ECHO): src/echo_backend.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

# Test-only fault injector, never linked into production executable.
$(SETUP_FAULTS): tests/setup_faults.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) -fPIC -shared $< -o $@

.PHONY: test-config
test-config: all
	python3 tests/test_cli_config.py

.PHONY: test-setup-failures
test-setup-failures: all $(SETUP_FAULTS)
	python3 tests/test_setup_failures.py

$(TIMEOUT_INJECT): tests/timeout_inject.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) -fPIC -shared $< -o $@

.PHONY: test-timeouts
test-timeouts: all $(TIMEOUT_INJECT)
	python3 tests/test_timeouts.py

# Terminal A: make backend (or ./build/echo-backend 9091)
# Terminal B: make run
.PHONY: backend
backend: $(ECHO)
	./$(ECHO) 9091

backend-multi:
	python3 tests/backend_multi.py 9091

.PHONY: backend-rr
backend-rr:
	python3 tests/rr_backends.py 9091 9092 9093

.PHONY: backend-multi

run: $(SERVER)
	./$(SERVER) 9090 127.0.0.1 9091

test: all $(SETUP_FAULTS) $(TIMEOUT_INJECT)
	python3 tests/test_adversarial_peers.py
	python3 tests/test_fd_leaks.py
	python3 tests/test_algorithm_stress.py
	python3 tests/test_concurrency_scale.py --connections 100
	python3 tests/test_harness.py
	python3 tests/test_basic_payloads.py
	python3 tests/test_large_transfers.py
	python3 tests/test_listener.py
	python3 tests/test_echo.py
	python3 tests/test_proxy.py
	python3 tests/test_epoll.py
	python3 tests/test_multiclient.py
	python3 tests/test_async_connect.py
	python3 tests/test_preconnect_buffer.py
	python3 tests/test_bidirectional_buffers.py
	python3 tests/test_splice_pipes.py
	python3 tests/test_client_splice.py
	python3 tests/test_full_splice.py
	python3 tests/test_pipe_fill.py
	python3 tests/test_pipe_drain.py
	python3 tests/test_splice_integrity.py
	python3 tests/test_round_robin.py
	python3 tests/test_least_connections.py
	python3 tests/test_algorithm_comparison.py
	python3 tests/test_backend_failures.py
	python3 tests/test_backpressure_duplex.py
	python3 tests/test_client_eof.py
	python3 tests/test_backend_eof.py
	python3 tests/test_abrupt_disconnects.py
	python3 tests/test_setup_failures.py
	python3 tests/test_cli_config.py
	python3 tests/test_timeouts.py
	python3 tests/test_operational_controls.py
	python3 tests/test_benchmark.py
	python3 tests/test_release_demo.py

.PHONY: test-operations
test-operations: all
	python3 tests/test_operational_controls.py

.PHONY: test-basic
test-basic: all
	python3 tests/test_harness.py
	python3 tests/test_basic_payloads.py

clean:
	rm -rf build

.PHONY: test-large
test-large: all
	python3 tests/test_large_transfers.py

# Default scale certification target. 5k/10k are explicit opt-in runs.
.PHONY: test-scale test-scale-5k test-scale-10k
test-scale: all
	python3 tests/test_concurrency_scale.py --connections 1000 --report /tmp/phase30-1k.json

test-scale-5k: all
	python3 tests/test_concurrency_scale.py --connections 5000 --report /tmp/phase30-5k.json

test-scale-10k: all
	python3 tests/test_concurrency_scale.py --connections 10000 --report /tmp/phase30-10k.json

# Phase 31: controlled unequal-lifetime distribution stress.
.PHONY: test-algorithm-stress
test-algorithm-stress: all
	python3 tests/test_algorithm_stress.py

# Phase 32: mixed slow peers, half-closes, resets, and backend recovery.
.PHONY: test-adversarial
test-adversarial: all
	python3 tests/test_adversarial_peers.py

# Phase 33: instrumented binaries are separate from the production build.
# Using a non-PIE sanitizer binary avoids ASan shadow/PIE mapping collisions
# observed on some Linux/container ASLR configurations.
SANITIZER_FLAGS := -std=c11 -O1 -g -Wall -Wextra -Wpedantic -Werror \
                   -fno-omit-frame-pointer -fno-pie -fsanitize=address,undefined
SANITIZER_LDFLAGS := -no-pie -fsanitize=address,undefined
SANITIZED_SERVER := build/sanitize/splice-lb
SANITIZED_DEMO := build/sanitize/splice-demo

$(SANITIZED_SERVER): src/main.c
	@mkdir -p build/sanitize
	$(CC) $(CPPFLAGS) $(SANITIZER_FLAGS) -DSANITIZER_TEST_EXIT $< -o $@ $(SANITIZER_LDFLAGS)

$(SANITIZED_DEMO): src/splice_demo.c
	@mkdir -p build/sanitize
	$(CC) $(CPPFLAGS) $(SANITIZER_FLAGS) $< -o $@ $(SANITIZER_LDFLAGS)

.PHONY: sanitize-build test-sanitizers test-fd-leaks
sanitize-build: $(SANITIZED_SERVER) $(SANITIZED_DEMO)

test-sanitizers: sanitize-build
	python3 tests/test_sanitizers.py

test-fd-leaks: all
	python3 tests/test_fd_leaks.py

# Phase 34 - comparison-only blocking thread-per-connection reference.
# It is intentionally not linked into the production splice-lb executable.
BLOCKING_BASELINE := build/blocking-baseline
$(BLOCKING_BASELINE): benchmarks/blocking_baseline.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) -pthread $< -o $@ $(LDFLAGS) -pthread $(LDLIBS)

.PHONY: test-benchmark benchmark benchmark-quick
# Small deterministic functional smoke. Timing numbers are not pass/fail criteria.
test-benchmark: all $(BLOCKING_BASELINE)
	python3 tests/test_benchmark.py

benchmark-quick: all $(BLOCKING_BASELINE)
	python3 benchmarks/bench_forwarding.py --quick --output /tmp/phase34-quick.json

benchmark: all $(BLOCKING_BASELINE)
	python3 benchmarks/bench_forwarding.py --output /tmp/phase34-benchmark.json

# Integrate Phase 34 smoke test into the full regression suite.
test: $(BLOCKING_BASELINE)

# Phase 35: deterministic, self-contained final demonstration. No root or
# external network connection required. JSON reports belong under /tmp.
.PHONY: demo test-release verify-release

demo: all
	python3 demo/final_demo.py --algorithm both --report /tmp/phase35-demo.json

test-release: all
	python3 tests/test_release_demo.py

# Full release gate; can be long and should be run locally on Fedora.
verify-release: all $(SETUP_FAULTS) $(TIMEOUT_INJECT)
	$(MAKE) test-release
	$(MAKE) test
	$(MAKE) test-sanitizers
	$(MAKE) test-benchmark
