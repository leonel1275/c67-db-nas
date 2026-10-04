package com.systutor.g5;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/**
 * Servicio en primer plano: PostgreSQL 18 nativo (TCP 5432) + API NAS (Python/Chaquopy, 8000).
 * Sobrevive a la pantalla apagada (START_STICKY + WakeLock parcial) y arranca en boot (BootReceiver).
 */
public class PgService extends Service {
    static final String TAG = "G5";
    static final String ACTION_STOP = "com.c67.pg.STOP";
    static final int PG_PORT = 5432;
    static final int API_PORT = 8000;
    static final String NAS_PUBLIC = "/storage/emulated/0/NAS";

    private static final String CHANNEL = "c67_pg";
    private static final int NOTIF_ID = 67;
    private static final String ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";

    private static final StringBuilder LOG = new StringBuilder();
    private static volatile String status = "iniciando";
    private static final AtomicBoolean started = new AtomicBoolean(false);

    private volatile boolean stopping = false;
    private volatile boolean pgReady = false;
    private PowerManager.WakeLock wakeLock;

    // ---------- estado visible para la Activity ----------
    static synchronized void log(String s) {
        Log.i(TAG, s);
        LOG.append(s).append('\n');
        if (LOG.length() > 8000) {
            LOG.delete(0, LOG.length() - 6000);
        }
    }

    static synchronized String logTail() {
        return LOG.toString();
    }

