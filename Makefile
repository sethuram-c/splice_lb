CC ?= cc
CFLAGS ?= -std=c11 -O2 -g -Wall -Wextra -Wpedantic -Werror
CPPFLAGS ?=
LDFLAGS ?=
LDLIBS ?=
TARGET := build/splice-lb

.PHONY: all clean test run
all: $(TARGET)

$(TARGET): src/main.c
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

# Blocks until one TCP client connects; see README for second-terminal command.
run: $(TARGET)
	./$(TARGET) 9090

test: $(TARGET)
	python3 tests/test_listener.py

clean:
	rm -rf build
