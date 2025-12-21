/* Test-only LD_PRELOAD interposer: check flags at the exact connect() call.
 * Never include this test helper in the production executable. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

int connect(int fd, const struct sockaddr *address, socklen_t length) {
    int flags = fcntl(fd, F_GETFL);
    int nonblocking = flags >= 0 && (flags & O_NONBLOCK) != 0;
    long result = syscall(SYS_connect, fd, address, length);
    int saved_errno = errno;

    const char *path = getenv("CONNECT_AUDIT_FILE");
    if (path != NULL && *path != '\0') {
        int out = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
        if (out >= 0) {
            (void)dprintf(out, "nonblocking=%d result=%ld error=%d\n",
                          nonblocking, result,
                          result == -1 ? saved_errno : 0);
            (void)close(out);
        }
    }
    errno = saved_errno;
    return (int)result;
}
