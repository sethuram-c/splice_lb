#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

#define LISTEN_BACKLOG 16
#define COPY_BUFFER_SIZE 8192U

static bool parse_port(const char *text, bool allow_zero, uint16_t *port) {
    if (text == NULL || *text == '\0') {
        return false;
    }
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
        if (*p < '0' || *p > '9') {
            return false;
        }
    }
    errno = 0;
    char *end = NULL;
    unsigned long value = strtoul(text, &end, 10);
    if (errno != 0 || *end != '\0' || value > UINT16_MAX ||
        (!allow_zero && value == 0)) {
        return false;
    }
    *port = (uint16_t)value;
    return true;
}

static void report_error(const char *call) {
    fprintf(stderr, "%s failed: %s\n", call, strerror(errno));
}

static bool write_all(int destination, const unsigned char *data, size_t length) {
    size_t offset = 0;
    while (offset < length) {
        ssize_t sent = send(destination, data + offset, length - offset,
                            MSG_NOSIGNAL);
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

/* A single blocking send_all() can stall both directions: fixed in Phase 5+. */
static bool forward_once(int source, int destination, bool *source_open,
                         uint64_t *byte_count) {
    unsigned char buffer[COPY_BUFFER_SIZE];
    ssize_t count;
    do {
        count = recv(source, buffer, sizeof(buffer), 0);
    } while (count < 0 && errno == EINTR);

    if (count < 0) {
        report_error("recv()");
        return false;
    }
    if (count == 0) {
        /* EOF is one-way. Deliver FIN only after previously forwarded bytes. */
        *source_open = false;
        if (shutdown(destination, SHUT_WR) < 0) {
            report_error("shutdown(SHUT_WR)");
            return false;
        }
        return true;
    }
    if (!write_all(destination, buffer, (size_t)count)) {
        return false;
    }
    *byte_count += (uint64_t)count;
    return true;
}

static bool relay_bidirectional(int client, int backend,
                                uint64_t *to_backend, uint64_t *to_client) {
    bool client_read_open = true;
    bool backend_read_open = true;
    if (client >= FD_SETSIZE || backend >= FD_SETSIZE) {
        fputs("socket descriptor exceeds select() FD_SETSIZE\n", stderr);
        return false;
    }

    while (client_read_open || backend_read_open) {
        fd_set read_ready;
        FD_ZERO(&read_ready);
        if (client_read_open) {
            FD_SET(client, &read_ready);
        }
        if (backend_read_open) {
            FD_SET(backend, &read_ready);
        }
        int highest = client > backend ? client : backend;
        int ready = select(highest + 1, &read_ready, NULL, NULL, NULL);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            report_error("select()");
            return false;
        }
        if (client_read_open && FD_ISSET(client, &read_ready) &&
            !forward_once(client, backend, &client_read_open, to_backend)) {
            return false;
        }
        if (backend_read_open && FD_ISSET(backend, &read_ready) &&
            !forward_once(backend, client, &backend_read_open, to_client)) {
            return false;
        }
    }
    return true;
}

int main(int argc, char **argv) {
    uint16_t listen_port;
    uint16_t backend_port;
    struct sockaddr_in backend_address = {0};
    if (argc != 4 || !parse_port(argv[1], true, &listen_port) ||
        !parse_port(argv[3], false, &backend_port) ||
        inet_pton(AF_INET, argv[2], &backend_address.sin_addr) != 1) {
        fprintf(stderr, "Usage: %s <listen-port: 0-65535> <backend-IPv4> "
                        "<backend-port: 1-65535>\n", argv[0]);
        return EXIT_FAILURE;
    }
    backend_address.sin_family = AF_INET;
    backend_address.sin_port = htons(backend_port);

    int listener = -1;
    int client = -1;
    int backend = -1;
    int result = EXIT_FAILURE;

    listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) {
        report_error("socket(listener)");
        goto cleanup;
    }
    const int reuse = 1;
    if (setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse)) < 0) {
        report_error("setsockopt(SO_REUSEADDR)");
        goto cleanup;
    }
    struct sockaddr_in local = {0};
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    local.sin_port = htons(listen_port);
    if (bind(listener, (const struct sockaddr *)&local, sizeof(local)) < 0) {
        report_error("bind()");
        goto cleanup;
    }
    if (listen(listener, LISTEN_BACKLOG) < 0) {
        report_error("listen()");
        goto cleanup;
    }
    socklen_t local_size = sizeof(local);
    if (getsockname(listener, (struct sockaddr *)&local, &local_size) < 0) {
        report_error("getsockname()");
        goto cleanup;
    }
    printf("Listening on 127.0.0.1:%u -> %s:%u\n",
           (unsigned)ntohs(local.sin_port), argv[2], (unsigned)backend_port);
    fflush(stdout);

    struct sockaddr_in remote_client = {0};
    socklen_t remote_size = sizeof(remote_client);
    do {
        client = accept(listener, (struct sockaddr *)&remote_client, &remote_size);
    } while (client < 0 && errno == EINTR);
    if (client < 0) {
        report_error("accept()");
        goto cleanup;
    }
    char ip_text[INET_ADDRSTRLEN];
    if (inet_ntop(AF_INET, &remote_client.sin_addr,
                  ip_text, sizeof(ip_text)) == NULL) {
        report_error("inet_ntop()");
        goto cleanup;
    }
    printf("Accepted client %s:%u (fd=%d)\n", ip_text,
           (unsigned)ntohs(remote_client.sin_port), client);
    fflush(stdout);

    backend = socket(AF_INET, SOCK_STREAM, 0);
    if (backend < 0) {
        report_error("socket(backend)");
        goto cleanup;
    }
    /* An interrupted connect may still be in progress; do not blindly retry.
       Phase 7 will manage EINPROGRESS / SO_ERROR using nonblocking I/O. */
    if (connect(backend, (const struct sockaddr *)&backend_address,
                sizeof(backend_address)) < 0) {
        report_error("connect(backend)");
        goto cleanup;
    }
    puts("Connected to backend");
    fflush(stdout);

    uint64_t to_backend = 0;
    uint64_t to_client = 0;
    if (!relay_bidirectional(client, backend, &to_backend, &to_client)) {
        result = EXIT_FAILURE;
        goto cleanup;
    }
    printf("Forwarded client-to-backend=%" PRIu64 " bytes, "
           "backend-to-client=%" PRIu64 " bytes\n", to_backend, to_client);
    result = EXIT_SUCCESS;

cleanup:
    if (backend >= 0 && close(backend) < 0) {
        report_error("close(backend)");
        result = EXIT_FAILURE;
    }
    if (client >= 0 && close(client) < 0) {
        report_error("close(client)");
        result = EXIT_FAILURE;
    }
    if (listener >= 0 && close(listener) < 0) {
        report_error("close(listener)");
        result = EXIT_FAILURE;
    }
    if (result == EXIT_SUCCESS) {
        puts("Closed client, backend, and listening sockets");
    }
    return result;
}
