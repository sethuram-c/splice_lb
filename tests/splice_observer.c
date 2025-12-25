/* Test-only LD_PRELOAD interposer. Observe real socket -> pipe splice traffic
 * without substituting a fake implementation or modifying the proxy binary. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

ssize_t splice(int in_fd, off64_t *in_offset, int out_fd,
               off64_t *out_offset, size_t length, unsigned int flags) {
    long result = syscall(SYS_splice, in_fd, in_offset, out_fd,
                          out_offset, length, flags);
    int saved_errno = errno;
    if (result > 0) {
        struct stat in_info, out_info;
        if (fstat(in_fd, &in_info) == 0 && fstat(out_fd, &out_info) == 0 &&
            S_ISSOCK(in_info.st_mode) && S_ISFIFO(out_info.st_mode)) {
            const char *path = getenv("SPLICE_AUDIT_FILE");
            if (path != NULL && *path) {
                int out = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC,
                               0600);
                if (out >= 0) {
                    (void)dprintf(out, "socket-to-pipe bytes=%ld flags=%u\n",
                                  result, flags);
                    (void)close(out);
                }
            }
        }
    }
    errno = saved_errno;
    return (ssize_t)result;
}
