/* Phase 13 test-only LD_PRELOAD probe.
 * Reduce pipe capacity to one page, then observe REAL splice syscalls.
 * Never linked into the production proxy. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

int pipe2(int pipefd[2], int flags) {
    long result = syscall(SYS_pipe2, pipefd, flags);
    int saved_errno = errno;
    if (result == 0) {
        if (fcntl(pipefd[1], F_SETPIPE_SZ, 4096) < 0) {
            int problem = errno;
            (void)close(pipefd[0]);
            (void)close(pipefd[1]);
            errno = problem;
            return -1;
        }
    }
    errno = saved_errno;
    return (int)result;
}

ssize_t splice(int input, off64_t *in_offset, int output,
               off64_t *out_offset, size_t count, unsigned int flags) {
    long result = syscall(SYS_splice, input, in_offset, output,
                          out_offset, count, flags);
    int saved_errno = errno;
    struct stat source, target;
    if (fstat(input, &source) == 0 && fstat(output, &target) == 0 &&
        S_ISSOCK(source.st_mode) && S_ISFIFO(target.st_mode)) {
        const char *path = getenv("PHASE13_SPLICE_LOG");
        if (path != NULL && *path) {
            char line[128];
            int size = 0;
            if (result > 0 && (size_t)result < count) {
                size = snprintf(line, sizeof(line), "SHORT requested=%zu moved=%ld\n",
                                count, result);
            } else if (result < 0 && saved_errno == EAGAIN) {
                int unread = 0;
                if (ioctl(input, FIONREAD, &unread) == 0 && unread > 0) {
                    /* Socket HAS unread data, so the blocking component is
                     * the destination pipe, not missing source bytes. */
                    size = snprintf(line, sizeof(line),
                                    "PIPE_FULL_EAGAIN socket_queued=%d\n", unread);
                }
            }
            if (size > 0 && (size_t)size < sizeof(line)) {
                int fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0600);
                if (fd >= 0) {
                    (void)write(fd, line, (size_t)size);
                    (void)close(fd);
                }
            }
        }
    }
    errno = saved_errno;
    return (ssize_t)result;
}
