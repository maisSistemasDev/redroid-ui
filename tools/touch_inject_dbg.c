#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <linux/input.h>

int main(int argc, char *argv[]) {
    if (argc < 2) return 1;
    int fd = open(argv[1], O_WRONLY);
    if (fd < 0) { perror("open"); return 1; }
    setbuf(stdin, NULL);
    struct input_event ev;
    int type, code, value;
    int count = 0;
    while (scanf("%d %d %d", &type, &code, &value) == 3) {
        memset(&ev, 0, sizeof(ev));
        ev.type = (__u16)type;
        ev.code = (__u16)code;
        ev.value = (__s32)value;
        ssize_t w = write(fd, &ev, sizeof(ev));
        fprintf(stderr, "event[%d]: type=%d code=%d value=%d write=%zd\n", count, type, code, value, w);
        count++;
    }
    fprintf(stderr, "total events: %d\n", count);
    close(fd);
    return 0;
}
