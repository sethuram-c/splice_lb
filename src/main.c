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

#define LISTEN_BACKLOG 128
#define COPY_BUFFER_SIZE 8192U
#define EVENT_BATCH 64

struct connection;
struct endpoint {
    int fd;
    uint32_t interest;
    uint64_t token;
    bool is_client;
    struct connection *owner;
};
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
struct connection {
    struct connection *next;
    struct endpoint client;
    struct endpoint backend;
    struct direction to_backend;
    struct direction to_client;
};
struct server {
    int listener;
    int epoll_fd;
    struct sockaddr_in backend_address;
    struct connection *connections;
    struct endpoint **by_fd;
    size_t fd_capacity;
    uint32_t next_generation;
    size_t active;
};

static bool parse_port(const char *text, bool allow_zero, uint16_t *port) {
    if (text == NULL || *text == '\0') return false;
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
        if (*p < '0' || *p > '9') return false;
    }
    errno = 0;
    char *end = NULL;
    unsigned long value = strtoul(text, &end, 10);
    if (errno || *end != '\0' || value > UINT16_MAX || (!allow_zero && value == 0)) {
        return false;
    }
    *port = (uint16_t)value;
    return true;
}

static void report_error(const char *call) {
    fprintf(stderr, "%s failed: %s\n", call, strerror(errno));
}

static bool set_nonblocking(int fd) {
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
        report_error("fcntl(O_NONBLOCK)");
        return false;
    }
    return true;
}

/* Store a generation with each fd.  An old event cannot be mistaken for a
 * newly accepted connection if Linux reuses a recently closed fd. */
static bool ensure_slot(struct server *server, int fd) {
    if (fd < 0) return false;
    if ((size_t)fd < server->fd_capacity) return true;
    size_t size = server->fd_capacity ? server->fd_capacity : 64;
    while (size <= (size_t)fd) {
        if (size > SIZE_MAX / 2) return false;
        size *= 2;
    }
    if (size > SIZE_MAX / sizeof(*server->by_fd)) return false;
    struct endpoint **next = realloc(server->by_fd, size * sizeof(*next));
    if (next == NULL) {
        report_error("realloc(fd lookup)");
        return false;
    }
    memset(next + server->fd_capacity, 0,
           (size - server->fd_capacity) * sizeof(*next));
    server->by_fd = next;
    server->fd_capacity = size;
    return true;
}

static bool register_endpoint(struct server *server, struct endpoint *endpoint,
                              uint32_t interest) {
    if (!ensure_slot(server, endpoint->fd)) return false;
    uint32_t generation = ++server->next_generation;
    if (generation == 0) generation = ++server->next_generation;
    endpoint->token = ((uint64_t)generation << 32) | (uint32_t)endpoint->fd;
    struct epoll_event event = {.events = interest, .data.u64 = endpoint->token};
    if (epoll_ctl(server->epoll_fd, EPOLL_CTL_ADD, endpoint->fd, &event) < 0) {
        report_error("epoll_ctl(ADD)");
        return false;
    }
    endpoint->interest = interest;
    server->by_fd[endpoint->fd] = endpoint;
    return true;
}

static bool change_interest(struct server *server, struct endpoint *endpoint,
                            uint32_t interest) {
    if (endpoint->interest == interest) return true;
    struct epoll_event event = {.events = interest, .data.u64 = endpoint->token};
    if (epoll_ctl(server->epoll_fd, EPOLL_CTL_MOD, endpoint->fd, &event) < 0) {
        report_error("epoll_ctl(MOD)");
        return false;
    }
    endpoint->interest = interest;
    return true;
}

static void release_endpoint(struct server *server, struct endpoint *endpoint) {
    if (endpoint->fd < 0) return;
    if ((size_t)endpoint->fd < server->fd_capacity &&
        server->by_fd[endpoint->fd] == endpoint) {
        server->by_fd[endpoint->fd] = NULL;
        if (epoll_ctl(server->epoll_fd, EPOLL_CTL_DEL, endpoint->fd, NULL) < 0) {
            report_error("epoll_ctl(DEL)");
        }
    }
    if (close(endpoint->fd) < 0) report_error("close(endpoint)");
    endpoint->fd = -1;
}

