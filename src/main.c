#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

int main(void) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd == -1) {
        fprintf(stderr, "socket() failed: %s\n", strerror(errno));
        return 1;
    }

    printf("TCP socket created successfully (fd=%d)\n", fd);

    if (close(fd) == -1) {
        fprintf(stderr, "close() failed: %s\n", strerror(errno));
        return 1;
    }
    puts("TCP socket closed successfully");
    return 0;
}
