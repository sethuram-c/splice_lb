#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <unistd.h>

#define LISTEN_BACKLOG 16
#define COPY_BUFFER_SIZE 8192U
#define EVENT_BATCH 4

struct direction {
    int source;
    int destination;
    unsigned char buffer[COPY_BUFFER_SIZE];
    size_t offset;
    size_t pending;
    uint64_t forwarded;
    bool source_eof;
    bool destination_shutdown;
};

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

/* Keep existing descriptor flags: fcntl(F_SETFL) must not overwrite them. */
static bool set_nonblocking(int fd) {
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
        report_error("fcntl(O_NONBLOCK)");
        return false;
    }
    return true;
}

static bool add_interest(int epoll_fd, int fd, uint32_t events) {
    struct epoll_event event = {.events = events, .data.fd = fd};
    if (epoll_ctl(epoll_fd, EPOLL_CTL_ADD, fd, &event) < 0) {
        report_error("epoll_ctl(ADD)");
        return false;
    }
    return true;
}

static bool modify_interest(int epoll_fd, int fd, uint32_t events) {
    struct epoll_event event = {.events = events, .data.fd = fd};
    if (epoll_ctl(epoll_fd, EPOLL_CTL_MOD, fd, &event) < 0) {
        report_error("epoll_ctl(MOD)");
        return false;
    }
    return true;
}

/* A readable, nonblocking listener can still return EAGAIN: this is normal. */
static int accept_single_client(int epoll_fd, int listener) {
    struct epoll_event events[EVENT_BATCH];
    for (;;) {
        int ready = epoll_wait(epoll_fd, events, EVENT_BATCH, -1);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            report_error("epoll_wait(listener)");
            return -1;
        }
        for (int i = 0; i < ready; ++i) {
            if (events[i].data.fd != listener) {
                continue;
            }
            if (events[i].events & EPOLLERR) {
                fputs("listener reported EPOLLERR\n", stderr);
                return -1;
            }
            struct sockaddr_in remote = {0};
            socklen_t length = sizeof(remote);
            int client;
            do {
                client = accept(listener, (struct sockaddr *)&remote, &length);
            } while (client < 0 && errno == EINTR);
            if (client < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                continue;
            }
            if (client < 0) {
                report_error("accept()");
                return -1;
            }
            char text[INET_ADDRSTRLEN];
            if (inet_ntop(AF_INET, &remote.sin_addr, text, sizeof(text)) == NULL) {
                report_error("inet_ntop()");
                close(client);
                return -1;
            }
            printf("Accepted client %s:%u (fd=%d)\n", text,
                   (unsigned)ntohs(remote.sin_port), client);
            fflush(stdout);
            return client;
        }
    }
}

/* A FIN can be relayed only after any buffered bytes are sent. */
static bool finish_direction(struct direction *direction) {
    if (!direction->source_eof || direction->pending != 0 ||
        direction->destination_shutdown) {
        return true;
    }
    if (shutdown(direction->destination, SHUT_WR) < 0) {
        report_error("shutdown(SHUT_WR)");
        return false;
    }
    direction->destination_shutdown = true;
    return true;
}

static bool flush_direction(struct direction *direction) {
    while (direction->pending != 0) {
        ssize_t written = send(direction->destination,
                               direction->buffer + direction->offset,
                               direction->pending, MSG_NOSIGNAL);
        if (written > 0) {
            direction->offset += (size_t)written;
            direction->pending -= (size_t)written;
            direction->forwarded += (uint64_t)written;
            continue;
        }
        if (written < 0 && errno == EINTR) {
            continue;
        }
        if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return true; /* Keep pending bytes; epoll will notify writability. */
        }
        if (written == 0) {
            fputs("send() made no progress\n", stderr);
        } else {
            report_error("send()");
        }
        return false;
    }
    direction->offset = 0;
    return finish_direction(direction);
}

/* Read at most one buffer: never overwrite data queued for the destination. */
static bool read_direction(struct direction *direction) {
    if (direction->source_eof || direction->pending != 0) {
        return true;
    }
    for (;;) {
        ssize_t count = recv(direction->source, direction->buffer,
                             sizeof(direction->buffer), 0);
        if (count > 0) {
            direction->offset = 0;
            direction->pending = (size_t)count;
            return flush_direction(direction);
        }
        if (count == 0) {
            direction->source_eof = true;
            return finish_direction(direction);
        }
        if (errno == EINTR) {
            continue;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            return true;
        }
        report_error("recv()");
        return false;
    }
}

