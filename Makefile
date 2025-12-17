CC ?= cc
CFLAGS ?= -std=c11 -O2 -g -Wall -Wextra -Wpedantic -Werror
CPPFLAGS ?=
LDFLAGS ?=
LDLIBS ?=
SERVER := build/splice-lb
CLIENT := build/backend-client

.PHONY: all clean test run
all: $(SERVER) $(CLIENT)

$(SERVER): src/main.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

$(CLIENT): src/backend_client.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

# Blocks until one client connects and then closes after echoing to EOF.
run: $(SERVER)
	./$(SERVER) 9090

test: all
	python3 tests/test_listener.py
	python3 tests/test_echo.py

clean:
	rm -rf build
