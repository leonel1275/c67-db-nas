package com.systutor.g5;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.graphics.Typeface;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.File;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Collections;

/** Panel de estado: arranca el servicio, muestra IPs/credenciales y pide los permisos necesarios. */
public class MainActivity extends Activity {
    private TextView tv;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            tv.setText(render());
            handler.postDelayed(this, 2000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        int pad = (int) (12 * getResources().getDisplayMetrics().density);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad * 2, pad, pad);

        root.addView(button("1. Permiso: acceso a todos los archivos (NAS)", v -> requestStorage()));
        root.addView(button("2. Excluir de optimizacion de bateria", v -> requestBattery()));
        root.addView(button("Detener todo (PostgreSQL + NAS)", v ->
                startService(new Intent(this, PgService.class).setAction(PgService.ACTION_STOP))));

        tv = new TextView(this);
        tv.setTypeface(Typeface.MONOSPACE);
        tv.setTextSize(11f);
        tv.setTextIsSelectable(true);
        tv.setPadding(0, pad, 0, 0);
        ScrollView sv = new ScrollView(this);
        sv.addView(tv);
        root.addView(sv);
        setContentView(root);

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        startForegroundService(new Intent(this, PgService.class));
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(tick);
    }

    @Override
    protected void onPause() {
        super.onPause();
        handler.removeCallbacks(tick);
    }

    private Button button(String label, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setOnClickListener(l);
        return b;
    }

    private void requestStorage() {
        if (Build.VERSION.SDK_INT >= 30) {
            startActivity(new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                    Uri.parse("package:" + getPackageName())));
        } else {
            requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, 2);
        }
    }

    private void requestBattery() {
        startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:" + getPackageName())));
    }

    private String read(String name) {
        try {
            return new String(Files.readAllBytes(new File(getFilesDir(), name).toPath()),
                    StandardCharsets.UTF_8).trim();
        } catch (Exception e) {
            return "(aun no generado)";
        }
    }

    private String ips() {
        StringBuilder sb = new StringBuilder();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) {
                    continue;
                }
                for (InetAddress a : Collections.list(ni.getInetAddresses())) {
                    if (a instanceof Inet4Address) {
                        sb.append("  ").append(ni.getName()).append("  ").append(a.getHostAddress()).append('\n');
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return sb.length() == 0 ? "  (sin red)\n" : sb.toString();
    }

    private String render() {
        boolean storage = Build.VERSION.SDK_INT < 30 || Environment.isExternalStorageManager();
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        boolean battery = pm.isIgnoringBatteryOptimizations(getPackageName());
        return "Estado: " + PgService.status() + "\n\n"
                + "IPs del telefono (Tailscale = tun/tailscale0, 100.x):\n" + ips() + "\n"
                + "PostgreSQL  puerto " + PgService.PG_PORT + "  usuario postgres\n"
                + "  password: " + read("pg_password.txt") + "\n\n"
                + "NAS API  http://<ip>:" + PgService.API_PORT + "/api/health\n"
                + "  token: " + read("nas_token.txt") + "\n"
                + "  carpeta: " + PgService.NAS_PUBLIC + "\n\n"
                + "Acceso a todos los archivos: " + (storage ? "SI" : "NO (toca el boton 1; luego Detener y abrir de nuevo)") + "\n"
                + "Exento de bateria: " + (battery ? "SI" : "NO (toca el boton 2)") + "\n\n"
                + "---- log ----\n" + tail(PgService.logTail(), 1800);
    }

    private static String tail(String s, int n) {
        return s.length() <= n ? s : s.substring(s.length() - n);
    }
}
