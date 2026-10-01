/* Test en host Linux: gcc -DHAVE_SHM_SEGSZ -DHAVE_SHM_NATTCH -DHAVE_SHM_PERM_UID test_shmem.c android_shmem.c */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/shm.h>
#include <unistd.h>

int main(int argc, char **argv) {
    int key = atoi(argv[2]);
    const char *m = argv[1];
    if (!strcmp(m, "create")) {            /* crea, escribe y queda vivo N seg */
        int id = shmget(key, 8192, IPC_CREAT | IPC_EXCL | 0600);
        if (id < 0) { printf("create FAIL errno=%d\n", errno); return 1; }
        char *p = shmat(id, NULL, 0);
        strcpy(p, "PGMAGIC");
        struct shmid_ds ds; shmctl(id, IPC_STAT, &ds);
        printf("create OK id=%d nattch=%lu segsz=%zu\n", id, (unsigned long)ds.shm_nattch, (size_t)ds.shm_segsz);
        fflush(stdout);
        sleep(atoi(argv[3]));
        shmctl(id, IPC_RMID, NULL);
        return 0;
    }
    if (!strcmp(m, "excl")) {              /* como PGSharedMemoryCreate */
        int id = shmget(key, 8192, IPC_CREAT | IPC_EXCL | 0600);
        printf("excl -> %s errno=%d\n", id < 0 ? "FAIL" : "OK(created)", id < 0 ? errno : 0);
        if (id < 0 && errno == EEXIST) {
            int old = shmget(key, 56, 0);
            struct shmid_ds ds; memset(&ds, 0, sizeof ds);
            shmctl(old, IPC_STAT, &ds);
            printf("   existing id=%d nattch=%lu uid_ok=%d\n", old, (unsigned long)ds.shm_nattch, ds.shm_perm.uid == geteuid());
        }
        return 0;
    }
    if (!strcmp(m, "attach")) {            /* adjuntar segmento ajeno via /proc/pid/fd */
        int id = shmget(key, 56, 0);
        char *p = id < 0 ? (char *)-1 : shmat(id, NULL, 0);
        printf("attach foreign -> %s\n", p == (char *)-1 ? "FAIL(EACCES=en uso)" : p);
        return 0;
    }
    if (!strcmp(m, "missing")) {
        int id = shmget(key, 56, 0);
        printf("missing -> id=%d errno=%d (ENOENT=%d)\n", id, errno, ENOENT);
        return 0;
    }
    return 2;
}