static void remove_connection(struct server *server, struct connection *connection) {
    struct connection **cursor = &server->connections;
    while (*cursor != NULL && *cursor != connection) cursor = &(*cursor)->next;
    if (*cursor == connection) *cursor = connection->next;
    release_endpoint(server, &connection->client);
    release_endpoint(server, &connection->backend);
    if (server->active > 0) --server->active;
    printf("Closed connection: client-to-backend=%" PRIu64 " bytes, "
           "backend-to-client=%" PRIu64 " bytes; active=%zu\n",
           connection->to_backend.forwarded, connection->to_client.forwarded,
           server->active);
    fflush(stdout);
    free(connection);
}

static bool finish_direction(struct direction *direction) {
    if (!direction->source_eof || direction->pending != 0 ||
        direction->destination_shutdown) return true;
    if (shutdown(direction->destination, SHUT_WR) < 0) {
        report_error("shutdown(SHUT_WR)");
        return false;
    }
    direction->destination_shutdown = true;
    return true;
}

static bool flush_direction(struct direction *direction) {
    while (direction->pending > 0) {
        ssize_t written = send(direction->destination,
                               direction->buffer + direction->offset,
                               direction->pending, MSG_NOSIGNAL);
        if (written > 0) {
            direction->offset += (size_t)written;
            direction->pending -= (size_t)written;
            direction->forwarded += (uint64_t)written;
        } else if (written < 0 && errno == EINTR) {
            continue;
        } else if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return true;
        } else {
            if (written == 0) fputs("send() made no progress\n", stderr);
            else report_error("send()");
            return false;
        }
    }
    direction->offset = 0;
    return finish_direction(direction);
}

static bool read_direction(struct direction *direction) {
    if (direction->source_eof || direction->pending != 0) return true;
    for (;;) {
        ssize_t got = recv(direction->source, direction->buffer,
                           sizeof(direction->buffer), 0);
        if (got > 0) {
            direction->offset = 0;
            direction->pending = (size_t)got;
            return flush_direction(direction);
        }
        if (got == 0) {
            direction->source_eof = true;
            return finish_direction(direction);
        }
        if (errno == EINTR) continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) return true;
        report_error("recv()");
        return false;
    }
}

static bool update_interests(struct server *server, struct connection *connection) {
    uint32_t client_mask = 0, backend_mask = 0;
    if (!connection->to_backend.source_eof && connection->to_backend.pending == 0)
        client_mask |= EPOLLIN;
    if (!connection->to_client.source_eof && connection->to_client.pending == 0)
        backend_mask |= EPOLLIN;
    if (connection->to_client.pending > 0) client_mask |= EPOLLOUT;
    if (connection->to_backend.pending > 0) backend_mask |= EPOLLOUT;
    return change_interest(server, &connection->client, client_mask) &&
           change_interest(server, &connection->backend, backend_mask);
}

static bool attach_connection(struct server *server, int client) {
    struct connection *connection = calloc(1, sizeof(*connection));
    if (!connection) {
        report_error("calloc(connection)");
        close(client);
        return false;
    }
    connection->client.fd = client;
    connection->backend.fd = -1;
    connection->client.is_client = true;
    connection->client.owner = connection;
    connection->backend.owner = connection;
    if (!set_nonblocking(client)) goto fail;
    connection->backend.fd = socket(AF_INET, SOCK_STREAM, 0);
    if (connection->backend.fd < 0) {
        report_error("socket(backend)");
        goto fail;
    }
    /* Still blocking until Phase 7's EINPROGRESS/SO_ERROR implementation. */
    if (connect(connection->backend.fd, (const struct sockaddr *)&server->backend_address,
                sizeof(server->backend_address)) < 0) {
        report_error("connect(backend)");
        goto fail;
    }
    if (!set_nonblocking(connection->backend.fd)) goto fail;
    connection->to_backend.source = client;
    connection->to_backend.destination = connection->backend.fd;
    connection->to_client.source = connection->backend.fd;
    connection->to_client.destination = client;
    if (!register_endpoint(server, &connection->client, EPOLLIN) ||
        !register_endpoint(server, &connection->backend, EPOLLIN)) goto fail;
    connection->next = server->connections;
    server->connections = connection;
    ++server->active;
    printf("Connected to backend; active=%zu\n", server->active);
    fflush(stdout);
    return true;
fail:
    release_endpoint(server, &connection->client);
    release_endpoint(server, &connection->backend);
    free(connection);
    return false;
}

