/* Test-only LD_PRELOAD shim. Suppress the first pending backend EPOLLOUT
 * notification to simulate a hung TCP handshake deterministically. It does
 * not alter the production binary and should never be used for real traffic. */
#define _GNU_SOURCE
#include <stddef.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <sys/epoll.h>
#include <sys/socket.h>

static bool suppressed;

int epoll_ctl(int efd, int op, int fd, struct epoll_event *event) {
    if (!suppressed && op == EPOLL_CTL_ADD && event != NULL &&
        event->events == EPOLLOUT) {
        struct epoll_event modified = *event;
        modified.events = EPOLLIN; /* silent backend provides no bytes */
        int result = (int)syscall(SYS_epoll_ctl, efd, op, fd, &modified);
        if (result == 0) suppressed = true;
        return result;
    }
    return (int)syscall(SYS_epoll_ctl, efd, op, fd, event);
}
