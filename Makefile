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

run: $(TARGET)
	./$(TARGET)

test: $(TARGET)
	bash tests/test_socket.sh

clean:
	rm -rf build
