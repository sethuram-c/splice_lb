#define _POSIX_C_SOURCE 200809L
/* BENCHMARK ONLY: thread-per-connection, blocking select/recv/send relay.
 * Not linked into production splice-lb and not a general-purpose server. */
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

struct session { int client; int backend_port; };

static int send_all(int fd, const unsigned char *buffer, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        ssize_t sent = send(fd, buffer + offset, size - offset, MSG_NOSIGNAL);
        if (sent > 0) { offset += (size_t)sent; continue; }
        if (sent == -1 && errno == EINTR) continue;
        return -1;
    }
    return 0;
}

static void *relay(void *argument) {
    struct session *ctx = argument;
    int client = ctx->client, backend = -1;
    int backend_port = ctx->backend_port;
    free(ctx);
    backend = socket(AF_INET, SOCK_STREAM, 0);
    if (backend < 0) goto done;
    struct sockaddr_in target = {.sin_family = AF_INET,
        .sin_port = htons((uint16_t)backend_port)};
    if (inet_pton(AF_INET, "127.0.0.1", &target.sin_addr) != 1) goto done;
    if (connect(backend, (struct sockaddr *)&target, sizeof(target)) < 0) goto done;
    int c_open = 1, b_open = 1;
    unsigned char data[65536];
    while (c_open || b_open) {
        fd_set reads;
        FD_ZERO(&reads);
        if (c_open) FD_SET(client, &reads);
        if (b_open) FD_SET(backend, &reads);
        int high = client > backend ? client : backend;
        int ready = select(high + 1, &reads, NULL, NULL, NULL);
        if (ready < 0) { if (errno == EINTR) continue; break; }
        if (c_open && FD_ISSET(client, &reads)) {
            ssize_t n = recv(client, data, sizeof(data), 0);
            if (n > 0) { if (send_all(backend, data, (size_t)n) < 0) break; }
            else if (n == 0) { c_open = 0; (void)shutdown(backend, SHUT_WR); }
            else if (errno != EINTR) break;
        }
        if (b_open && FD_ISSET(backend, &reads)) {
            ssize_t n = recv(backend, data, sizeof(data), 0);
            if (n > 0) { if (send_all(client, data, (size_t)n) < 0) break; }
            else if (n == 0) { b_open = 0; (void)shutdown(client, SHUT_WR); }
            else if (errno != EINTR) break;
        }
    }
done:
    if (backend >= 0) close(backend);
    close(client);
    return NULL;
}

static int parse_port(const char *str, int allow_zero) {
    if (*str == '\0') return -1;
    char *end = NULL;
    errno = 0;
    unsigned long n = strtoul(str, &end, 10);
    if (errno || !end || *end || n > 65535 || (!allow_zero && n == 0)) return -1;
    return (int)n;
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: %s <listen-port-0-65535> <backend-port-1-65535>\n", argv[0]);
        return 2;
    }
    int port = parse_port(argv[1], 1), backend_port = parse_port(argv[2], 0);
    if (port < 0 || backend_port < 0) return 2;
    (void)signal(SIGPIPE, SIG_IGN);
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) { perror("socket"); return 1; }
    int reuse = 1;
    (void)setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
    if (inet_pton(AF_INET, "127.0.0.1", &address.sin_addr) != 1 ||
        bind(listener, (struct sockaddr *)&address, sizeof(address)) < 0 ||
        listen(listener, 1024) < 0) { perror("bind/listen"); close(listener); return 1; }
    socklen_t len = sizeof(address);
    if (getsockname(listener, (struct sockaddr *)&address, &len) < 0) {
        close(listener); return 1;
    }
    printf("Listening on 127.0.0.1:%u\n", (unsigned)ntohs(address.sin_port));
    fflush(stdout);
    for (;;) {
        int client = accept(listener, NULL, NULL);
        if (client < 0) { if (errno == EINTR) continue; perror("accept"); break; }
        struct session *ctx = malloc(sizeof(*ctx));
        if (!ctx) { close(client); continue; }
        *ctx = (struct session){.client = client, .backend_port = backend_port};
        pthread_t thread;
        if (pthread_create(&thread, NULL, relay, ctx) != 0) {
            close(client); free(ctx); continue;
        }
        (void)pthread_detach(thread);
    }
    close(listener);
    return 1;
}
