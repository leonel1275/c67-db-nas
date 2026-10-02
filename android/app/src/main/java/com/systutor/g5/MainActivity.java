package com.systutor.g5;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.OpenableColumns;
import android.provider.Settings;
import android.graphics.Typeface;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Collections;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Panel nativo para el estado, los archivos NAS y la conexión PostgreSQL. */
public class MainActivity extends Activity {
    private static final int PICK_UPLOAD = 10;
    private static final int PICK_DOWNLOAD = 11;
    private static final String API = "http://127.0.0.1:" + PgService.API_PORT;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (screen == 0 && statusText != null) {
                statusText.setText(statusSummary());
            }
            handler.postDelayed(this, 2000);
        }
    };

    private LinearLayout content;
    private TextView statusText;
    private int screen;
    private String currentPath = "";
    private String pendingDownloadName = "download";
    private String healthSummary = "Estado API: consultando";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        int pad = dp(12);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, 0);

        LinearLayout nav = new LinearLayout(this);
        nav.setOrientation(LinearLayout.HORIZONTAL);
        addNav(nav, "Estado", 0);
        addNav(nav, "Archivos", 1);
        addNav(nav, "PostgreSQL", 2);
        addNav(nav, "Ajustes", 3);
        root.addView(nav);

        FrameLayout frame = new FrameLayout(this);
        content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(0, dp(8), 0, dp(12));
        ScrollView scroll = new ScrollView(this);
        scroll.addView(content);
        frame.addView(scroll);
        root.addView(frame, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1));
        setContentView(root);

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        startForegroundService(new Intent(this, PgService.class));
        showScreen(0);
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

    @Override
    protected void onDestroy() {
        super.onDestroy();
        io.shutdown();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        Uri uri = data.getData();
        if (requestCode == PICK_UPLOAD) {
            upload(uri);
        } else if (requestCode == PICK_DOWNLOAD) {
            download(uri);
        }
    }

    private void addNav(LinearLayout nav, String title, int target) {
        Button b = button(title, v -> showScreen(target));
        nav.addView(b, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1));
    }

    private void showScreen(int target) {
        screen = target;
        content.removeAllViews();
        statusText = null;
        if (target == 0) {
            showStatus();
        } else if (target == 1) {
            showFiles();
        } else if (target == 2) {
            showDatabase();
        } else {
            showSettings();
        }
    }

    private void showStatus() {
        title("Estado del servidor");
        statusText = text(statusSummary());
        statusText.setTypeface(Typeface.MONOSPACE);
        content.addView(statusText);
        content.addView(button("Actualizar estado", v -> refreshHealth()));
        refreshHealth();
    }

    private String statusSummary() {
        return "Servicio: " + PgService.status() + "\n\n"
                + "IPs del telefono (Tailscale si esta activo):\n" + ips()
                + "\nPostgreSQL: puerto " + PgService.PG_PORT
                + "\nNAS API: http://<ip>:" + PgService.API_PORT
                + "\nCarpeta: " + PgService.NAS_PUBLIC + "\n\n"
                + healthSummary + "\n\nLog reciente:\n" + tail(PgService.logTail(), 1200);
    }

    private void refreshHealth() {
        io.execute(() -> {
            try {
                JSONObject health = api("GET", "/api/health", null, -1);
                String message = "API: " + health.optString("status", "sin respuesta")
                        + "\nPostgreSQL: " + (health.optBoolean("postgres") ? "activo" : "no responde")
                        + "\nAlmacenamiento NAS: " + (health.optBoolean("nas_writable") ? "escribible" : "solo lectura")
                        + "\nAntiguedad del servicio: " + health.optLong("uptime_s") + " s";
                runOnUiThread(() -> {
                    healthSummary = message;
                    if (screen == 0) {
                        statusText.setText(statusSummary());
                    }
                });
            } catch (Exception e) {
                runOnUiThread(() -> {
                    healthSummary = "Estado API: error de conexion (" + e.getMessage() + ")";
                    if (screen == 0) {
                        statusText.setText(statusSummary());
                    }
                });
            }
        });
    }

    private void showFiles() {
        title("Archivos NAS");
        TextView location = text("Carpeta: /" + currentPath);
        content.addView(location);
        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        actions.addView(button("Subir", v -> pickUpload()), weighted());
        actions.addView(button("Nueva carpeta", v -> createFolder()), weighted());
        actions.addView(button("Recargar", v -> listFiles()), weighted());
        content.addView(actions);
        if (!currentPath.isEmpty()) {
            content.addView(button("← Carpeta superior", v -> {
                int slash = currentPath.lastIndexOf('/');
                currentPath = slash < 0 ? "" : currentPath.substring(0, slash);
                showScreen(1);
            }));
        }
        content.addView(text("Toca una carpeta para entrar o un archivo para descargar."));
        listFiles();
    }

    private void listFiles() {
        String requestedPath = currentPath;
        io.execute(() -> {
            try {
                JSONArray items = new JSONArray();
                int offset = 0;
                boolean hasMore;
                do {
                    JSONObject result = api("GET", "/api/nas/list?path=" + enc(requestedPath)
                            + "&limit=500&offset=" + offset, null, -1);
                    JSONArray page = result.getJSONArray("items");
                    for (int i = 0; i < page.length(); i++) {
                        items.put(page.get(i));
                    }
                    offset += page.length();
                    hasMore = result.optBoolean("has_more");
                } while (hasMore);
                runOnUiThread(() -> {
                    if (screen != 1 || !requestedPath.equals(currentPath)) {
                        return;
                    }
                    int fixedViews = requestedPath.isEmpty() ? 4 : 5;
                    while (content.getChildCount() > fixedViews) {
                        content.removeViewAt(fixedViews);
                    }
                    if (items.length() == 0) {
                        content.addView(text("Esta carpeta esta vacia."));
                    }
                    for (int i = 0; i < items.length(); i++) {
                        JSONObject item = items.optJSONObject(i);
                        if (item != null) {
                            addFileRow(item);
                        }
                    }
                });
            } catch (Exception e) {
                runOnUiThread(() -> toast("Error al listar archivos: " + e.getMessage()));
            }
        });
    }

    private void addFileRow(JSONObject item) {
        String name = item.optString("name");
        boolean directory = item.optBoolean("is_dir");
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        String label = (directory ? "📁  " : "📄  ") + name
                + (directory ? "" : "  (" + item.optLong("size") + " bytes)");
        Button open = button(label, v -> {
            if (directory) {
                currentPath = currentPath.isEmpty() ? name : currentPath + "/" + name;
                showScreen(1);
            } else {
                pendingDownloadName = name;
                Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("application/octet-stream");
                intent.putExtra(Intent.EXTRA_TITLE, name);
                startActivityForResult(intent, PICK_DOWNLOAD);
            }
        });
        row.addView(open, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        row.addView(button("Borrar", v -> confirmDelete(name, directory)));
        content.addView(row);
    }

    private void createFolder() {
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setHint("Nombre de carpeta");
        new AlertDialog.Builder(this).setTitle("Crear carpeta")
                .setView(input)
                .setNegativeButton("Cancelar", null)
                .setPositiveButton("Crear", (dialog, which) -> {
                    String name = input.getText().toString().trim();
                    if (name.isEmpty() || name.contains("/") || name.equals(".") || name.equals("..")) {
                        toast("Nombre de carpeta no valido");
                        return;
                    }
                    String path = currentPath.isEmpty() ? name : currentPath + "/" + name;
                    runApiAction("POST", "/api/nas/mkdir?path=" + enc(path), null, -1,
                            "Carpeta creada", this::listFiles);
                }).show();
    }

    private void confirmDelete(String name, boolean directory) {
        String path = currentPath.isEmpty() ? name : currentPath + "/" + name;
        new AlertDialog.Builder(this)
                .setTitle(directory ? "Eliminar carpeta" : "Eliminar archivo")
                .setMessage(directory ? "Solo se pueden borrar carpetas vacias. ¿Continuar?"
                        : "Se eliminara " + name + ". ¿Continuar?")
                .setNegativeButton("Cancelar", null)
                .setPositiveButton("Eliminar", (dialog, which) ->
                        runApiAction("DELETE", "/api/nas/delete?path=" + enc(path), null, -1,
                                "Elemento eliminado", this::listFiles))
                .show();
    }

    private void pickUpload() {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("*/*");
        startActivityForResult(intent, PICK_UPLOAD);
    }

    private void upload(Uri uri) {
        String name = displayName(uri);
        if (name == null || name.isEmpty() || name.contains("/") || name.equals(".") || name.equals("..")) {
            toast("No se pudo obtener un nombre de archivo valido");
            return;
        }
        long size = contentLength(uri);
        if (size < 0) {
            toast("El proveedor de archivos no informa el tamano; no se puede subir este archivo.");
            return;
        }
        String path = currentPath.isEmpty() ? name : currentPath + "/" + name;
        runApiAction("PUT", "/api/nas/upload?path=" + enc(path), uri, size,
                "Archivo subido", this::listFiles);
    }

    private void download(Uri destination) {
        String path = currentPath.isEmpty() ? pendingDownloadName : currentPath + "/" + pendingDownloadName;
        io.execute(() -> {
            try {
                HttpURLConnection connection = connection("/api/nas/download?path=" + enc(path), "GET");
                connection.connect();
                requireSuccess(connection);
                try (InputStream in = connection.getInputStream();
                     OutputStream out = getContentResolver().openOutputStream(destination)) {
                    if (out == null) {
                        throw new IllegalStateException("No se pudo abrir el destino");
                    }
                    byte[] buffer = new byte[32768];
                    int count;
                    while ((count = in.read(buffer)) != -1) {
                        out.write(buffer, 0, count);
                    }
                } finally {
                    connection.disconnect();
                }
                runOnUiThread(() -> toast("Descarga guardada"));
            } catch (Exception e) {
                runOnUiThread(() -> toast("Error al descargar: " + e.getMessage()));
            }
        });
    }

    private void showDatabase() {
        title("Conexion PostgreSQL");
        String host = firstLanIp();
        String details = "Host: " + host + "\nPuerto: " + PgService.PG_PORT
                + "\nUsuario: postgres\nBase de datos: postgres\n"
                + "Seguridad: SCRAM-SHA-256\n"
                + "Redes admitidas: LAN privada y Tailscale";
        content.addView(text(details));
        content.addView(button("Copiar direccion de conexion", v ->
                copy("PostgreSQL", host + ":" + PgService.PG_PORT)));
        content.addView(button("Copiar usuario", v -> copy("Usuario PostgreSQL", "postgres")));
        content.addView(button("Copiar contraseña", v -> {
            String password = read("pg_password.txt");
            if (password.startsWith("(")) {
                toast("La contraseña aun no esta disponible");
            } else {
                copy("Contraseña PostgreSQL", password);
            }
        }));
        content.addView(text("La aplicacion solo muestra la contraseña inicial. Si la cambias con SQL, guarda la nueva en un lugar seguro."));
    }

    private void showSettings() {
        title("Ajustes y mantenimiento");
        boolean storage = Build.VERSION.SDK_INT < 30 || Environment.isExternalStorageManager();
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        boolean battery = pm.isIgnoringBatteryOptimizations(getPackageName());
        content.addView(text("Acceso a todos los archivos: " + (storage ? "permitido" : "no permitido")));
        content.addView(button("Configurar acceso a archivos", v -> requestStorage()));
        content.addView(text("Exento de optimizacion de bateria: " + (battery ? "si" : "no")));
        content.addView(button("Configurar bateria", v -> requestBattery()));
        content.addView(button("Detener PostgreSQL y NAS", v ->
                startService(new Intent(this, PgService.class).setAction(PgService.ACTION_STOP))));
        content.addView(text("La carpeta NAS es " + PgService.NAS_PUBLIC
                + ". Sin el permiso de almacenamiento, el servicio usa la carpeta privada de la app."));
    }

    private void runApiAction(String method, String path, Uri body, long length,
                              String success, Runnable after) {
        io.execute(() -> {
            try {
                api(method, path, body, length);
                runOnUiThread(() -> {
                    toast(success);
                    after.run();
                });
            } catch (Exception e) {
                runOnUiThread(() -> toast("Error: " + e.getMessage()));
            }
        });
    }

    private JSONObject api(String method, String path, Uri body, long length) throws Exception {
        HttpURLConnection connection = connection(path, method);
        try {
            if (body != null) {
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/octet-stream");
                connection.setFixedLengthStreamingMode(length);
                try (InputStream in = getContentResolver().openInputStream(body);
                     OutputStream out = connection.getOutputStream()) {
                    if (in == null) {
                        throw new IllegalStateException("No se pudo leer el archivo seleccionado");
                    }
                    byte[] buffer = new byte[32768];
                    int count;
                    long remaining = length;
                    while (remaining > 0 && (count = in.read(buffer, 0, (int) Math.min(buffer.length, remaining))) != -1) {
                        out.write(buffer, 0, count);
                        remaining -= count;
                    }
                    if (remaining != 0) {
                        throw new IllegalStateException("El archivo cambio mientras se subia");
                    }
                }
            }
            requireSuccess(connection);
            try (InputStream in = connection.getInputStream()) {
                byte[] response = readAll(in);
                return response.length == 0 ? new JSONObject() : new JSONObject(new String(response, StandardCharsets.UTF_8));
            }
        } finally {
            connection.disconnect();
        }
    }

    private HttpURLConnection connection(String path, String method) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(API + path).openConnection();
        connection.setRequestMethod(method);
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(30000);
        connection.setRequestProperty("Authorization", "Bearer " + read("nas_token.txt"));
        return connection;
    }

    private void requireSuccess(HttpURLConnection connection) throws Exception {
        int code = connection.getResponseCode();
        if (code < 200 || code >= 300) {
            InputStream error = connection.getErrorStream();
            String detail = error == null ? "" : new String(readAll(error), StandardCharsets.UTF_8);
            throw new IllegalStateException("HTTP " + code + (detail.isEmpty() ? "" : ": " + detail));
        }
    }

    private byte[] readAll(InputStream in) throws Exception {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        int count;
        while ((count = in.read(buffer)) != -1) {
            out.write(buffer, 0, count);
        }
        return out.toByteArray();
    }

    private String displayName(Uri uri) {
        try (Cursor cursor = getContentResolver().query(uri,
                new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) {
                return cursor.getString(0);
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    private long contentLength(Uri uri) {
        try (Cursor cursor = getContentResolver().query(uri,
                new String[]{OpenableColumns.SIZE}, null, null, null)) {
            if (cursor != null && cursor.moveToFirst() && !cursor.isNull(0)) {
                return cursor.getLong(0);
            }
        } catch (Exception ignored) {
        }
        return -1;
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
            return "";
        }
    }

    private String ips() {
        StringBuilder result = new StringBuilder();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) {
                    continue;
                }
                for (InetAddress address : Collections.list(ni.getInetAddresses())) {
                    if (address instanceof Inet4Address) {
                        result.append("  ").append(ni.getName()).append("  ")
                                .append(address.getHostAddress()).append('\n');
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return result.length() == 0 ? "  (sin red)\n" : result.toString();
    }

    private String firstLanIp() {
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (ni.isUp() && !ni.isLoopback()) {
                    for (InetAddress address : Collections.list(ni.getInetAddresses())) {
                        if (address instanceof Inet4Address) {
                            return address.getHostAddress();
                        }
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return "<IP del telefono>";
    }

    private void copy(String label, String value) {
        ClipboardManager clipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        clipboard.setPrimaryClip(ClipData.newPlainText(label, value));
        toast(label + " copiado");
    }

    private String enc(String value) {
        return Uri.encode(value, "/");
    }

    private LinearLayout.LayoutParams weighted() {
        return new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1);
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private void title(String value) {
        TextView title = text(value);
        title.setTextSize(21);
        content.addView(title);
    }

    private TextView text(String value) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(15);
        view.setPadding(0, dp(8), 0, dp(8));
        return view;
    }

    private Button button(String label, View.OnClickListener listener) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setOnClickListener(listener);
        return button;
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
    }

    private static String tail(String value, int max) {
        return value.length() <= max ? value : value.substring(value.length() - max);
    }
}
