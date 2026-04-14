/*
 * Entrypoint wrapper for redroid containers.
 * Starts create_touch in background, waits for /dev/input/ to be populated,
 * then exec's /init_orig with all original arguments.
 *
 * Compile: gcc -static -o init_wrapper init_wrapper.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <dirent.h>
#include <string.h>
#include <errno.h>

static int has_event_device(void) {
    DIR *d = opendir("/dev/input");
    if (!d) return 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (strncmp(e->d_name, "event", 5) == 0) {
            closedir(d);
            return 1;
        }
    }
    closedir(d);
    return 0;
}

int main(int argc, char *argv[]) {
    /* Fork create_touch into background */
    pid_t pid = fork();
    if (pid == 0) {
        setsid();
        execl("/system/bin/create_touch", "create_touch", (char *)NULL);
        _exit(1);
    }

    if (pid > 0) {
        /* Wait up to 3 seconds for the device node to appear */
        for (int i = 0; i < 30; i++) {
            usleep(100000);
            if (has_event_device())
                break;
        }
    }

    /* Build argv for /init_orig, passing through all original args */
    char **new_argv = malloc(sizeof(char *) * (argc + 1));
    new_argv[0] = "/init_orig";
    for (int i = 1; i < argc; i++)
        new_argv[i] = argv[i];
    new_argv[argc] = NULL;

    execv("/init_orig", new_argv);
    perror("execv /init_orig");
    return 1;
}
