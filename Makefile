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

.PHONY: test-operations
test-operations: all
	python3 tests/test_operational_controls.py

clean:
	rm -rf build
