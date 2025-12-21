CC ?= cc
CFLAGS ?= -std=c11 -O2 -g -Wall -Wextra -Wpedantic -Werror
CPPFLAGS ?=
LDFLAGS ?=
LDLIBS ?=
SERVER := build/splice-lb
CLIENT := build/backend-client
ECHO := build/echo-backend

.PHONY: all clean test run
all: $(SERVER) $(CLIENT) $(ECHO)

$(SERVER): src/main.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

$(CLIENT): src/backend_client.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

$(ECHO): src/echo_backend.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

# Terminal A: make backend (or ./build/echo-backend 9091)
# Terminal B: make run
.PHONY: backend
backend: $(ECHO)
	./$(ECHO) 9091

backend-multi:
	python3 tests/backend_multi.py 9091

.PHONY: backend-multi

run: $(SERVER)
	./$(SERVER) 9090 127.0.0.1 9091

test: all
	python3 tests/test_listener.py
	python3 tests/test_echo.py
	python3 tests/test_proxy.py
	python3 tests/test_epoll.py
	python3 tests/test_multiclient.py
	python3 tests/test_async_connect.py

clean:
	rm -rf build
