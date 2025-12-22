#define _GNU_SOURCE
/* Test-only LD_PRELOAD shim: defer backend EPOLLOUT notifications for 250ms.
 * This creates a deterministic window in which the proxy must stage client
 * bytes while BACKEND_CONNECTING. Never include this in the production build.
 */
#include <dlfcn.h>
#include <errno.h>
#include <stdint.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <time.h>

static int backend_fd = -1;
static struct timespec allow_after;

static int delay_active(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0;
    return now.tv_sec < allow_after.tv_sec ||
           (now.tv_sec == allow_after.tv_sec && now.tv_nsec < allow_after.tv_nsec);
}

int connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    int (*original)(int, const struct sockaddr *, socklen_t) =
        (int (*)(int, const struct sockaddr *, socklen_t))dlsym(RTLD_NEXT, "connect");
    if (original == NULL) { errno = ENOSYS; return -1; }
    int result = original(sockfd, addr, addrlen);
    if (result == 0 || (result == -1 && errno == EINPROGRESS)) {
        backend_fd = sockfd;
        if (clock_gettime(CLOCK_MONOTONIC, &allow_after) == 0) {
            allow_after.tv_nsec += 250000000L;
            if (allow_after.tv_nsec >= 1000000000L) {
                allow_after.tv_nsec -= 1000000000L;
                allow_after.tv_sec++;
            }
        }
        /* Immediate success can also be represented by pending completion:
         * SO_ERROR will be zero once the deferred event is delivered. */
        errno = EINPROGRESS;
        return -1;
    }
    return result;
}

int epoll_wait(int epfd, struct epoll_event *events, int maxevents, int timeout) {
    int (*original)(int, struct epoll_event *, int, int) =
        (int (*)(int, struct epoll_event *, int, int))dlsym(RTLD_NEXT, "epoll_wait");
    if (original == NULL) { errno = ENOSYS; return -1; }
    int count = original(epfd, events, maxevents, timeout);
    if (count <= 0 || backend_fd < 0 || !delay_active()) return count;
    int kept = 0;
    for (int i = 0; i < count; ++i) {
        /* The proxy encodes the descriptor in the low 32 bits of its token. */
        if ((int)(uint32_t)events[i].data.u64 != backend_fd)
            events[kept++] = events[i];
    }
    if (kept == 0) {
        struct timespec pause = {.tv_sec = 0, .tv_nsec = 5000000L};
        (void)nanosleep(&pause, NULL); /* do not spin on permanently writable fd */
    }
    return kept;
}
