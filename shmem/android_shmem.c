/*
 * libandroid-shmem de reemplazo (shmget/shmat/shmdt/shmctl) para PostgreSQL en Android.
 *
 * Diferencias con la libandroid-shmem de Termux (los dos bugs que reporta cairn-ehr):
 *   1. NO usa un directorio fijo /data/data/com.termux/files/usr/tmp: la metadata
 *      de cada clave vive en $TMPDIR (en runtime). Sin esto: EACCES + bucle infinito.
 *   2. NO usa /dev/ashmem (eliminado en Android 11+) ni ASharedMemory_create (que
 *      arrastra libandroid.so y choca con OpenSSL). Usa memfd_create(2) por syscall:
 *      solo depende de libc.
 *
 * Semantica minima que PostgreSQL necesita (sysv_shmem.c):
 *   - shmget(key, size, IPC_CREAT|IPC_EXCL) -> EEXIST si hay un postmaster VIVO con esa clave.
 *   - Un segmento cuyo creador murio se considera inexistente (se limpia solo).
 *   - shmctl(IPC_STAT).shm_nattch >= 1 mientras el creador vive (deteccion de "en uso").
 *   - shmctl(IPC_RMID) borra la metadata.
 * Los backends son fork() del postmaster y heredan el mapeo; no necesitan shmget.
 *
 * Compilar para Android con scripts/build-shmem-fix.sh (usa los headers del propio
 * paquete Termux para que el layout de struct shmid_ds coincida con el de postgres).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/mman.h>
#include <sys/shm.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef __NR_memfd_create
#if defined(__aarch64__)
#define __NR_memfd_create 279
#elif defined(__x86_64__)
#define __NR_memfd_create 319
#elif defined(__arm__)
#define __NR_memfd_create 385
#endif
#endif

#define MAX_SEGS 64
#define MAX_ATT 128

struct seg {
    int used;
    int id;
    key_t key;
    int fd;            /* fd local (memfd) o -1 si es ajeno y aun no abierto */
    size_t size;
    pid_t owner;       /* pid del creador */
    int owner_fd;      /* numero de fd en el proceso creador (para /proc/<pid>/fd/<n>) */
    int nattch;
    int removed;
};

struct att {
    void *addr;
    size_t len;
    int id;
};

static struct seg segs[MAX_SEGS];
static struct att atts[MAX_ATT];
static int next_id = 1;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;

static int memfd(const char *name) {
    return (int)syscall(__NR_memfd_create, name, 1u /* MFD_CLOEXEC */);
}

static const char *tmpdir(void) {
    const char *d = getenv("TMPDIR");
    if (d && *d) return d;
    return "/data/local/tmp";
}

static void meta_path(char *buf, size_t n, key_t key) {
    snprintf(buf, n, "%s/.android-shm-%d", tmpdir(), (int)key);
}

static int pid_alive(pid_t pid) {
    if (pid <= 0) return 0;
    if (kill(pid, 0) == 0) return 1;
    return errno == EPERM;
}

/* 0 = ok, -1 = no existe / ilegible */
static int meta_read(key_t key, pid_t *pid, int *fd, size_t *size) {
    char p[512], line[128];
    long lpid = 0, lfd = 0, lsize = 0;
    meta_path(p, sizeof p, key);
    FILE *f = fopen(p, "r");
    if (!f) return -1;
    int ok = fgets(line, sizeof line, f) && sscanf(line, "%ld %ld %ld", &lpid, &lfd, &lsize) == 3;
    fclose(f);
    if (!ok) return -1;
    *pid = (pid_t)lpid;
    *fd = (int)lfd;
    *size = (size_t)lsize;
    return 0;
}

