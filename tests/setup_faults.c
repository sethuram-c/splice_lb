#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

/* Test-only LD_PRELOAD shim. Intercepts selected setup syscalls, deliberately
 * fails ONCE, then forwards future calls to the real kernel via syscall().
 * This file is NEVER linked into the production splice-lb binary.
 */
static bool injected;
static unsigned sockets_seen;
static unsigned pipes_seen;
static unsigned adds_seen;

static bool should_fail(const char *stage) {
    const char *chosen = getenv("PHASE24_FAIL_STAGE");
    if (injected || chosen == NULL || strcmp(stage, chosen) != 0) return false;
    injected = true;
    const char *marker = getenv("PHASE24_FAULT_MARKER");
    if (marker != NULL && marker[0] != '\0') {
        int fd = open(marker, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
        if (fd >= 0) {
            (void)write(fd, stage, strlen(stage));
            (void)close(fd);
        }
    }
    const char head[] = "phase24-injected-failure=";
    (void)write(STDERR_FILENO, head, sizeof(head) - 1U);
    (void)write(STDERR_FILENO, stage, strlen(stage));
    (void)write(STDERR_FILENO, "\n", 1);
    errno = EMFILE; /* mimic resource exhaustion for resource setup calls */
    return true;
}

int socket(int domain, int type, int protocol) {
    if (domain == AF_INET && (type & SOCK_STREAM) == SOCK_STREAM) {
        ++sockets_seen;
        if (sockets_seen == 2 && should_fail("backend-socket")) return -1;
    }
    return (int)syscall(SYS_socket, domain, type, protocol);
}

int pipe2(int pipefd[2], int flags) {
    ++pipes_seen;
    if (pipes_seen == 1 && should_fail("pipe-first")) return -1;
    if (pipes_seen == 2 && should_fail("pipe-second")) return -1;
    return (int)syscall(SYS_pipe2, pipefd, flags);
}

int epoll_ctl(int epfd, int op, int fd, struct epoll_event *event) {
    /* Phase 27: ignore the control-plane signalfd registration. */
    if (op == EPOLL_CTL_ADD && event != NULL && event->data.u64 != UINT64_MAX) {
        ++adds_seen; /* first listener, second client, third backend */
        if (adds_seen == 2 && should_fail("epoll-client")) return -1;
        if (adds_seen == 3 && should_fail("epoll-backend")) return -1;
    }
    return (int)syscall(SYS_epoll_ctl, epfd, op, fd, event);
}
