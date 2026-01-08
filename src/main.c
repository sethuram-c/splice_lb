#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <unistd.h>

#define LISTEN_BACKLOG 128
#define COPY_BUFFER_SIZE 65536U
#define PRECONNECT_BUFFER_SIZE 8192U
#define EVENT_BATCH 64
#define MAX_BACKENDS 32U
/* A refused connect removes the backend from new selections for three seconds.
 * Next request after expiry is a single natural traffic probe, not a timer. */
#define BACKEND_RETRY_DELAY_MS 3000ULL

struct backend_target {
    struct sockaddr_in address;
    char label[INET_ADDRSTRLEN + 8];
    uint64_t selections;
    size_t active_connections; /* CONNECTING and READY sessions, until teardown */
    uint64_t retry_after_ms; /* CLOCK_MONOTONIC; zero means selectable */
    uint64_t connect_failures;
    bool probe_in_flight; /* at most one in-flight re-probe per failed target */
};

struct connection;
struct endpoint {
    int fd;
    uint32_t interest;
    uint64_t token;
    bool is_client;
    struct connection *owner;
};
enum direction_input_mode { INPUT_SPLICE_BIDIRECTIONAL };
struct direction {
    int source;
    int destination;
    enum direction_input_mode input_mode;
    int pipe_read_fd;
    int pipe_write_fd;
    /* Kernel pipe occupancy tracked separately from the user-space queue. */
    size_t pipe_pending;
    uint64_t spliced_to_pipe;
    uint64_t spliced_from_pipe;
    /* A successful splice may move less than requested. If EAGAIN follows
     * while bytes remain in the pipe, the destination must drain first. */
    uint64_t partial_pipe_writes;
    uint64_t pipe_write_eagain;
    /* Phase 14: destination-side output readiness is independent of pipe fill.
     * Pending pipe bytes survive EAGAIN until destination EPOLLOUT fires. */
    bool drain_blocked;
    /* Phase 20: one bounded queue per direction (socket reads are disabled
     * whenever either pending queue is nonempty). These counters make the
     * two independent pressure states observable in an integration test. */
    bool source_paused;
    uint64_t backpressure_pauses;
    uint64_t backpressure_resumes;
    size_t peak_queued_bytes;
    uint64_t partial_pipe_drains;
    uint64_t pipe_drain_eagain;
    unsigned char buffer[COPY_BUFFER_SIZE];
    size_t offset;
    size_t pending;
    uint64_t forwarded;
    /* Bytes delivered through send() from the preconnect staging queue.
     * Together with spliced_from_pipe, this must equal forwarded. */
    uint64_t staged_sent;
    bool source_eof;
    bool destination_shutdown;
};
enum backend_state { BACKEND_CONNECTING, BACKEND_READY };
/* Phase 21: a client FIN is not permission to discard already-read data.
 * EOF_OBSERVED means splice()/recv() returned zero, while FIN_FORWARDED
 * means the backend write side has actually been shut down after draining. */
enum client_fin_state {
    CLIENT_FIN_OPEN, CLIENT_FIN_EOF_OBSERVED, CLIENT_FIN_FORWARDED
};
/* Phase 22: the backend's send half closes independently of its receive half.
 * Neither receiving FIN nor forwarding it permits discarding an active client
 * upload. Completion requires BOTH streams to be drained and half-closed. */
enum backend_fin_state {
    BACKEND_FIN_OPEN, BACKEND_FIN_EOF_OBSERVED, BACKEND_FIN_FORWARDED
};

/* Phase 24: ownership states. A candidate is private during setup, becomes
 * ACTIVE only after both epoll registrations succeed, and transitions to
 * CLOSING before endpoints and memory are released. epoll callbacks only
 * reference ACTIVE connections and validate their generation-bearing token.
 */
enum connection_lifecycle {
    CONN_INITIALIZING, CONN_ACTIVE, CONN_CLOSING
};

struct connection {
    enum connection_lifecycle lifecycle;
    enum backend_state backend_state;
    size_t preconnect_buffered; /* client bytes staged before SO_ERROR success */
    enum client_fin_state client_fin_state;
    bool client_rdhup_hint; /* EPOLLRDHUP is a hint; not proof input was drained */
    bool client_fin_was_deferred; /* EOF arrived with a queue or connecting backend */
    enum backend_fin_state backend_fin_state;
    bool backend_rdhup_hint;
    bool backend_fin_was_deferred; /* pending backend response on EOF */
    size_t backend_index; /* stable for this connection until cleanup */
    struct connection *next;
    struct endpoint client;
    struct endpoint backend;
    /* Each direction owns one nonblocking pipe for socket -> pipe -> socket. */
    int pipe_to_backend[2];
    int pipe_to_client[2];
    struct direction to_backend;
    struct direction to_client;
};
/* Phase 23: all exits use the same teardown; the reason distinguishes a
 * graceful two-FIN completion from an abrupt peer failure. */
enum close_reason {
    CLOSE_GRACEFUL,
    CLOSE_CLIENT_ABORT,
    CLOSE_BACKEND_ABORT,
    CLOSE_BACKEND_CONNECT_FAILURE,
    CLOSE_INTERNAL_ERROR,
    CLOSE_SERVER_SHUTDOWN
};