/* Level-triggered epoll; EPOLLOUT is armed only while a write is pending. */
static bool update_interests(int epoll_fd, int client, int backend,
                             const struct direction *to_backend,
                             const struct direction *to_client) {
    uint32_t client_events = 0;
    uint32_t backend_events = 0;
    if (!to_backend->source_eof && to_backend->pending == 0) {
        client_events |= EPOLLIN;
    }
    if (!to_client->source_eof && to_client->pending == 0) {
        backend_events |= EPOLLIN;
    }
    if (to_client->pending != 0) {
        client_events |= EPOLLOUT;
    }
    if (to_backend->pending != 0) {
        backend_events |= EPOLLOUT;
    }
    return modify_interest(epoll_fd, client, client_events) &&
           modify_interest(epoll_fd, backend, backend_events);
}

static bool relay_bidirectional(int epoll_fd, int client, int backend,
                                uint64_t *to_backend, uint64_t *to_client) {
    struct direction client_to_backend = {
        .source = client, .destination = backend
    };
    struct direction backend_to_client = {
        .source = backend, .destination = client
    };
    if (!add_interest(epoll_fd, client, EPOLLIN) ||
        !add_interest(epoll_fd, backend, EPOLLIN)) {
        return false;
    }
    struct epoll_event events[EVENT_BATCH];
    while (!client_to_backend.destination_shutdown ||
           !backend_to_client.destination_shutdown) {
        int ready = epoll_wait(epoll_fd, events, EVENT_BATCH, -1);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            report_error("epoll_wait(relay)");
            return false;
        }
        for (int i = 0; i < ready; ++i) {
            const int fd = events[i].data.fd;
            const uint32_t flags = events[i].events;
            if (fd != client && fd != backend) {
                fputs("unexpected epoll fd\n", stderr);
                return false;
            }
            struct direction *incoming = fd == client ?
                                         &client_to_backend : &backend_to_client;
            struct direction *outgoing = fd == client ?
                                         &backend_to_client : &client_to_backend;
            if ((flags & EPOLLOUT) && !flush_direction(outgoing)) {
                return false;
            }
            /* EPOLLHUP and EPOLLERR require a read/response, not silent drop. */
            if ((flags & (EPOLLIN | EPOLLHUP | EPOLLERR)) &&
                !read_direction(incoming)) {
                return false;
            }
        }
        if (!update_interests(epoll_fd, client, backend,
                              &client_to_backend, &backend_to_client)) {
            return false;
        }
    }
    *to_backend = client_to_backend.forwarded;
    *to_client = backend_to_client.forwarded;
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
    int epoll_fd = -1;
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
    if (!set_nonblocking(listener)) {
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
    epoll_fd = epoll_create1(EPOLL_CLOEXEC);
    if (epoll_fd < 0) {
        report_error("epoll_create1()");
        goto cleanup;
    }
    if (!add_interest(epoll_fd, listener, EPOLLIN)) {
        goto cleanup;
    }
    printf("Listening on 127.0.0.1:%u -> %s:%u\n",
           (unsigned)ntohs(local.sin_port), argv[2], (unsigned)backend_port);
    fflush(stdout);

    client = accept_single_client(epoll_fd, listener);
    if (client < 0 || !set_nonblocking(client)) {
        goto cleanup;
    }
    if (epoll_ctl(epoll_fd, EPOLL_CTL_DEL, listener, NULL) < 0) {
        report_error("epoll_ctl(DEL listener)");
        goto cleanup;
    }
    backend = socket(AF_INET, SOCK_STREAM, 0);
    if (backend < 0) {
        report_error("socket(backend)");
        goto cleanup;
    }
    /* Still blocking at this stage; Phase 7 adds EINPROGRESS + SO_ERROR. */
    if (connect(backend, (const struct sockaddr *)&backend_address,
                sizeof(backend_address)) < 0) {
        report_error("connect(backend)");
        goto cleanup;
    }
    if (!set_nonblocking(backend)) {
        goto cleanup;
    }
    puts("Connected to backend");
    fflush(stdout);

    uint64_t to_backend = 0;
    uint64_t to_client = 0;
    if (!relay_bidirectional(epoll_fd, client, backend,
                             &to_backend, &to_client)) {
        goto cleanup;
    }
    printf("Forwarded client-to-backend=%" PRIu64 " bytes, "
           "backend-to-client=%" PRIu64 " bytes\n", to_backend, to_client);
    result = EXIT_SUCCESS;

cleanup:
    if (epoll_fd >= 0 && close(epoll_fd) < 0) {
        report_error("close(epoll)");
        result = EXIT_FAILURE;
    }
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
