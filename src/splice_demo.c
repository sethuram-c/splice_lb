#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L

/* A standalone Linux splice() experiment: regular file -> pipe -> file.
 * The TCP proxy keeps its proven recv()/send() data path in Phase 10;
 * socket-to-pipe splice() begins in Phase 11. */
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define CHUNK_SIZE 65536U

static void error(const char *operation) {
    fprintf(stderr, "%s failed: %s\n", operation, strerror(errno));
}

static void close_if_valid(int fd) {
    if (fd >= 0 && close(fd) < 0) error("close");
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "Usage: %s <input-file> <output-file>\n", argv[0]);
        return EXIT_FAILURE;
    }

    int in_fd = -1, out_fd = -1;
    int pipefd[2] = {-1, -1};
    int status = EXIT_FAILURE;
    uint64_t copied = 0;

    in_fd = open(argv[1], O_RDONLY | O_CLOEXEC);
    if (in_fd < 0) { error("open(input)"); goto done; }

    struct stat source;
    if (fstat(in_fd, &source) < 0) { error("fstat(input)"); goto done; }
    if (!S_ISREG(source.st_mode)) {
        fputs("Input must be a regular file\n", stderr);
        goto done;
    }

    /* Do not truncate an input file when both paths name the same inode. */
    out_fd = open(argv[2], O_WRONLY | O_CREAT | O_CLOEXEC, 0600);
    if (out_fd < 0) { error("open(output)"); goto done; }
    struct stat destination;
    if (fstat(out_fd, &destination) < 0) { error("fstat(output)"); goto done; }
    if (!S_ISREG(destination.st_mode) ||
        (source.st_dev == destination.st_dev && source.st_ino == destination.st_ino)) {
        fputs("Output must be a different regular file\n", stderr);
        goto done;
    }
    if (ftruncate(out_fd, 0) < 0) { error("ftruncate(output)"); goto done; }
    if (pipe2(pipefd, O_CLOEXEC) < 0) { error("pipe2"); goto done; }

    for (;;) {
        ssize_t filled = splice(in_fd, NULL, pipefd[1], NULL, CHUNK_SIZE, 0);
        if (filled < 0 && errno == EINTR) continue;
        if (filled < 0) { error("splice(file->pipe)"); goto done; }
        if (filled == 0) break; /* source file EOF */

        size_t remaining = (size_t)filled;
        while (remaining > 0) {
            ssize_t drained = splice(pipefd[0], NULL, out_fd, NULL,
                                     remaining, 0);
            if (drained < 0 && errno == EINTR) continue;
            if (drained <= 0) {
                if (drained < 0) error("splice(pipe->file)");
                else fputs("splice(pipe->file) made no progress\n", stderr);
                goto done;
            }
            remaining -= (size_t)drained;
            copied += (uint64_t)drained;
        }
    }
    printf("splice-demo: transferred %" PRIu64 " bytes\n", copied);
    status = EXIT_SUCCESS;
done:
    close_if_valid(pipefd[0]);
    close_if_valid(pipefd[1]);
    close_if_valid(out_fd);
    close_if_valid(in_fd);
    return status;
}