static const char *close_reason_name(enum close_reason reason) {
    switch (reason) {
    case CLOSE_GRACEFUL: return "graceful";
    case CLOSE_CLIENT_ABORT: return "client-abort";
    case CLOSE_BACKEND_ABORT: return "backend-abort";
    case CLOSE_BACKEND_CONNECT_FAILURE: return "backend-connect-failure";
    case CLOSE_INTERNAL_ERROR: return "internal-error";
    case CLOSE_SERVER_SHUTDOWN: return "server-shutdown";
    }
    return "unknown";
}

enum balance_algorithm { BALANCE_ROUND_ROBIN, BALANCE_LEAST_CONNECTIONS };

struct server {
    enum balance_algorithm algorithm;
    int listener;
    int epoll_fd;
    struct backend_target backends[MAX_BACKENDS];
    size_t backend_count;
    size_t next_backend; /* next index for connection-level round robin */
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

/* Explicit backend spec is IPv4:port (no DNS or IPv6 in Phase 16).
 * Keep the original three-argument CLI form for older regression tests. */
static bool parse_backend_spec(const char *text, struct backend_target *target) {
    const char *colon = strchr(text, ':');
    if (colon == NULL || strchr(colon + 1, ':') != NULL) return false;
    size_t ip_length = (size_t)(colon - text);
    if (ip_length == 0 || ip_length >= INET_ADDRSTRLEN) return false;
    char ip[INET_ADDRSTRLEN];
    memcpy(ip, text, ip_length);
    ip[ip_length] = '\0';
    uint16_t port;
    if (!parse_port(colon + 1, false, &port) ||
        inet_pton(AF_INET, ip, &target->address.sin_addr) != 1) return false;
    target->address.sin_family = AF_INET;
    target->address.sin_port = htons(port);
    (void)snprintf(target->label, sizeof(target->label), "%s:%u",
                   ip, (unsigned)port);
    target->selections = 0;
    target->active_connections = 0;
    target->retry_after_ms = 0;
    target->connect_failures = 0;
    target->probe_in_flight = false;
    return true;
}

static void report_error(const char *call);

/* All health deadlines use monotonic time, never wall-clock timestamps.
 * If the clock itself is unavailable, refuse new selections rather than
 * bypass the cooldown and hammer a failed backend. */
static bool monotonic_ms(uint64_t *now) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0) return false;
    *now = (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)ts.tv_nsec / 1000000ULL;
    return true;
}

/* A failed TCP handshake is connection-local, but it also temporarily
 * quarantines the target for NEW accepts. Established sessions are untouched.
 * We never replay an in-flight byte stream onto another backend: blindly
 * retrying can duplicate non-idempotent requests or reorder stream data. */
static void mark_backend_connect_failed(struct server *server, size_t index,
                                        int error) {
    struct backend_target *target = &server->backends[index];
    uint64_t now;
    ++target->connect_failures;
    target->probe_in_flight = false;
    if (monotonic_ms(&now))
        target->retry_after_ms = now + BACKEND_RETRY_DELAY_MS;
    else
        target->retry_after_ms = UINT64_MAX;
    fprintf(stderr, "Backend[%zu]=%s connect failure: %s; "
            "cooldown-ms=%llu; failures=%" PRIu64 "\n",
            index, target->label, strerror(error),
            (unsigned long long)BACKEND_RETRY_DELAY_MS,
            target->connect_failures);
}

/* Skip cooling-down backends. Choose a healthy target per accepted client;
 * among healthy targets keep Round Robin or least-active selection semantics.
 * The rotating cursor advances only on a successful selection. */
