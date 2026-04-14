/*
 * Virtual keyboard daemon for redroid containers.
 * Creates a uinput device then continuously ensures the /dev/input/eventN
 * node exists (Android's init remounts /dev as tmpfs, erasing early nodes).
 *
 * Compile: gcc -static -o create_keyboard create_keyboard.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <dirent.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/ioctl.h>
#include <linux/uinput.h>
#include <linux/input.h>

static volatile int running = 1;

static void handle_signal(int sig) {
    (void)sig;
    running = 0;
}

/*
 * Find our device in sysfs, return eventN number (or -1).
 * Also outputs major:minor.
 */
static int find_event_info(int *out_major, int *out_minor) {
    DIR *inputdir = opendir("/sys/devices/virtual/input");
    if (!inputdir) return -1;

    struct dirent *d;
    int evnum = -1;
    while ((d = readdir(inputdir)) != NULL) {
        if (strncmp(d->d_name, "input", 5) != 0) continue;

        char path[256];
        snprintf(path, sizeof(path), "/sys/devices/virtual/input/%s/name", d->d_name);
        FILE *f = fopen(path, "r");
        if (!f) continue;
        char name[64] = {0};
        if (fgets(name, sizeof(name), f)) {
            char *nl = strchr(name, '\n');
            if (nl) *nl = '\0';
        }
        fclose(f);

        if (strcmp(name, "qemu-keyboard") != 0) continue;

        /* Find eventN */
        char ipath[256];
        snprintf(ipath, sizeof(ipath), "/sys/devices/virtual/input/%s", d->d_name);
        DIR *evdir = opendir(ipath);
        if (!evdir) continue;

        struct dirent *e;
        while ((e = readdir(evdir)) != NULL) {
            if (strncmp(e->d_name, "event", 5) != 0) continue;
            evnum = atoi(e->d_name + 5);

            char devpath[256];
            snprintf(devpath, sizeof(devpath), "%s/%s/dev", ipath, e->d_name);
            FILE *devf = fopen(devpath, "r");
            if (devf) {
                fscanf(devf, "%d:%d", out_major, out_minor);
                fclose(devf);
            }
            break;
        }
        closedir(evdir);
        break;
    }
    closedir(inputdir);
    return evnum;
}

static void ensure_dev_node(int evnum, int major, int minor) {
    char nodepath[64];
    snprintf(nodepath, sizeof(nodepath), "/dev/input/event%d", evnum);

    struct stat st;
    if (stat(nodepath, &st) == 0 && S_ISCHR(st.st_mode)) {
        /* Ensure correct ownership/perms even if node already exists */
        if (st.st_gid != 1004 || (st.st_mode & 0777) != 0660) {
            chmod(nodepath, 0660);
            chown(nodepath, 0, 1004);
        }
        return;
    }

    mkdir("/dev/input", 0755);
    unlink(nodepath);
    mode_t old_umask = umask(0);
    if (mknod(nodepath, S_IFCHR | 0660, makedev(major, minor)) == 0) {
        chown(nodepath, 0, 1004);
        fprintf(stderr, "create_keyboard: created %s (root:input 0660)\n", nodepath);
    }
    umask(old_umask);
}

int main(void) {
    signal(SIGTERM, handle_signal);
    signal(SIGINT, handle_signal);

    int fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
    if (fd < 0) {
        perror("open /dev/uinput");
        return 1;
    }

    /* Setup events: SYN for sync, KEY for keyboard */
    ioctl(fd, UI_SET_EVBIT, EV_SYN);
    ioctl(fd, UI_SET_EVBIT, EV_KEY);

    /* Enable most standard keyboard keys (A-Z, 0-9, special keys) */
    for (int i = KEY_ESC; i < KEY_CNT; i++) {
        ioctl(fd, UI_SET_KEYBIT, i);
    }

    struct uinput_setup usetup = {0};
    snprintf(usetup.name, UINPUT_MAX_NAME_SIZE, "qemu-keyboard");
    usetup.id.bustype = BUS_USB;
    usetup.id.vendor  = 0x0627;  /* QEMU vendor ID */
    usetup.id.product = 0x0001;
    usetup.id.version = 0x0100;
    ioctl(fd, UI_DEV_SETUP, &usetup);
    ioctl(fd, UI_DEV_CREATE);

    usleep(300000);

    int major = 0, minor = 0;
    int evnum = find_event_info(&major, &minor);
    if (evnum < 0) {
        fprintf(stderr, "create_keyboard: could not find event device in sysfs\n");
    }

    /*
     * Poll loop: keep checking that the /dev/input/eventN node exists.
     * Android's init remounts /dev as tmpfs, so we may need to recreate it.
     * We check frequently during the first 60s (EventHub startup window),
     * then less often.
     */
    int checks = 0;
    while (running) {
        if (evnum >= 0)
            ensure_dev_node(evnum, major, minor);

        if (checks < 120)
            usleep(500000);    /* 0.5s for first 60s */
        else
            sleep(10);         /* 10s after that */
        checks++;
    }

    ioctl(fd, UI_DEV_DESTROY);
    close(fd);
    return 0;
}
