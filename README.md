# C67 DB+NAS — PostgreSQL 18 nativo + API NAS en Android (arm64)

Fork reducido de `luc444s/librefact/android-g5`. Sin systutor-core, sin plugins, sin 1259 productos,
sin `com.g4pg`, sin Termux, sin root. Standalone: no necesita los submódulos de librefact.

## Qué hace
- PostgreSQL 18 (binarios aarch64 de Termux) en `0.0.0.0:5432`, solo TCP, `scram-sha-256`,
  accesible desde loopback, Tailscale (100.64.0.0/10) y LAN privada.
- API NAS (Python stdlib) en `:8000`: `/api/health`, `/api/nas/{list,download,upload,delete}` sobre
  `/storage/emulated/0/NAS`, con token y lista de redes permitidas.
- Servicio en primer plano (`specialUse`) + WakeLock + `START_STICKY` + arranque en boot.
- WAL archiving a `/storage/emulated/0/NAS/pg_wal` (solo si la carpeta es escribible).

## Cambios vs. el original (y por qué)
| Original | Ahora |
|---|---|
| armeabi-v7a, Python 3.11 | arm64-v8a, Python 3.12 (Chaquopy 17) |
| `com.g5pg` (8 car.) reescribiendo `com.g4pg` | **`com.c67.pg` (10 car.)** = largo de `com.termux`: el parche de ruta de los debs de Termux conserva el largo |
| libs sacadas de `com.g4pg` instalado | `scripts/fetch_termux.py` baja los `.deb` aarch64 de packages.termux.dev |
| libandroid-shmem de Termux (CPU 100 % / EACCES / ashmem) | `shmem/android_shmem.c`: `memfd_create` + `$TMPDIR`, sin libandroid.so |
| 127.0.0.1:54329 + `trust` | 0.0.0.0:5432, `scram-sha-256` + password aleatorio |
| sin servicio | `PgService` + `BootReceiver` |
| FastAPI + pydantic_core (wheel armv7 propio) | `http.server` de la stdlib: **cero wheels nativos** |

## Compilar
**GitHub Actions (recomendado):** subí esta carpeta a un repo → Actions → *build-apk* → *Run workflow* → descargá `app-debug.apk`.

**Local (Linux):** JDK 17, Gradle 8.9, Android SDK 34, NDK r27d, Python 3.12, `readelf`:
```
export ANDROID_HOME=... ANDROID_NDK_HOME=...
scripts/build-c67.sh --install      # build + adb install + abre la app
scripts/verify-g0-g3.sh 100.x.y.z   # G0 exec, G1 initdb, G2 puertos, G3 API
```

## Uso
Al abrir la app ves IPs, password de Postgres (`postgres`) y token NAS. Tocá los botones 1 y 2
(acceso a archivos y exención de batería) y luego *Detener todo* + abrir otra vez.
```
psql -h 100.x.y.z -U postgres        # o DBeaver
curl -H "Authorization: Bearer TOKEN" http://100.x.y.z:8000/api/nas/list
curl -X PUT -H "Authorization: Bearer TOKEN" --data-binary @foto.jpg "http://100.x.y.z:8000/api/nas/upload?path=fotos/foto.jpg"
```
Cambiar password: `ALTER USER postgres PASSWORD '...';` (la app solo muestra la inicial).

## Estado de verificación
Probado en este repo (host Linux, `tests/`): servidor NAS (auth, traversal, symlinks, 5 MB up/down),
lógica del shim de shmem (creador vivo/muerto), `fetch_termux.py` end-to-end con repo falso
(renombrado de libs, parche de ruta, el binario parcheado ejecuta), Java compila contra android.jar API 34,
manifest/YAML bien formados.

**NO probado (requiere dispositivo/CI):** Gradle/Chaquopy, descarga real de Termux, el APK en el C67.
Riesgos a mirar primero si algo falla (el log de la app y `verify-g0-g3.sh` los delatan):
1. `initdb` vía `/system/bin/linker64` (truco para el bloqueo W^X); si el ROM lo bloquea → G1 FAIL.
2. Headers/layout de `struct shmid_ds` del paquete libandroid-shmem de Termux (el build lo avisa).
3. `--builtin-locale` de initdb (hay reintento automático con locale C clásico).
