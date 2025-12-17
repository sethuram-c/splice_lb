#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define READ_BUFFER_SIZE 4096U

static bool parse_remote_port(const char *text, uint16_t *port) {
    if (text == NULL || *text == '\0') {
        return false;
    }
    for (const unsigned char *p = (const unsigned char *)text; *p != '\0'; ++p) {
        if (*p < '0' || *p > '9') {
            return false;
        }
    }
    errno = 0;
    char *end = NULL;
    unsigned long value = strtoul(text, &end, 10);
    if (errno != 0 || *end != '\0' || value == 0 || value > UINT16_MAX) {
        return false;
    }
    *port = (uint16_t)value;
    return true;
}

static void report_error(const char *operation) {
    fprintf(stderr, "%s failed: %s\n", operation, strerror(errno));
}

/* send() can transfer fewer bytes than requested even on blocking sockets. */
static bool send_all(int socket_fd, const unsigned char *data, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        ssize_t sent = send(socket_fd, data + offset, size - offset, MSG_NOSIGNAL);
        if (sent < 0) {
            if (errno == EINTR) {
                continue;
            }
            report_error("send()");
            return false;
        }
        if (sent == 0) {
            fputs("send() returned zero without progress\n", stderr);
            return false;
        }
        offset += (size_t)sent;
    }
    return true;
}

static bool read_echo_to_stdout(int socket_fd) {
    unsigned char buffer[READ_BUFFER_SIZE];
    for (;;) {
        ssize_t got = recv(socket_fd, buffer, sizeof(buffer), 0);
        if (got == 0) {
            return true;
        }
        if (got < 0) {
            if (errno == EINTR) {
                continue;
            }
            report_error("recv()");
            return false;
        }
        size_t offset = 0;
        while (offset < (size_t)got) {
            ssize_t written = write(STDOUT_FILENO, buffer + offset,
                                    (size_t)got - offset);
            if (written < 0) {
                if (errno == EINTR) {
                    continue;
                }
                report_error("write(stdout)");
                return false;
            }
            if (written == 0) {
                fputs("write(stdout) returned zero without progress\n", stderr);
                return false;
            }
            offset += (size_t)written;
        }
    }
}

int main(int argc, char **argv) {
    uint16_t port;
    if (argc != 4 || !parse_remote_port(argc > 2 ? argv[2] : NULL, &port)) {
        fprintf(stderr, "Usage: %s <IPv4-address> <port: 1-65535> <payload>\n", argv[0]);
        return EXIT_FAILURE;
    }

    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    if (inet_pton(AF_INET, argv[1], &address.sin_addr) != 1) {
        fprintf(stderr, "Invalid IPv4 address: %s\n", argv[1]);
        return EXIT_FAILURE;
    }

    int socket_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (socket_fd < 0) {
        report_error("socket()");
        return EXIT_FAILURE;
    }

    int result = EXIT_FAILURE;
    if (connect(socket_fd, (const struct sockaddr *)&address, sizeof(address)) < 0) {
        report_error("connect()");
        goto cleanup;
    }

    const unsigned char *payload = (const unsigned char *)argv[3];
    if (!send_all(socket_fd, payload, strlen(argv[3]))) {
        goto cleanup;
    }
    /* Announce request completion but keep the read direction open for echo. */
    if (shutdown(socket_fd, SHUT_WR) < 0) {
        report_error("shutdown(SHUT_WR)");
        goto cleanup;
    }
    if (!read_echo_to_stdout(socket_fd)) {
        goto cleanup;
    }
    result = EXIT_SUCCESS;

cleanup:
    if (close(socket_fd) < 0) {
        report_error("close(socket)");
        result = EXIT_FAILURE;
    }
    return result;
}