/* 0 = creada, -1 con errno (EEXIST si ya existia) */
static int meta_create(key_t key, pid_t pid, int fd, size_t size) {
    char p[512], line[128];
    meta_path(p, sizeof p, key);
    int mfd = open(p, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (mfd < 0) return -1;
    int n = snprintf(line, sizeof line, "%ld %d %ld\n", (long)pid, fd, (long)size);
    ssize_t w = write(mfd, line, (size_t)n);
    close(mfd);
    if (w != n) {
        unlink(p);
        errno = EIO;
        return -1;
    }
    return 0;
}

static void meta_unlink(key_t key) {
    char p[512];
    meta_path(p, sizeof p, key);
    unlink(p);
}

static struct seg *by_id(int id) {
    for (int i = 0; i < MAX_SEGS; i++)
        if (segs[i].used && segs[i].id == id) return &segs[i];
    return NULL;
}

static struct seg *by_key(key_t key) {
    for (int i = 0; i < MAX_SEGS; i++)
        if (segs[i].used && !segs[i].removed && segs[i].key == key) return &segs[i];
    return NULL;
}

static struct seg *new_seg(void) {
    for (int i = 0; i < MAX_SEGS; i++) {
        if (!segs[i].used) {
            memset(&segs[i], 0, sizeof segs[i]);
            segs[i].used = 1;
            segs[i].id = next_id++;
            segs[i].fd = -1;
            return &segs[i];
        }
    }
    return NULL;
}

int shmget(key_t key, size_t size, int flags) {
    pthread_mutex_lock(&lock);
    int ret = -1;
    long pg = sysconf(_SC_PAGESIZE);
    size_t rsize = (size + (size_t)pg - 1) & ~((size_t)pg - 1);
    struct seg *s;

    if (key != IPC_PRIVATE) {
        s = by_key(key);
        if (s) {
            if ((flags & IPC_CREAT) && (flags & IPC_EXCL)) { errno = EEXIST; goto out; }
            if (size > s->size) { errno = EINVAL; goto out; }
            ret = s->id;
            goto out;
        }
        pid_t opid; int ofd; size_t osize;
        if (meta_read(key, &opid, &ofd, &osize) == 0) {
            if (opid != getpid() && pid_alive(opid)) {
                /* postmaster vivo con esta clave */
                if ((flags & IPC_CREAT) && (flags & IPC_EXCL)) { errno = EEXIST; goto out; }
                if (size > osize) { errno = EINVAL; goto out; }
                s = new_seg();
                if (!s) { errno = ENOSPC; goto out; }
                s->key = key; s->size = osize; s->owner = opid; s->owner_fd = ofd;
                ret = s->id;
                goto out;
            }
            meta_unlink(key);   /* creador muerto: segmento huerfano -> inexistente */
        }
        if (!(flags & IPC_CREAT)) { errno = ENOENT; goto out; }
    }

    if (rsize == 0) { errno = EINVAL; goto out; }
    s = new_seg();
    if (!s) { errno = ENOSPC; goto out; }
    int fd = memfd("pg-shm");
    if (fd < 0) { s->used = 0; goto out; }               /* errno de la syscall */
    if (ftruncate(fd, (off_t)rsize) != 0) { int e = errno; close(fd); s->used = 0; errno = e; goto out; }
    if (key != IPC_PRIVATE && meta_create(key, getpid(), fd, rsize) != 0) {
        int e = errno; close(fd); s->used = 0; errno = e; goto out;
    }
    s->key = key; s->fd = fd; s->size = rsize; s->owner = getpid(); s->owner_fd = fd;
    ret = s->id;
out:
    pthread_mutex_unlock(&lock);
    return ret;
}

void *shmat(int id, const void *addr, int flags) {
    void *ret = (void *)-1;
    pthread_mutex_lock(&lock);
    struct seg *s = by_id(id);
    if (!s) { errno = EINVAL; goto out; }
    if (s->fd < 0) {
        char p[64];
        snprintf(p, sizeof p, "/proc/%d/fd/%d", (int)s->owner, s->owner_fd);
        s->fd = open(p, O_RDWR | O_CLOEXEC);
        if (s->fd < 0) { errno = EACCES; goto out; }     /* PostgreSQL lo toma como "en uso" */
    }
    int slot = -1;
    for (int i = 0; i < MAX_ATT; i++) if (!atts[i].addr) { slot = i; break; }
    if (slot < 0) { errno = ENOMEM; goto out; }
    int prot = PROT_READ | ((flags & SHM_RDONLY) ? 0 : PROT_WRITE);
    void *m = mmap((void *)addr, s->size, prot, MAP_SHARED, s->fd, 0);
    if (m == MAP_FAILED) goto out;
    atts[slot].addr = m; atts[slot].len = s->size; atts[slot].id = id;
    s->nattch++;
    ret = m;
out:
    pthread_mutex_unlock(&lock);
    return ret;
}

int shmdt(const void *addr) {
    int ret = -1;
    pthread_mutex_lock(&lock);
    for (int i = 0; i < MAX_ATT; i++) {
        if (atts[i].addr && atts[i].addr == addr) {
            munmap(atts[i].addr, atts[i].len);
            struct seg *s = by_id(atts[i].id);
            if (s && s->nattch > 0) s->nattch--;
            atts[i].addr = NULL;
            ret = 0;
            goto out;
        }
    }
    errno = EINVAL;
out:
    pthread_mutex_unlock(&lock);
    return ret;
}

int shmctl(int id, int cmd, struct shmid_ds *buf) {
    int ret = -1;
    pthread_mutex_lock(&lock);
    struct seg *s = by_id(id);
    if (!s) { errno = EINVAL; goto out; }
    if (cmd == IPC_RMID) {
        if (s->key != IPC_PRIVATE && s->owner == getpid()) meta_unlink(s->key);
        s->removed = 1;     /* el mapeo sigue vivo hasta shmdt/exit */
        ret = 0;
    } else if (cmd == IPC_STAT) {
        if (!buf) { errno = EFAULT; goto out; }
        memset(buf, 0, sizeof *buf);
#ifdef HAVE_SHM_SEGSZ
        buf->shm_segsz = s->size;
#endif
#ifdef HAVE_SHM_NATTCH
        if (s->owner == getpid()) buf->shm_nattch = (unsigned long)s->nattch;
        else buf->shm_nattch = pid_alive(s->owner) ? 1 : 0;
#endif
#ifdef HAVE_SHM_PERM_UID
        buf->shm_perm.uid = geteuid();
#endif
        ret = 0;
    } else {
        errno = EINVAL;
    }
out:
    pthread_mutex_unlock(&lock);
    return ret;
}