static bool select_backend(struct server *server, size_t *selected) {
    uint64_t now;
    if (!monotonic_ms(&now)) {
        report_error("clock_gettime(CLOCK_MONOTONIC)");
        return false;
    }
    bool found = false;
    size_t chosen = 0;
    size_t minimum = SIZE_MAX;
    for (size_t step = 0; step < server->backend_count; ++step) {
        size_t candidate = (server->next_backend + step) % server->backend_count;
        struct backend_target *target = &server->backends[candidate];
        if (target->retry_after_ms > now || target->probe_in_flight) continue;
        if (server->algorithm == BALANCE_ROUND_ROBIN) {
            chosen = candidate;
            found = true;
            break;
        }
        if (!found || target->active_connections < minimum) {
            chosen = candidate;
            minimum = target->active_connections;
            found = true;
        }
    }
    if (!found) return false;
    /* An expired quarantine gets one natural connect probe, not a flood.
     * If it fails again, the new failure immediately extends the deadline. */
    server->next_backend = (chosen + 1U) % server->backend_count;
    struct backend_target *target = &server->backends[chosen];
    ++target->selections;
    if (target->retry_after_ms != 0) target->probe_in_flight = true;
    *selected = chosen;
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

/* Pipes are not watched directly: source EPOLLIN fills a bounded pipe and
 * destination EPOLLOUT resumes a blocked pipe-to-socket splice. */
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

/* Phase 15: a byte-counting invariant is not a content hash. It catches
 * lost/double-counted bytes inside our two-hop forwarding state machine;
 * byte-for-byte payload tests provide independent content verification. */
static size_t direction_queued_bytes(const struct direction *direction) {
    return direction->pending + direction->pipe_pending;
}

/* Only 8 KiB can be staged during connect, and staging fully drains before
 * a socket->pipe splice begins. The direction never owns more than 64 KiB
 * of outstanding application/kernel-pipe bytes at a time. */
static void record_queued_peak(struct direction *direction) {
    const size_t queued = direction_queued_bytes(direction);
    if (queued > direction->peak_queued_bytes)
        direction->peak_queued_bytes = queued;
}

static bool account_backpressure(struct direction *direction) {
    const size_t queued = direction_queued_bytes(direction);
    if (queued > COPY_BUFFER_SIZE) {
        fprintf(stderr, "backpressure queue exceeded limit: %zu > %u\n",
                queued, COPY_BUFFER_SIZE);
        return false;
    }
    record_queued_peak(direction);
    const bool pause = queued != 0 || direction->drain_blocked;
    if (pause != direction->source_paused) {
        if (pause) ++direction->backpressure_pauses;
        else ++direction->backpressure_resumes;
        direction->source_paused = pause;
    }
    return true;
}

static bool direction_accounting_valid(const struct direction *direction) {
    return direction_queued_bytes(direction) <= COPY_BUFFER_SIZE &&
           direction->spliced_from_pipe <= direction->spliced_to_pipe &&
           direction->spliced_to_pipe - direction->spliced_from_pipe ==
               (uint64_t)direction->pipe_pending &&
           direction->forwarded ==
               direction->staged_sent + direction->spliced_from_pipe;
}

static bool fully_forwarded(const struct connection *connection) {
    const struct direction *up = &connection->to_backend;
    const struct direction *down = &connection->to_client;
    return connection->backend_state == BACKEND_READY &&
           connection->client_fin_state == CLIENT_FIN_FORWARDED &&
           up->source_eof && down->source_eof &&
           up->destination_shutdown && down->destination_shutdown &&
           up->pending == 0 && down->pending == 0 &&
           up->pipe_pending == 0 && down->pipe_pending == 0 &&
           up->staged_sent == connection->preconnect_buffered &&
           down->staged_sent == 0 &&
           direction_accounting_valid(up) && direction_accounting_valid(down);
}

/* Safe for completely initialized, partially registered and already-closed
 * endpoints. Each fd gets removed from epoll, cleared from by_fd, and closed
 * at most once. Keeping this routine shared avoids diverging error paths. */
static void release_connection_resources(struct server *server,
                                         struct connection *connection) {
    release_endpoint(server, &connection->client);
    release_endpoint(server, &connection->backend);
    close_connection_pipes(connection);
}

static void remove_connection(struct server *server, struct connection *connection,
                              enum close_reason reason) {
    if (connection->lifecycle != CONN_ACTIVE) {
        fputs("invalid connection lifecycle during teardown\n", stderr);
        abort();
    }
    /* No event may dispatch to this connection once unregistration starts.
     * The dispatch loop checks both token and lifecycle before dereferencing
     * connection state; the object is freed only after all endpoint slots
     * have been cleared. */
    connection->lifecycle = CONN_CLOSING;
    /* If the client vanishes before the health re-probe completes, allow a
     * later connection to try again; this attempt proves neither health nor
     * a backend fault. The original cooldown deadline remains unchanged. */
    if (connection->backend_state == BACKEND_CONNECTING)
        server->backends[connection->backend_index].probe_in_flight = false;
    struct connection **cursor = &server->connections;
    while (*cursor != NULL && *cursor != connection) cursor = &(*cursor)->next;
    if (*cursor == connection) *cursor = connection->next;
    release_connection_resources(server, connection);
    /* The connection is added to the list only after all setup succeeds.
     * Every listed connection owns exactly one active backend slot. */
    struct backend_target *target = &server->backends[connection->backend_index];
    if (target->active_connections == 0 || server->active == 0) {
        fputs("active connection accounting underflow\n", stderr);
        abort();
    }
    --target->active_connections;
    --server->active;
    printf("Closed connection: client-to-backend=%" PRIu64 " bytes, "
           "backend-to-client=%" PRIu64 " bytes; active=%zu; "
           "client-to-pipe=%" PRIu64 " bytes; backend-to-pipe=%" PRIu64
           " bytes; pipe-to-backend=%" PRIu64 " bytes; pipe-to-client=%" PRIu64
           " bytes; partial-pipe-writes=%" PRIu64 "/%" PRIu64
           "; pipe-write-EAGAIN=%" PRIu64 "/%" PRIu64
           "; partial-pipe-drains=%" PRIu64 "/%" PRIu64
           "; pipe-drain-EAGAIN=%" PRIu64 "/%" PRIu64 "\n",
           connection->to_backend.forwarded, connection->to_client.forwarded,
           server->active, connection->to_backend.spliced_to_pipe,
           connection->to_client.spliced_to_pipe,
           connection->to_backend.spliced_from_pipe,
           connection->to_client.spliced_from_pipe,
           connection->to_backend.partial_pipe_writes,
           connection->to_client.partial_pipe_writes,
           connection->to_backend.pipe_write_eagain,
           connection->to_client.pipe_write_eagain,
           connection->to_backend.partial_pipe_drains,
           connection->to_client.partial_pipe_drains,
           connection->to_backend.pipe_drain_eagain,
           connection->to_client.pipe_drain_eagain);
    printf("backpressure-pauses=%" PRIu64 "/%" PRIu64
           "; backpressure-resumes=%" PRIu64 "/%" PRIu64
           "; backpressure-peak=%zu/%zu bytes; queue-cap=%u bytes\n",
           connection->to_backend.backpressure_pauses,
           connection->to_client.backpressure_pauses,
           connection->to_backend.backpressure_resumes,
           connection->to_client.backpressure_resumes,
           connection->to_backend.peak_queued_bytes,
           connection->to_client.peak_queued_bytes,
           COPY_BUFFER_SIZE);
    printf("client-eof-final=%s; client-fin-deferred=%s; rdhup-hint=%s\n",
           connection->client_fin_state == CLIENT_FIN_FORWARDED ? "forwarded" :
           connection->client_fin_state == CLIENT_FIN_EOF_OBSERVED ? "observed" : "open",
           connection->client_fin_was_deferred ? "yes" : "no",
           connection->client_rdhup_hint ? "yes" : "no");
    printf("backend-eof-final=%s; backend-fin-deferred=%s; backend-rdhup-hint=%s\n",
           connection->backend_fin_state == BACKEND_FIN_FORWARDED ? "forwarded" :
           connection->backend_fin_state == BACKEND_FIN_EOF_OBSERVED ? "observed" : "open",
           connection->backend_fin_was_deferred ? "yes" : "no",
           connection->backend_rdhup_hint ? "yes" : "no");
    printf("transfer-integrity=%s; staged=%" PRIu64 "; "
           "pipe-pending=%zu/%zu; forwarded=%" PRIu64 "/%" PRIu64 "\n",
           fully_forwarded(connection) ? "verified" : "incomplete",
           connection->to_backend.staged_sent,
           connection->to_backend.pipe_pending,
           connection->to_client.pipe_pending,
           connection->to_backend.forwarded,
           connection->to_client.forwarded);
    printf("connection-close-reason=%s; backend-index=%zu; "
           "queued-to-backend=%zu; queued-to-client=%zu\n",
           close_reason_name(reason), connection->backend_index,
           direction_queued_bytes(&connection->to_backend),
           direction_queued_bytes(&connection->to_client));
    printf("Released backend[%zu]=%s; backend-active=%zu; total-active=%zu\n",
           connection->backend_index, target->label,
           target->active_connections, server->active);
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

static bool flush_direction(struct direction *direction) {
    while (direction->pending > 0) {
        ssize_t written = send(direction->destination,
                               direction->buffer + direction->offset,
                               direction->pending, MSG_NOSIGNAL);
        if (written > 0) {
            direction->offset += (size_t)written;
            direction->pending -= (size_t)written;
            direction->forwarded += (uint64_t)written;
            direction->staged_sent += (uint64_t)written;
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

/* Stage 8 KiB of preconnect client data in the existing application queue.
 * Those bytes MUST drain before later socket-to-pipe splice data: TCP is an
 * ordered stream, and sending the kernel-pipe bytes first would reorder it. */
/* Keep the pipe's bytes intact when the destination socket is full. A
 * nonblocking pipe-to-socket splice() may return EAGAIN even though the pipe
 * has data: this time it is the destination that cannot accept more bytes.
 * In that state, update_interests() must arm the destination's EPOLLOUT and
 * DISARM the source EPOLLIN until the pipe has drained. */
static bool drain_pipe_to_socket(struct direction *direction) {
    while (direction->pipe_pending != 0) {
        size_t requested = direction->pipe_pending;
        ssize_t sent = splice(direction->pipe_read_fd, NULL,
                              direction->destination, NULL,
                              requested, SPLICE_F_MOVE | SPLICE_F_NONBLOCK);
        if (sent > 0) {
            if ((size_t)sent < requested) ++direction->partial_pipe_drains;
            direction->pipe_pending -= (size_t)sent;
            direction->spliced_from_pipe += (uint64_t)sent;
            direction->forwarded += (uint64_t)sent;
            direction->drain_blocked = false;
            continue;
        }
        if (sent == 0) {
            fputs("splice(pipe -> socket) unexpectedly returned zero\n", stderr);
            return false;
        }
        if (errno == EINTR) continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            direction->drain_blocked = true;
            ++direction->pipe_drain_eagain;
            return true;
        }
        report_error("splice(pipe -> socket)");
        return false;
    }
    direction->drain_blocked = false;
    return finish_direction(direction);
}

static bool flush_spliced_direction(struct direction *direction) {
    /* Preconnect recv() bytes must be sent first; while any remain,
     * source EPOLLIN is paused to prevent overtaking by splice(). */
    if (!flush_direction(direction)) return false;
    if (direction->pending != 0) return true;
    return drain_pipe_to_socket(direction);
}

/* Phase 13/14: socket -> pipe is a counted, bounded fill loop. splice() is
 * allowed to move fewer bytes than requested, even when it succeeds. Keep
 * filling until the kernel reports EAGAIN (source empty OR pipe unable to
 * accept more), EOF, or the 64 KiB per-dispatch budget is reached. Only then
 * drain the pipe into the destination. On a blocked destination, keep the
 * exact occupancy and leave EPOLLIN disabled until EPOLLOUT drains it.
 * Phase 14 tracks blocked pipe drains and arms output readiness on demand. */
static bool read_spliced_direction(struct direction *direction) {
    if (direction->source_eof) return true;
    if (!flush_spliced_direction(direction)) return false;
    /* Staged preconnect bytes and pipe data precede later source bytes. */
    if (direction->pending != 0 || direction->pipe_pending != 0) return true;

    while (direction->pipe_pending < COPY_BUFFER_SIZE) {
        const size_t requested = COPY_BUFFER_SIZE - direction->pipe_pending;
        ssize_t got = splice(direction->source, NULL,
                             direction->pipe_write_fd, NULL,
                             requested, SPLICE_F_MOVE | SPLICE_F_NONBLOCK);
        if (got > 0) {
            if ((size_t)got < requested) ++direction->partial_pipe_writes;
            direction->pipe_pending += (size_t)got;
            direction->spliced_to_pipe += (uint64_t)got;
            record_queued_peak(direction);
            continue;
        }
        if (got == 0) {
            direction->source_eof = true;
            break;
        }
        if (errno == EINTR) continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (direction->pipe_pending != 0) ++direction->pipe_write_eagain;
            break;
        }
        report_error("splice(socket -> pipe)");
        return false;
    }
    return drain_pipe_to_socket(direction);
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
            record_queued_peak(direction);
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

/* Level-triggered readiness state machine:
 * - Source EPOLLIN: only when its direction has ZERO pending output.
 * - Destination EPOLLOUT: only when queued data needs a retry (including
 *   pipe data whose last drain returned EAGAIN).
 * - Neither socket is busy-polled: the kernel wakes us on actual readiness.
 * - The CONNECTING backend remains watched for EPOLLOUT until SO_ERROR.
 * See tests/test_pipe_drain.py for the blocked -> resumed -> idle sequence. */
static bool update_interests(struct server *server, struct connection *connection) {
    if (connection->backend_state == BACKEND_CONNECTING) {
        uint32_t early_read = (!connection->to_backend.source_eof &&
                               connection->to_backend.pending == 0) ?
                              (EPOLLIN | EPOLLRDHUP) : 0;
        return change_interest(server, &connection->client, early_read) &&
               change_interest(server, &connection->backend, EPOLLOUT);
    }
    const struct direction *up = &connection->to_backend;
    const struct direction *down = &connection->to_client;
    uint32_t client_mask = 0;
    uint32_t backend_mask = 0;
    if (!account_backpressure(&connection->to_backend) ||
        !account_backpressure(&connection->to_client)) return false;

    /* Source EPOLLIN stays disabled until all of that direction's queued
     * bytes have been drained. Destination EPOLLOUT drives the retry. */
    if (!up->source_eof && !up->source_paused)
        client_mask |= EPOLLIN | EPOLLRDHUP;
    if (!down->source_eof && !down->source_paused)
        backend_mask |= EPOLLIN | EPOLLRDHUP;
    if (down->pending > 0 || down->pipe_pending > 0 || down->drain_blocked)
        client_mask |= EPOLLOUT;
    if (up->pending > 0 || up->pipe_pending > 0 || up->drain_blocked)
        backend_mask |= EPOLLOUT;

    return change_interest(server, &connection->client, client_mask) &&
           change_interest(server, &connection->backend, backend_mask);
}

static bool attach_connection(struct server *server, int client) {
    bool backend_selected = false;
    const char *setup_stage = "allocation";
    struct connection *connection = calloc(1, sizeof(*connection));
    if (!connection) {
        report_error("calloc(connection)");
        close(client);
        return false;
    }
    connection->lifecycle = CONN_INITIALIZING;
    connection->client.fd = client;
    connection->backend.fd = -1;
    connection->pipe_to_backend[0] = -1;
    connection->pipe_to_backend[1] = -1;
    connection->pipe_to_client[0] = -1;
    connection->pipe_to_client[1] = -1;
    connection->client.is_client = true;
    connection->client.owner = connection;
    connection->backend.owner = connection;
    setup_stage = "backend-selection";
    if (!select_backend(server, &connection->backend_index)) {
        fputs("No healthy backends; rejecting client while targets cool down\n", stderr);
        goto fail;
    }
    backend_selected = true;
    const struct backend_target *target = &server->backends[connection->backend_index];
    printf("Selected backend[%zu]=%s; selections=%" PRIu64
           "; backend-active-before=%zu; algorithm=%s\n",
           connection->backend_index, target->label,
           target->selections, target->active_connections,
           server->algorithm == BALANCE_LEAST_CONNECTIONS ? "least-connections" : "round-robin");
    fflush(stdout);
    setup_stage = "client-nonblocking";
    if (!set_nonblocking(client)) goto fail;
    setup_stage = "backend-socket";
    connection->backend.fd = socket(AF_INET, SOCK_STREAM, 0);
    if (connection->backend.fd < 0) {
        report_error("socket(backend)");
        goto fail;
    }
    /* O_NONBLOCK must be set *before* connect(), otherwise a slow backend
     * can stall the whole epoll event loop. */
    setup_stage = "backend-nonblocking";
    if (!set_nonblocking(connection->backend.fd)) goto fail;
    setup_stage = "backend-connect";
    int connect_result = connect(connection->backend.fd,
                                 (const struct sockaddr *)&target->address,
                                 sizeof(target->address));
    if (connect_result == 0) {
        connection->backend_state = BACKEND_READY;
        if (target->probe_in_flight) {
            printf("Backend[%zu]=%s recovered after successful TCP handshake\n",
                   connection->backend_index, target->label);
            server->backends[connection->backend_index].retry_after_ms = 0;
            server->backends[connection->backend_index].probe_in_flight = false;
        }
    } else if (errno == EINPROGRESS) {
        connection->backend_state = BACKEND_CONNECTING;
    } else {
        int connect_error = errno;
        report_error("connect(backend)");
        mark_backend_connect_failed(server, connection->backend_index, connect_error);
        goto fail;
    }
    connection->to_backend.source = client;
    connection->to_backend.destination = connection->backend.fd;
    connection->to_backend.input_mode = INPUT_SPLICE_BIDIRECTIONAL;
    connection->to_client.source = connection->backend.fd;
    connection->to_client.destination = client;
    connection->to_client.input_mode = INPUT_SPLICE_BIDIRECTIONAL;
    /* Both pipe pairs must exist before the connection is visible in epoll.
     * Any error takes the same cleanup path (including partially made pipes). */
    setup_stage = "connection-pipes";
    if (!create_connection_pipes(connection)) goto fail;
    connection->to_backend.pipe_read_fd = connection->pipe_to_backend[0];
    connection->to_backend.pipe_write_fd = connection->pipe_to_backend[1];
    connection->to_client.pipe_read_fd = connection->pipe_to_client[0];
    connection->to_client.pipe_write_fd = connection->pipe_to_client[1];
    uint32_t client_events = EPOLLIN | EPOLLRDHUP; /* early bytes / FIN hint */
    uint32_t backend_events = connection->backend_state == BACKEND_READY ?
                              (EPOLLIN | EPOLLRDHUP) : EPOLLOUT;
    setup_stage = "client-epoll-registration";
    if (!register_endpoint(server, &connection->client, client_events)) goto fail;
    setup_stage = "backend-epoll-registration";
    if (!register_endpoint(server, &connection->backend, backend_events)) goto fail;
    /* Publish only after BOTH endpoint registrations, pipes, and state are
     * ready. No partially constructed connection owns an active slot. */
    connection->lifecycle = CONN_ACTIVE;
    connection->next = server->connections;
    server->connections = connection;
    ++server->active;
    ++server->backends[connection->backend_index].active_connections;
    if (connection->backend_state == BACKEND_READY)
        printf("Connected to backend; active=%zu\n", server->active);
    else
        printf("Backend connection pending; active=%zu\n", server->active);
    fflush(stdout);
    return true;
fail:
    /* A failed candidate never enters the published list and never claims
     * a backend-active slot. Nonetheless it may own 0, 1, or 2 registered
     * epoll endpoints, sockets, and a partially allocated pipe pair. */
    fprintf(stderr, "connection-setup-failed=%s; rolling-back=yes\n", setup_stage);
    connection->lifecycle = CONN_CLOSING;
    if (backend_selected)
        server->backends[connection->backend_index].probe_in_flight = false;
    release_connection_resources(server, connection);
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
        mark_backend_connect_failed(server, connection->backend_index, socket_error);
        return false;
    }
    connection->backend_state = BACKEND_READY;
    struct backend_target *target = &server->backends[connection->backend_index];
    if (target->probe_in_flight) {
        printf("Backend[%zu]=%s recovered after successful TCP handshake\n",
               connection->backend_index, target->label);
        target->retry_after_ms = 0;
        target->probe_in_flight = false;
    }
    printf("Connected to backend; preconnect-buffered=%zu bytes; active=%zu\n",
           connection->preconnect_buffered, server->active);
    fflush(stdout);
    /* Flush staged bytes immediately, including a queued client FIN; on EAGAIN
     * the existing EPOLLOUT interest takes over without blocking the loop. */
    return flush_spliced_direction(&connection->to_backend) &&
           update_interests(server, connection);
}

/* Observe FIN transitions exactly once. EPOLLRDHUP can arrive while data is
 * unread, so only recv()/splice()==0 marks logical source EOF. Likewise,
 * shutdown(SHUT_WR) is only forwarded after BOTH staged bytes and pipe bytes
 * are fully drained. The two states intentionally remain distinct. */
static void track_client_fin(struct connection *connection) {
    const struct direction *up = &connection->to_backend;
    if (up->source_eof && connection->client_fin_state == CLIENT_FIN_OPEN) {
        connection->client_fin_state = CLIENT_FIN_EOF_OBSERVED;
        bool deferred = connection->backend_state == BACKEND_CONNECTING ||
                        direction_queued_bytes(up) != 0;
        connection->client_fin_was_deferred = deferred;
        printf("client-eof-state=observed; deferred=%s; pending=%zu; "
               "pipe-pending=%zu; backend=%s\n",
               deferred ? "yes" : "no", up->pending, up->pipe_pending,
               connection->backend_state == BACKEND_CONNECTING ? "connecting" : "ready");
        fflush(stdout);
    }
    if (up->destination_shutdown &&
        connection->client_fin_state == CLIENT_FIN_EOF_OBSERVED) {
        connection->client_fin_state = CLIENT_FIN_FORWARDED;
        printf("client-eof-state=forwarded; bytes=%" PRIu64
               "; pending=%zu; pipe-pending=%zu\n",
               up->forwarded, up->pending, up->pipe_pending);
        fflush(stdout);
    }
}

/* A backend FIN is only an EOF on the backend->client byte stream.
 * The opposite client->backend upload may still be in progress, even after
 * the backend has finished its response. EPOLLRDHUP is only a hint; the
 * splice() source returning zero is the authoritative EOF observation. */
static void track_backend_fin(struct connection *connection) {
    const struct direction *down = &connection->to_client;
    if (down->source_eof && connection->backend_fin_state == BACKEND_FIN_OPEN) {
        connection->backend_fin_state = BACKEND_FIN_EOF_OBSERVED;
        connection->backend_fin_was_deferred = direction_queued_bytes(down) != 0;
        printf("backend-eof-state=observed; deferred=%s; pending=%zu; "
               "pipe-pending=%zu; client-upload-done=%s\n",
               connection->backend_fin_was_deferred ? "yes" : "no",
               down->pending, down->pipe_pending,
               connection->to_backend.destination_shutdown ? "yes" : "no");
        fflush(stdout);
    }
    if (down->destination_shutdown &&
        connection->backend_fin_state == BACKEND_FIN_EOF_OBSERVED) {
        connection->backend_fin_state = BACKEND_FIN_FORWARDED;
        printf("backend-eof-state=forwarded; bytes=%" PRIu64
               "; pending=%zu; pipe-pending=%zu\n",
               down->forwarded, down->pending, down->pipe_pending);
        fflush(stdout);
    }
}

/* The two TCP halves are independent. Never release a session just because
 * the backend has shut down its sending half; that would discard a late
 * client upload. The integrity invariant is checked before clean teardown. */
static bool ready_for_clean_completion(const struct connection *connection) {
    return connection->client_fin_state == CLIENT_FIN_FORWARDED &&
           connection->backend_fin_state == BACKEND_FIN_FORWARDED &&
           fully_forwarded(connection);
}

/* SO_ERROR gives the pending socket error when epoll reports EPOLLERR.
 * EPOLLHUP and EPOLLRDHUP alone are NOT aborts: ordinary FIN can generate
 * readiness flags and must be drained normally through splice()/recv(). */
static bool socket_has_fatal_error(const struct endpoint *endpoint) {
    int error = 0;
    socklen_t length = sizeof(error);
    if (getsockopt(endpoint->fd, SOL_SOCKET, SO_ERROR, &error, &length) < 0) {
        report_error("getsockopt(SO_ERROR after EPOLLERR)");
        return true;
    }
    if (error == 0) return false;
    errno = error;
    /* A reset is expected in the Phase 23 fault tests. Keep the listener
     * alive; ordinary unreset connections still use the normal EOF path. */
    fprintf(stderr, "%s peer error: %s\n",
            endpoint->is_client ? "client" : "backend", strerror(error));
    return true;
}

static void dispatch_client_event(struct server *server, uint64_t token,
                                  uint32_t flags) {
    int fd = (int)(uint32_t)token;
    if (fd < 0 || (size_t)fd >= server->fd_capacity) return;
    struct endpoint *endpoint = server->by_fd[fd];
    /* epoll_wait() returns a BATCH. A previous event may have released this
     * connection, and Linux may have reused its fd. Token equality prevents
     * dereferencing freed memory or dispatching an event to the wrong peer. */
    if (endpoint == NULL || endpoint->token != token) return;
    struct connection *connection = endpoint->owner;
    if (connection == NULL || connection->lifecycle != CONN_ACTIVE) return;
    enum close_reason endpoint_failure = endpoint->is_client ?
                                          CLOSE_CLIENT_ABORT : CLOSE_BACKEND_ABORT;
    if (flags & EPOLLRDHUP) {
        if (endpoint->is_client) connection->client_rdhup_hint = true;
        else connection->backend_rdhup_hint = true;
    }
    if (connection->backend_state == BACKEND_CONNECTING) {
        if (endpoint->is_client) {
            bool ok = true;
            if (flags & EPOLLERR) ok = !socket_has_fatal_error(endpoint);
            if (ok && (flags & (EPOLLIN | EPOLLHUP | EPOLLRDHUP)))
                ok = stage_client_data(connection);
            track_client_fin(connection);
            if (!ok) remove_connection(server, connection, CLOSE_CLIENT_ABORT);
            else if (!update_interests(server, connection))
                remove_connection(server, connection, CLOSE_INTERNAL_ERROR);
            return;
        }
        if (!(flags & (EPOLLOUT | EPOLLERR | EPOLLHUP))) return;
        if (!complete_backend_connect(server, connection)) {
            remove_connection(server, connection, CLOSE_BACKEND_CONNECT_FAILURE);
            return;
        }
        /* Existing backend event may also carry normal readiness flags. */
    }
    if ((flags & EPOLLERR) && socket_has_fatal_error(endpoint)) {
        remove_connection(server, connection, endpoint_failure);
        return;
    }
    struct direction *incoming = endpoint->is_client ?
                                 &connection->to_backend : &connection->to_client;
    struct direction *outgoing = endpoint->is_client ?
                                 &connection->to_client : &connection->to_backend;
    bool ok = true;
    if (flags & EPOLLOUT) ok = flush_spliced_direction(outgoing);
    if (ok && (flags & (EPOLLIN | EPOLLHUP | EPOLLRDHUP)))
        ok = read_spliced_direction(incoming);
    if (!ok) {
        remove_connection(server, connection, endpoint_failure);
        return;
    }
    if (!direction_accounting_valid(&connection->to_backend) ||
        !direction_accounting_valid(&connection->to_client)) {
        fputs("splice accounting invariant failed\n", stderr);
        remove_connection(server, connection, CLOSE_INTERNAL_ERROR);
        return;
    }
    track_client_fin(connection);
    track_backend_fin(connection);
    if (ready_for_clean_completion(connection)) {
        puts("connection-completion=graceful; both-fins-forwarded=yes; "
             "queued-to-backend=0; queued-to-client=0");
        fflush(stdout);
        remove_connection(server, connection, CLOSE_GRACEFUL);
        return;
    }
    if (!update_interests(server, connection))
        remove_connection(server, connection, CLOSE_INTERNAL_ERROR);
}

static void cleanup_server(struct server *server) {
    while (server->connections != NULL)
        remove_connection(server, server->connections, CLOSE_SERVER_SHUTDOWN);
    if (server->listener >= 0) close(server->listener);
    if (server->epoll_fd >= 0) close(server->epoll_fd);
    free(server->by_fd);
}

int main(int argc, char **argv) {
    /* splice(pipe -> socket) can raise SIGPIPE on a disconnected peer.
     * Treat EPIPE as a connection-local error instead of killing the proxy. */
    if (signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        report_error("signal(SIGPIPE)");
        return EXIT_FAILURE;
    }
    uint16_t listen_port;
    struct server server = {.listener = -1, .epoll_fd = -1};
    int arg = 1;
    /* Phase 18: accept both --algorithm NAME and --algorithm=NAME.
     * Preserve legacy positional startup and reject repeated selectors. */
    if (argc == 2 && strcmp(argv[1], "--help") == 0) {
        printf("Usage: %s [--algorithm round-robin|least-connections] "
               "<listen-port> <IPv4:port> [IPv4:port ...]\n"
               "       %s <listen-port> <backend-IPv4> <backend-port>\n"
               "       Alternative: --algorithm=round-robin|least-connections\n"
               "       --help displays this message; listen port 0 picks a free port.\n",
               argv[0], argv[0]);
        return EXIT_SUCCESS;
    }
    const char *requested_algorithm = NULL;
    if (argc > 1 && strcmp(argv[1], "--algorithm") == 0) {
        if (argc < 3) goto usage;
        requested_algorithm = argv[2];
        arg = 3;
    } else if (argc > 1 && strncmp(argv[1], "--algorithm=", 12) == 0) {
        requested_algorithm = argv[1] + 12;
        arg = 2;
    }
    if (requested_algorithm != NULL) {
        if (strcmp(requested_algorithm, "least-connections") == 0)
            server.algorithm = BALANCE_LEAST_CONNECTIONS;
        else if (strcmp(requested_algorithm, "round-robin") == 0)
            server.algorithm = BALANCE_ROUND_ROBIN;
        else
            goto usage;
    }
    if (argc - arg < 2 || !parse_port(argv[arg], true, &listen_port)) goto usage;

    if (argc - arg == 3 && strchr(argv[arg + 1], ':') == NULL) {
        /* Compatibility: <listen-port> <IPv4> <backend-port>. */
        uint16_t port;
        struct sockaddr_in *backend = &server.backends[0].address;
        if (!parse_port(argv[arg + 2], false, &port) ||
            inet_pton(AF_INET, argv[arg + 1], &backend->sin_addr) != 1) goto usage;
        backend->sin_family = AF_INET;
        backend->sin_port = htons(port);
        (void)snprintf(server.backends[0].label,
                       sizeof(server.backends[0].label), "%s:%u",
                       argv[arg + 1], (unsigned)port);
        server.backend_count = 1;
    } else {
        if ((size_t)(argc - arg - 1) > MAX_BACKENDS) goto usage;
        for (int i = arg + 1; i < argc; ++i) {
            if (!parse_backend_spec(argv[i],
                                    &server.backends[server.backend_count])) goto usage;
            ++server.backend_count;
        }
    }
    goto parsed;
usage:
    fprintf(stderr, "Usage: %s <listen-port: 0-65535> "
                    "<backend-IPv4> <backend-port: 1-65535>\n"
                    "   or: %s <listen-port: 0-65535> "
                    "<backend-IPv4:port> [backend-IPv4:port ...] (max %u)\n"
                    "   optional prefix: --algorithm round-robin|least-connections\n"
                    "   or: --algorithm=round-robin|least-connections\n"
                    "   for help: --help\n",
            argv[0], argv[0], (unsigned)MAX_BACKENDS);
    return EXIT_FAILURE;
parsed: ;
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
    if (server.backend_count == 1) {
        printf("Listening on 127.0.0.1:%u -> %s\n",
               (unsigned)ntohs(local.sin_port), server.backends[0].label);
    } else {
        printf("Listening on 127.0.0.1:%u -> %s (%zu backends)\n",
               (unsigned)ntohs(local.sin_port),
               server.algorithm == BALANCE_LEAST_CONNECTIONS ?
               "least-connections" : "round-robin", server.backend_count);
        for (size_t i = 0; i < server.backend_count; ++i)
            printf("Backend[%zu]=%s\n", i, server.backends[i].label);
    }
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