static bool accept_ready(struct server *server) {
    for (;;) {
        struct sockaddr_in remote = {0};
        socklen_t length = sizeof(remote);
        int client = accept(server->listener, (struct sockaddr *)&remote, &length);
        if (client < 0 && errno == EINTR) continue;
        if (client < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return true;
        if (client < 0) {
            report_error("accept()");
            return false;
        }
        /* A backend refusal is isolated to this client; the listener remains alive. */
        (void)attach_connection(server, client);
    }
}

static void dispatch_client_event(struct server *server, uint64_t token,
                                  uint32_t flags) {
    int fd = (int)(uint32_t)token;
    if (fd < 0 || (size_t)fd >= server->fd_capacity) return;
    struct endpoint *endpoint = server->by_fd[fd];
    if (endpoint == NULL || endpoint->token != token) return; /* stale event */
    struct connection *connection = endpoint->owner;
    struct direction *incoming = endpoint->is_client ?
                                 &connection->to_backend : &connection->to_client;
    struct direction *outgoing = endpoint->is_client ?
                                 &connection->to_client : &connection->to_backend;
    bool ok = true;
    if (flags & EPOLLOUT) ok = flush_direction(outgoing);
    if (ok && (flags & (EPOLLIN | EPOLLHUP | EPOLLERR | EPOLLRDHUP)))
        ok = read_direction(incoming);
    if (ok && incoming->destination_shutdown && outgoing->destination_shutdown) {
        remove_connection(server, connection);
        return;
    }
    if (!ok || !update_interests(server, connection))
        remove_connection(server, connection);
}

static void cleanup_server(struct server *server) {
    while (server->connections != NULL)
        remove_connection(server, server->connections);
    if (server->listener >= 0) close(server->listener);
    if (server->epoll_fd >= 0) close(server->epoll_fd);
    free(server->by_fd);
}

int main(int argc, char **argv) {
    uint16_t listen_port, backend_port;
    struct sockaddr_in backend = {0};
    if (argc != 4 || !parse_port(argv[1], true, &listen_port) ||
        !parse_port(argv[3], false, &backend_port) ||
        inet_pton(AF_INET, argv[2], &backend.sin_addr) != 1) {
        fprintf(stderr, "Usage: %s <listen-port: 0-65535> <backend-IPv4> "
                        "<backend-port: 1-65535>\n", argv[0]);
        return EXIT_FAILURE;
    }
    backend.sin_family = AF_INET;
    backend.sin_port = htons(backend_port);
    struct server server = {.listener = -1, .epoll_fd = -1, .backend_address = backend};
    int result = EXIT_FAILURE;
    server.listener = socket(AF_INET, SOCK_STREAM, 0);
    if (server.listener < 0) { report_error("socket(listener)"); goto done; }
    const int reuse = 1;
    if (setsockopt(server.listener, SOL_SOCKET, SO_REUSEADDR, &reuse,
                   sizeof(reuse)) < 0) {
        report_error("setsockopt(SO_REUSEADDR)"); goto done;
    }
    if (!set_nonblocking(server.listener)) goto done;
    struct sockaddr_in local = {0};
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    local.sin_port = htons(listen_port);
    if (bind(server.listener, (const struct sockaddr *)&local, sizeof(local)) < 0) {
        report_error("bind()"); goto done;
    }
    if (listen(server.listener, LISTEN_BACKLOG) < 0) { report_error("listen()"); goto done; }
    socklen_t size = sizeof(local);
    if (getsockname(server.listener, (struct sockaddr *)&local, &size) < 0) {
        report_error("getsockname()"); goto done;
    }
    server.epoll_fd = epoll_create1(EPOLL_CLOEXEC);
    if (server.epoll_fd < 0) { report_error("epoll_create1()"); goto done; }
    struct epoll_event listener_event = {.events = EPOLLIN, .data.u64 = 0};
    if (epoll_ctl(server.epoll_fd, EPOLL_CTL_ADD, server.listener,
                  &listener_event) < 0) { report_error("epoll_ctl(listener)"); goto done; }
    printf("Listening on 127.0.0.1:%u -> %s:%u\n",
           (unsigned)ntohs(local.sin_port), argv[2], (unsigned)backend_port);
    fflush(stdout);

    struct epoll_event events[EVENT_BATCH];
    for (;;) {
        int ready = epoll_wait(server.epoll_fd, events, EVENT_BATCH, -1);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) { report_error("epoll_wait()"); break; }
        for (int i = 0; i < ready; ++i) {
            if (events[i].data.u64 == 0) {
                if ((events[i].events & EPOLLERR) || !accept_ready(&server)) goto done;
            } else {
                dispatch_client_event(&server, events[i].data.u64, events[i].events);
            }
        }
    }

done:
    cleanup_server(&server);
    return result;
}
