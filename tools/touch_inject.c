/*
 * Persistent touch event injector for redroid containers.
 * Reads "type code value" triples from stdin and writes
 * struct input_event directly to the device fd — no per-event
 * process spawn, atomic writes, minimal latency.
 *
 * Compile: gcc -static -o touch_inject touch_inject.c
 */
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <linux/input.h>

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: touch_inject /dev/input/eventN\n");
        return 1;
    }

    int fd = open(argv[1], O_WRONLY);
    if (fd < 0) {
        perror("open");
        return 1;
    }

    setbuf(stdin, NULL);

    struct input_event ev;
    int type, code, value;

    while (scanf("%d %d %d", &type, &code, &value) == 3) {
        memset(&ev, 0, sizeof(ev));
        ev.type = (__u16)type;
        ev.code = (__u16)code;
        ev.value = (__s32)value;
        write(fd, &ev, sizeof(ev));
    }

    close(fd);
    return 0;
}
