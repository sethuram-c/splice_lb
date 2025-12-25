#define _GNU_SOURCE
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
#define COPY_BUFFER_SIZE 65536U
#define PRECONNECT_BUFFER_SIZE 8192U
#define EVENT_BATCH 64

struct connection;
struct endpoint {
    int fd;
    uint32_t interest;
    uint64_t token;
    bool is_client;
    struct connection *owner;
};
enum direction_input_mode { INPUT_COPY, INPUT_SPLICE_TO_PIPE };
struct direction {
    int source;
    int destination;
    enum direction_input_mode input_mode;
    int pipe_read_fd;
    int pipe_write_fd;
    /* Kernel pipe occupancy tracked separately from the user-space queue. */
    size_t pipe_pending;
    uint64_t spliced_to_pipe;
    unsigned char buffer[COPY_BUFFER_SIZE];
    size_t offset;
    size_t pending;
    uint64_t forwarded;
    bool source_eof;
    bool destination_shutdown;
};
enum backend_state { BACKEND_CONNECTING, BACKEND_READY };

struct connection {
    enum backend_state backend_state;
    size_t preconnect_buffered; /* client bytes staged before SO_ERROR success */
    struct connection *next;
    struct endpoint client;
    struct endpoint backend;
    /* Each direction owns a pipe, but only client -> pipe is active in Phase 11. */
    int pipe_to_backend[2];
    int pipe_to_client[2];
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

/* Pipes remain outside epoll: splice socket readiness is watched on the
 * source fd, while destination EPOLLOUT drains the existing byte queue. */
static bool create_connection_pipes(struct connection *connection) {
    if (pipe2(connection->pipe_to_backend, O_NONBLOCK | O_CLOEXEC) < 0) {
        report_error("pipe2(client-to-backend)");
        return false;
    }
    if (pipe2(connection->pipe_to_client, O_NONBLOCK | O_CLOEXEC) < 0) {
        report_error("pipe2(backend-to-client)");
        return false;
    }
    return true;
}

static void close_connection_pipes(struct connection *connection) {
    int *const pipe_ends[] = {
        &connection->pipe_to_backend[0], &connection->pipe_to_backend[1],
        &connection->pipe_to_client[0], &connection->pipe_to_client[1]
    };
    for (size_t i = 0; i < sizeof(pipe_ends) / sizeof(pipe_ends[0]); ++i) {
        if (*pipe_ends[i] >= 0) {
            if (close(*pipe_ends[i]) < 0) report_error("close(pipe)");
            *pipe_ends[i] = -1;
        }
    }
}

static void remove_connection(struct server *server, struct connection *connection) {
    struct connection **cursor = &server->connections;
    while (*cursor != NULL && *cursor != connection) cursor = &(*cursor)->next;
    if (*cursor == connection) *cursor = connection->next;
    release_endpoint(server, &connection->client);
    release_endpoint(server, &connection->backend);
    close_connection_pipes(connection);
    if (server->active > 0) --server->active;
    printf("Closed connection: client-to-backend=%" PRIu64 " bytes, "
           "backend-to-client=%" PRIu64 " bytes; active=%zu; "
           "client-to-pipe=%" PRIu64 " bytes\n",
           connection->to_backend.forwarded, connection->to_client.forwarded,
           server->active, connection->to_backend.spliced_to_pipe);
    fflush(stdout);
    free(connection);
}

static bool finish_direction(struct direction *direction) {
    if (!direction->source_eof || direction->pending != 0 ||
        direction->pipe_pending != 0 || direction->destination_shutdown) return true;
    if (shutdown(direction->destination, SHUT_WR) < 0) {
        report_error("shutdown(SHUT_WR)");
        return false;
    }
    direction->destination_shutdown = true;
    return true;
}

/* A direction owns a bounded, linear byte queue. [offset, offset+pending)
 * contains unsent bytes. We compact only when the tail has no space; this
 * allows readable sockets to fill a queue even if their peer is slow. */
static size_t available_tail(struct direction *direction) {
    if (direction->pending == 0) direction->offset = 0;
    size_t tail = direction->offset + direction->pending;
    if (tail == sizeof(direction->buffer) && direction->offset != 0) {
        memmove(direction->buffer, direction->buffer + direction->offset,
                direction->pending);
        direction->offset = 0;
        tail = direction->pending;
    }
    return sizeof(direction->buffer) - tail;
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

/* Drain readable input into the bounded queue, not just a single 8 KiB
 * chunk. Write attempts are nonblocking; once the 64 KiB queue fills,
 * update_interests() removes source EPOLLIN until it drains. */
static bool read_direction(struct direction *direction) {
    if (direction->source_eof) return true;
    for (;;) {
        size_t tail_free = available_tail(direction);
        if (tail_free == 0) return true;
        ssize_t got = recv(direction->source,
                           direction->buffer + direction->offset + direction->pending,
                           tail_free, 0);
        if (got > 0) {
            direction->pending += (size_t)got;
            if (!flush_direction(direction)) return false;
            continue;
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

/* Phase 11's client -> pipe splice path.  The pipe is drained immediately
 * with read() into the Phase 9 bounded output queue; the final destination
 * still uses send(), so Phase 12 can replace the pipe drain independently.
 * Never claim this hybrid flow is end-to-end zero-copy. */
static bool drain_input_pipe(struct direction *direction) {
    while (direction->pipe_pending > 0) {
        size_t free_tail = available_tail(direction);
        if (free_tail == 0) return true;
        size_t count = direction->pipe_pending < free_tail ?
                       direction->pipe_pending : free_tail;
        ssize_t got = read(direction->pipe_read_fd,
                           direction->buffer + direction->offset + direction->pending,
                           count);
        if (got > 0) {
            direction->pipe_pending -= (size_t)got;
            direction->pending += (size_t)got;
            continue;
        }
        if (got < 0 && errno == EINTR) continue;
        if (got < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return true;
        if (got == 0) fputs("read(pipe) unexpectedly returned EOF\n", stderr);
        else report_error("read(pipe)");
        return false;
    }
    return true;
}

static bool read_spliced_client(struct direction *direction) {
    if (direction->source_eof) return true;
    for (;;) {
        if (!drain_input_pipe(direction)) return false;
        if (direction->pipe_pending > 0) return true;
        size_t free_tail = available_tail(direction);
        if (free_tail == 0) return true;
        ssize_t got = splice(direction->source, NULL, direction->pipe_write_fd,
                             NULL, free_tail, SPLICE_F_MOVE | SPLICE_F_NONBLOCK);
        if (got > 0) {
            direction->pipe_pending += (size_t)got;
            direction->spliced_to_pipe += (uint64_t)got;
            if (!drain_input_pipe(direction) || !flush_direction(direction))
                return false;
            continue;
        }
        if (got == 0) {
            direction->source_eof = true;
            return finish_direction(direction);
        }
        if (errno == EINTR) continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) return true;
        report_error("splice(client -> pipe)");
        return false;
    }
}

/* Staging must never send to a socket whose connect() is unfinished.
 * A single read (at most 8 KiB) bounds preconnect memory per connection.
 * With pending > 0, update_interests() pauses client EPOLLIN until READY. */
static bool stage_client_data(struct connection *connection) {
    struct direction *direction = &connection->to_backend;
    if (direction->pending > 0 || direction->source_eof) return true;
    for (;;) {
        ssize_t got = recv(direction->source, direction->buffer,
                           PRECONNECT_BUFFER_SIZE, 0);
        if (got > 0) {
            direction->offset = 0;
            direction->pending = (size_t)got;
            connection->preconnect_buffered += (size_t)got;
            return true;
        }
        if (got == 0) {
            /* Do not propagate FIN yet: backend connect() may still be pending. */
            direction->source_eof = true;
            return true;
        }
        if (errno == EINTR) continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) return true;
        report_error("recv(preconnect)");
        return false;
    }
}

static bool update_interests(struct server *server, struct connection *connection) {
    if (connection->backend_state == BACKEND_CONNECTING) {
        /* EPOLLIN only while the staging buffer is empty: bounded backpressure.
         * Keep EPOLLOUT on backend until SO_ERROR confirms completion. */
        uint32_t early_read = (!connection->to_backend.source_eof &&
                               connection->to_backend.pending == 0) ? EPOLLIN : 0;
        return change_interest(server, &connection->client, early_read) &&
               change_interest(server, &connection->backend, EPOLLOUT);
    }
    uint32_t client_mask = 0, backend_mask = 0;
    if (!connection->to_backend.source_eof &&
        connection->to_backend.pipe_pending == 0 &&
        connection->to_backend.pending < sizeof(connection->to_backend.buffer))
        client_mask |= EPOLLIN;
    if (!connection->to_client.source_eof &&
        connection->to_client.pending < sizeof(connection->to_client.buffer))
        backend_mask |= EPOLLIN;
    /* Writable level-triggered sockets are nearly always ready: subscribing
     * without pending bytes would spin the event loop at idle. */
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
    connection->pipe_to_backend[0] = -1;
    connection->pipe_to_backend[1] = -1;
    connection->pipe_to_client[0] = -1;
    connection->pipe_to_client[1] = -1;
    connection->client.is_client = true;
    connection->client.owner = connection;
    connection->backend.owner = connection;
    if (!set_nonblocking(client)) goto fail;
    connection->backend.fd = socket(AF_INET, SOCK_STREAM, 0);
    if (connection->backend.fd < 0) {
        report_error("socket(backend)");
        goto fail;
    }
    /* O_NONBLOCK must be set *before* connect(), otherwise a slow backend
     * can stall the whole epoll event loop. */
    if (!set_nonblocking(connection->backend.fd)) goto fail;
    int connect_result = connect(connection->backend.fd,
                                 (const struct sockaddr *)&server->backend_address,
                                 sizeof(server->backend_address));
    if (connect_result == 0) {
        connection->backend_state = BACKEND_READY;
    } else if (errno == EINPROGRESS) {
        connection->backend_state = BACKEND_CONNECTING;
    } else {
        report_error("connect(backend)");
        goto fail;
    }
    connection->to_backend.source = client;
    connection->to_backend.destination = connection->backend.fd;
    connection->to_backend.input_mode = INPUT_SPLICE_TO_PIPE;
    connection->to_client.source = connection->backend.fd;
    connection->to_client.destination = client;
    /* Both pipe pairs must exist before the connection is visible in epoll.
     * Any error takes the same cleanup path (including partially made pipes). */
    if (!create_connection_pipes(connection)) goto fail;
    connection->to_backend.pipe_read_fd = connection->pipe_to_backend[0];
    connection->to_backend.pipe_write_fd = connection->pipe_to_backend[1];
    uint32_t client_events = EPOLLIN; /* stage early bytes while CONNECTING */
    uint32_t backend_events = connection->backend_state == BACKEND_READY ? EPOLLIN : EPOLLOUT;
    if (!register_endpoint(server, &connection->client, client_events) ||
        !register_endpoint(server, &connection->backend, backend_events)) goto fail;
    connection->next = server->connections;
    server->connections = connection;
    ++server->active;
    if (connection->backend_state == BACKEND_READY)
        printf("Connected to backend; active=%zu\n", server->active);
    else
        printf("Backend connection pending; active=%zu\n", server->active);
    fflush(stdout);
    return true;
fail:
    release_endpoint(server, &connection->client);
    release_endpoint(server, &connection->backend);
    close_connection_pipes(connection);
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

/* A writable nonblocking TCP socket is not proof of a successful connect:
 * it can also be writable because the handshake failed. Read SO_ERROR. */
static bool complete_backend_connect(struct server *server,
                                     struct connection *connection) {
    int socket_error = 0;
    socklen_t length = sizeof(socket_error);
    if (getsockopt(connection->backend.fd, SOL_SOCKET, SO_ERROR,
                   &socket_error, &length) < 0) {
        report_error("getsockopt(SO_ERROR)");
        return false;
    }
    if (socket_error != 0) {
        errno = socket_error;
        report_error("connect(backend)");
        return false;
    }
    connection->backend_state = BACKEND_READY;
    printf("Connected to backend; preconnect-buffered=%zu bytes; active=%zu\n",
           connection->preconnect_buffered, server->active);
    fflush(stdout);
    /* Flush staged bytes immediately, including a queued client FIN; on EAGAIN
     * the existing EPOLLOUT interest takes over without blocking the loop. */
    return flush_direction(&connection->to_backend) &&
           update_interests(server, connection);
}

static void dispatch_client_event(struct server *server, uint64_t token,
                                  uint32_t flags) {
    int fd = (int)(uint32_t)token;
    if (fd < 0 || (size_t)fd >= server->fd_capacity) return;
    struct endpoint *endpoint = server->by_fd[fd];
    if (endpoint == NULL || endpoint->token != token) return; /* stale event */
    struct connection *connection = endpoint->owner;
    if (connection->backend_state == BACKEND_CONNECTING) {
        if (endpoint->is_client) {
            bool ok = true;
            if (flags & (EPOLLIN | EPOLLHUP | EPOLLERR | EPOLLRDHUP))
                ok = stage_client_data(connection);
            if (!ok || !update_interests(server, connection))
                remove_connection(server, connection);
            return;
        }
        if (!(flags & (EPOLLOUT | EPOLLERR | EPOLLHUP))) return;
        if (!complete_backend_connect(server, connection)) {
            remove_connection(server, connection);
            return;
        }
        /* Existing backend event may also carry normal readiness flags. */
    }
    struct direction *incoming = endpoint->is_client ?
                                 &connection->to_backend : &connection->to_client;
    struct direction *outgoing = endpoint->is_client ?
                                 &connection->to_client : &connection->to_backend;
    bool ok = true;
    if (flags & EPOLLOUT) ok = flush_direction(outgoing);
    if (ok && (flags & (EPOLLIN | EPOLLHUP | EPOLLERR | EPOLLRDHUP))) {
        ok = incoming->input_mode == INPUT_SPLICE_TO_PIPE ?
             read_spliced_client(incoming) : read_direction(incoming);
    }
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