    static String status() {
        return status;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            shutdownAndExit();
            return START_NOT_STICKY;
        }
        // Debe ser lo PRIMERO: startForegroundService() exige startForeground() en <5 s.
        startInForeground();
        if (wakeLock == null) {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "c67:pg");
            wakeLock.setReferenceCounted(false);
            wakeLock.acquire();
        }
        if (started.compareAndSet(false, true)) {
            new Thread(this::runAll, "c67-main").start();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        super.onDestroy();
    }

    // ---------- notificacion ----------
    private Notification buildNotification() {
        PendingIntent pi = PendingIntent.getActivity(this, 0,
                new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL)
                .setContentTitle("C67 DB+NAS")
                .setContentText(status)
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentIntent(pi)
                .setOngoing(true)
                .build();
    }

    private void startInForeground() {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        nm.createNotificationChannel(
                new NotificationChannel(CHANNEL, "C67 DB+NAS", NotificationManager.IMPORTANCE_LOW));
        Notification n = buildNotification();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(NOTIF_ID, n);
        }
    }

    private void setStatus(String s) {
        status = s;
        log("status: " + s);
        try {
            ((NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE))
                    .notify(NOTIF_ID, buildNotification());
        } catch (Throwable ignored) {
        }
    }

    // ---------- flujo principal ----------
    private void runAll() {
        try {
            File files = getFilesDir();
            File nativeDir = new File(getApplicationInfo().nativeLibraryDir);
            log("filesDir=" + files + " nativeDir=" + nativeDir);

            setStatus("preparando archivos");
            prepareRuntime(files, nativeDir);

            setStatus("inicializando base de datos");
            ensurePgData(files);

            new Thread(() -> pgSupervisor(files, nativeDir), "c67-pg").start();
            setStatus("arrancando PostgreSQL");
            if (!waitPgReady(180)) {
                setStatus("ERROR: PostgreSQL no arranco (adb logcat -s G5:*)");
                return;
            }

            try {
                File nas = prepareNas();
                String token = ensureToken(files);
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(this));
                }
                String res = Python.getInstance().getModule("c67core")
                        .callAttr("start", files.getAbsolutePath(), nas.getAbsolutePath(), token, API_PORT)
                        .toString();
                log(res);
                setStatus("listo: PostgreSQL :" + PG_PORT + " | NAS API :" + API_PORT);
                launchDockerVm();
            } catch (Throwable t) {
                log("NAS API FAIL " + t);
                setStatus("PostgreSQL OK; NAS API fallo: " + t.getMessage());
            }
        } catch (Throwable t) {
            log("FATAL " + t);
            setStatus("ERROR: " + t.getMessage());
        }
    }

    // ---------- preparacion de archivos ----------
    private void prepareRuntime(File files, File nativeDir) throws IOException {
        long updated;
        try {
            updated = getPackageManager().getPackageInfo(getPackageName(), 0).lastUpdateTime;
        } catch (Exception e) {
            updated = 0;
        }
        File usr = new File(files, "usr");
        File marker = new File(usr, ".assets-" + updated);
        if (!marker.exists()) {
            log("extrayendo pgsupport.zip");
            deleteTree(new File(usr, "share"));
            deleteTree(new File(usr, "lib"));
            File[] old = usr.listFiles((d, n) -> n.startsWith(".assets-"));
            if (old != null) {
                for (File f : old) {
                    f.delete();
                }
            }
            extractZip("pgsupport.zip", files);
            usr.mkdirs();
            if (!marker.createNewFile()) {
                log("aviso: no pude crear el marcador de assets");
            }
        }
        new File(files, "tmp").mkdirs();   // TMPDIR de postgres y del shim de shmem

        // initdb necesita un "postgres" HERMANO en su propio directorio (usr/bin).
        // - initdb se ejecuta con /system/bin/linker64 (W^X bypass) pero comprueba
        //   su propio bit +x, por lo que DEBE ser una copia real con chmod 755 (copyBin).
        // - postgres NO puede ser una copia en /data/data porque SELinux bloquea execve()
        //   en app_data_file. postgres DEBE ser un SYMLINK a libpostgres.so en nativeLibraryDir,
        //   donde SELinux sí permite execve() (apk_data_file).
        File bin = new File(usr, "bin");
        bin.mkdirs();
        File initdbBin = new File(bin, "initdb");
        File postgresBin = new File(bin, "postgres");

        // 1. initdb: copia real ejecutable (+x)
        copyBin(new File(nativeDir, "libinitdb.so"), initdbBin);

        // 2. postgres: symlink a libpostgres.so en nativeLibraryDir
        if (postgresBin.exists() || Files.isSymbolicLink(postgresBin.toPath())) {
            try {
                Files.delete(postgresBin.toPath());
            } catch (Throwable t) {
                postgresBin.delete();
            }
        }
        try {
            Files.createSymbolicLink(postgresBin.toPath(), new File(nativeDir, "libpostgres.so").toPath());
        } catch (Throwable t) {
            try {
                Runtime.getRuntime().exec(new String[]{"ln", "-sf",
                        new File(nativeDir, "libpostgres.so").getAbsolutePath(),
                        postgresBin.getAbsolutePath()}).waitFor();
            } catch (Throwable ignored) {}
        }
        log("bin listo: initdb size=" + initdbBin.length() + " exec=" + initdbBin.canExecute() + " path=" + initdbBin);
        log("bin listo: postgres symlink=" + Files.isSymbolicLink(postgresBin.toPath()) + " target=" + new File(nativeDir, "libpostgres.so"));
    }

    private File prepareNas() {
        File nas = new File(NAS_PUBLIC);
        if ((nas.isDirectory() || nas.mkdirs()) && nas.canWrite()) {
            log("NAS: " + nas);
            return nas;
        }
        File fallback = new File(getExternalFilesDir(null), "NAS");
        fallback.mkdirs();
        log("NAS: sin permiso en " + NAS_PUBLIC + " -> usando " + fallback
                + " (otorga 'Acceso a todos los archivos' y toca Detener + abrir)");
        return fallback;
    }

    // ---------- PostgreSQL ----------
    private Map<String, String> pgEnv(ProcessBuilder pb, File files, File nativeDir) {
        Map<String, String> env = pb.environment();
        env.put("LD_LIBRARY_PATH", nativeDir.getAbsolutePath());
        env.put("TMPDIR", new File(files, "tmp").getAbsolutePath());   // el shim de shmem lo lee en runtime
        env.put("HOME", files.getAbsolutePath());
        env.put("TZ", "UTC");
        String existingPath = env.getOrDefault("PATH", "/system/bin:/system/xbin");
        env.put("PATH", nativeDir.getAbsolutePath() + ":" + existingPath);
        return env;
    }

    private void ensurePgData(File files) throws Exception {
        File pgdata = new File(files, "pgdata");
        if (new File(pgdata, "PG_VERSION").exists()) {
            log("PGDATA existente");
            return;
        }
        File passFile = new File(files, "pg_password.txt");
        writeFile(passFile, randomString(24) + "\n");
        passFile.setReadable(false, false);
        passFile.setReadable(true, true);

        File nativeDir = new File(getApplicationInfo().nativeLibraryDir);
        int rc = runInitdb(files, nativeDir, pgdata, passFile, true);
        if (rc != 0) {
            log("initdb con proveedor 'builtin' fallo (rc=" + rc + "); reintento con locale C clasico");
            rc = runInitdb(files, nativeDir, pgdata, passFile, false);
        }
        if (rc != 0) {
            deleteTree(pgdata);
            throw new IOException("initdb fallo rc=" + rc + " (ver logcat -s G5:*)");
        }
    }

    private int runInitdb(File files, File nativeDir, File pgdata, File passFile, boolean builtin)
            throws Exception {
        deleteTree(pgdata);

        List<String> cmd = new ArrayList<>();
        // linker64 ejecuta initdb desde files/usr/bin (W^X bypass).
        cmd.add("/system/bin/linker64");
        cmd.add(new File(files, "usr/bin/initdb").getAbsolutePath());
        cmd.addAll(Arrays.asList("-D", pgdata.getAbsolutePath(), "-U", "postgres",
                "--auth=scram-sha-256", "--pwfile=" + passFile.getAbsolutePath(),
                "-E", "UTF8", "--no-instructions"));
        if (builtin) {
            cmd.addAll(Arrays.asList("--locale=C", "--locale-provider=builtin", "--builtin-locale=C.UTF-8"));
        } else {
            cmd.add("--locale=C");
        }
        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.redirectErrorStream(true);
        pgEnv(pb, files, nativeDir);
        log("initdb: " + cmd);
        Process p = pb.start();
        pump(p.getInputStream(), "initdb", true);
        if (!p.waitFor(300, TimeUnit.SECONDS)) {
            p.destroyForcibly();
            return -1;
        }
        return p.exitValue();
    }

    private void pgSupervisor(File files, File nativeDir) {
        int fails = 0;
        while (!stopping) {
            if (portOpen(PG_PORT)) {          // postmaster vivo (huerfano de una sesion anterior)
                pgReady = true;
                sleep(5000);
                continue;
            }
            pgReady = false;
            long t0 = System.currentTimeMillis();
            try {
                Process p = launchPostgres(files, nativeDir);
                int rc = p.waitFor();
                log("postgres termino rc=" + rc);
            } catch (Throwable t) {
                log("no pude lanzar postgres: " + t);
            }
            pgReady = false;
            fails = (System.currentTimeMillis() - t0 > 60000) ? 0 : fails + 1;
            if (!stopping) {
                setStatus("PostgreSQL caido; reintentando (" + fails + ")");
                sleep(Math.min(60000, 3000L * (fails + 1)));
            }
        }
    }

    private Process launchPostgres(File files, File nativeDir) throws IOException {
        File pgdata = new File(files, "pgdata");
        File pid = new File(pgdata, "postmaster.pid");
        if (pid.exists()) {
            log("borrando postmaster.pid viejo");
            pid.delete();
        }
        // Solo red: TCP (los sockets Unix estan prohibidos por SELinux). Acceso: loopback,
        // Tailscale (100.64.0.0/10, fd7a:115c:a1e0::/48) y LAN privada. Siempre con password.
        writeFile(new File(pgdata, "pg_hba.conf"),
                "host all all 127.0.0.1/32 scram-sha-256\n"
                        + "host all all ::1/128 scram-sha-256\n"
                        + "host all all 100.64.0.0/10 scram-sha-256\n"
                        + "host all all fd7a:115c:a1e0::/48 scram-sha-256\n"
                        + "host all all 192.168.0.0/16 scram-sha-256\n"
                        + "host all all 10.0.0.0/8 scram-sha-256\n"
                        + "host all all 172.16.0.0/12 scram-sha-256\n");

        List<String> cmd = new ArrayList<>();
        cmd.add(new File(nativeDir, "libpostgres.so").getAbsolutePath());
        cmd.addAll(Arrays.asList(
                "-D", pgdata.getAbsolutePath(),
                "-c", "listen_addresses=*",
                "-c", "port=" + PG_PORT,
                "-c", "unix_socket_directories=",
                "-c", "shared_memory_type=mmap",
                "-c", "dynamic_shared_memory_type=mmap",   // /dev/shm no existe en Android
                "-c", "huge_pages=off",
                "-c", "shared_buffers=64MB",
                "-c", "max_connections=30",
                "-c", "timezone=UTC",
                "-c", "log_timezone=UTC",
                "-c", "log_min_messages=warning",
                "-c", "log_statement=none"));

        // WAL archiving: solo si el directorio es escribible (si no, el WAL se acumularia).
        File wal = new File(NAS_PUBLIC, "pg_wal");
        if ((wal.isDirectory() || wal.mkdirs()) && wal.canWrite()) {
            cmd.addAll(Arrays.asList(
                    "-c", "archive_mode=on",
                    "-c", "archive_timeout=900",
                    "-c", "archive_command=test ! -f '" + wal + "/%f' && cp '%p' '" + wal + "/%f'"));
            log("WAL archiving -> " + wal);
        } else {
            log("WAL archiving DESACTIVADO (sin acceso a " + wal + ")");
        }

        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.redirectErrorStream(true);
        pgEnv(pb, files, nativeDir);
        Process p = pb.start();
        pump(p.getInputStream(), "pg", false);
        return p;
    }

    private void pump(InputStream is, String tag, boolean all) {
        Thread t = new Thread(() -> {
            try (BufferedReader r = new BufferedReader(new InputStreamReader(is))) {
                String line;
                while ((line = r.readLine()) != null) {
                    if (line.contains("ready to accept connections")) {
                        pgReady = true;
                    }
                    if (all || line.contains("ready to accept connections") || line.contains("ERROR")
                            || line.contains("FATAL") || line.contains("PANIC")) {
                        log("[" + tag + "] " + line);
                    }
                }
            } catch (Throwable ignored) {
            }
        }, "c67-pump-" + tag);
        t.setDaemon(true);
        t.start();
    }

    private boolean waitPgReady(int seconds) {
        for (int i = 0; i < seconds; i++) {
            if (pgReady) {
                log("postgres listo tras " + i + "s");
                return true;
            }
            sleep(1000);
        }
        return false;
    }

    // ---------- parada ----------
    private void shutdownAndExit() {
        stopping = true;
        setStatus("deteniendo");
        stopForeground(Service.STOP_FOREGROUND_REMOVE);
        stopSelf();
        new Thread(() -> {
            try {
                File pidFile = new File(getFilesDir(), "pgdata/postmaster.pid");
                if (pidFile.exists()) {
                    try (BufferedReader r = new BufferedReader(new InputStreamReader(
                            Files.newInputStream(pidFile.toPath()), StandardCharsets.UTF_8))) {
                        int pid = Integer.parseInt(r.readLine().trim());
                        android.os.Process.sendSignal(pid, 2);      // SIGINT = apagado rapido
                    }
                    for (int i = 0; i < 30 && portOpen(PG_PORT); i++) {
                        sleep(500);
                    }
                }
            } catch (Throwable t) {
                log("error al detener postgres: " + t);
            }
            started.set(false);
            android.os.Process.killProcess(android.os.Process.myPid());
        }, "c67-stop").start();
    }

    // ---------- utilidades ----------
    private String ensureToken(File files) throws IOException {
        File f = new File(files, "nas_token.txt");
        if (!f.exists()) {
            writeFile(f, randomString(32) + "\n");
            f.setReadable(false, false);
            f.setReadable(true, true);
        }
        return new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8).trim();
    }

    private static String randomString(int n) {
        SecureRandom rnd = new SecureRandom();
        StringBuilder sb = new StringBuilder(n);
        for (int i = 0; i < n; i++) {
            sb.append(ALPHABET.charAt(rnd.nextInt(ALPHABET.length())));
        }
        return sb.toString();
    }

    private static boolean portOpen(int port) {
        try (Socket s = new Socket()) {
            s.connect(new InetSocketAddress("127.0.0.1", port), 500);
            return true;
        } catch (IOException e) {
            return false;
        }
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
        }
    }

    private static void writeFile(File f, String content) throws IOException {
        try (OutputStream o = new FileOutputStream(f)) {
            o.write(content.getBytes(StandardCharsets.UTF_8));
        }
    }

    private static void deleteTree(File f) {
        if (!f.exists() && !Files.isSymbolicLink(f.toPath())) {
            return;
        }
        File[] kids = f.isDirectory() && !Files.isSymbolicLink(f.toPath()) ? f.listFiles() : null;
        if (kids != null) {
            for (File k : kids) {
                deleteTree(k);
            }
        }
        f.delete();
    }

    private static void copyIfDifferent(File src, File dst) throws IOException {
        if (dst.exists() && dst.length() == src.length() && dst.lastModified() >= src.lastModified()) {
            return;
        }
        try (InputStream in = Files.newInputStream(src.toPath()); OutputStream o = new FileOutputStream(dst)) {
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) {
                o.write(buf, 0, n);
            }
        }
        dst.setReadable(true, true);
    }

    /** Copia src -> dst y fuerza permisos de ejecución (Java + chmod). */
    private static void copyBin(File src, File dst) throws IOException {
        try (InputStream in = Files.newInputStream(src.toPath());
             OutputStream o = new FileOutputStream(dst)) {
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) {
                o.write(buf, 0, n);
            }
        }
        dst.setReadable(true, true);
        dst.setExecutable(true, false);   // +x para todos (necesario para linker64)
        // Doble seguro con chmod vía shell (algunos ROMs ignoran setExecutable en /data)
        try {
            Runtime.getRuntime().exec(new String[]{"chmod", "755", dst.getAbsolutePath()}).waitFor();
        } catch (Throwable ignored) {}
    }

    private void extractZip(String asset, File dest) throws IOException {
        String root = dest.getCanonicalPath() + File.separator;
        try (InputStream in = getAssets().open(asset); ZipInputStream z = new ZipInputStream(in)) {
            ZipEntry e;
            byte[] buf = new byte[65536];
            while ((e = z.getNextEntry()) != null) {
                File f = new File(dest, e.getName());
                if (!f.getCanonicalPath().startsWith(root)) {
                    throw new IOException("entrada zip fuera del destino: " + e.getName());
                }
                if (e.isDirectory()) {
                    f.mkdirs();
                    continue;
                }
                f.getParentFile().mkdirs();
                try (FileOutputStream o = new FileOutputStream(f)) {
                    int n;
                    while ((n = z.read(buf)) > 0) {
                        o.write(buf, 0, n);
                    }
                }
            }
        }
    }

    private void launchDockerVm() {
        try {
            log("Iniciando Docker VM (com.tuempresa.dockerphone)...");
            Intent launchIntent = getPackageManager().getLaunchIntentForPackage("com.tuempresa.dockerphone");
            if (launchIntent != null) {
                launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(launchIntent);
                log("Docker VM lanzada exitosamente via getLaunchIntentForPackage");
            } else {
                Intent explicitIntent = new Intent();
                explicitIntent.setClassName("com.tuempresa.dockerphone", "com.tuempresa.dockerphone.MainActivity");
                explicitIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(explicitIntent);
                log("Docker VM lanzada exitosamente via Intent explicito");
            }
        } catch (Throwable t) {
            log("No se pudo autoiniciar Docker VM: " + t.getMessage());
        }
    }

}
