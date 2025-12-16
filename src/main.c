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

#define DEFAULT_PORT 9090U
#define LISTEN_BACKLOG 16

static bool parse_port(const char *text, uint16_t *port) {
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
    if (errno != 0 || *end != '\0' || value > UINT16_MAX) {
        return false;
    }
    *port = (uint16_t)value;
    return true;
}

static void report_error(const char *operation) {
    fprintf(stderr, "%s failed: %s\n", operation, strerror(errno));
}

int main(int argc, char **argv) {
    uint16_t port = DEFAULT_PORT;
    if (argc > 2 || (argc == 2 && !parse_port(argv[1], &port))) {
        fprintf(stderr, "Usage: %s [port: 0-65535]\n", argv[0]);
        return EXIT_FAILURE;
    }

    int listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) {
        report_error("socket()");
        return EXIT_FAILURE;
    }

    int result = EXIT_FAILURE;
    int client = -1;
    const int reuse = 1;
    if (setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse)) < 0) {
        report_error("setsockopt(SO_REUSEADDR)");
        goto cleanup;
    }

    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

    if (bind(listener, (const struct sockaddr *)&address, sizeof(address)) < 0) {
        report_error("bind()");
        goto cleanup;
    }
    if (listen(listener, LISTEN_BACKLOG) < 0) {
        report_error("listen()");
        goto cleanup;
    }

    /* Port 0 asks the OS to choose a free ephemeral listening port. */
    socklen_t address_len = sizeof(address);
    if (getsockname(listener, (struct sockaddr *)&address, &address_len) < 0) {
        report_error("getsockname()");
        goto cleanup;
    }
    printf("Listening on 127.0.0.1:%u\n", (unsigned)ntohs(address.sin_port));
    fflush(stdout);  /* Let test clients discover the chosen port before accept(). */

    struct sockaddr_in peer = {0};
    socklen_t peer_len = sizeof(peer);
    do {
        client = accept(listener, (struct sockaddr *)&peer, &peer_len);
    } while (client < 0 && errno == EINTR);
    if (client < 0) {
        report_error("accept()");
        goto cleanup;
    }

    char peer_ip[INET_ADDRSTRLEN];
    if (inet_ntop(AF_INET, &peer.sin_addr, peer_ip, sizeof(peer_ip)) == NULL) {
        report_error("inet_ntop()");
        goto cleanup;
    }
    printf("Accepted client %s:%u (fd=%d)\n", peer_ip,
           (unsigned)ntohs(peer.sin_port), client);
    result = EXIT_SUCCESS;

cleanup:
    if (client >= 0 && close(client) < 0) {
        report_error("close(client)");
        result = EXIT_FAILURE;
    }
    if (close(listener) < 0) {
        report_error("close(listener)");
        result = EXIT_FAILURE;
    }
    if (result == EXIT_SUCCESS) {
        puts("Closed client and listening sockets");
    }
    return result;
}
