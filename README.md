# C67 DB+NAS — PostgreSQL 18 nativo + API NAS en Android (arm64)

Fork reducido de `luc444s/librefact/android-g5`. Sin systutor-core, sin plugins, sin 1259 productos,
sin `com.g4pg`, sin Termux, sin root. Standalone: no necesita los submódulos de librefact.

## Qué hace
- PostgreSQL 18 (binarios aarch64 de Termux) en `0.0.0.0:5432`, solo TCP, `scram-sha-256`,
  accesible desde loopback, Tailscale (100.64.0.0/10) y LAN privada.
- API NAS (Python stdlib) en `:8000`: interfaz web `/` y `/api/health`, `/api/nas/{list,download,upload,mkdir,delete}` sobre
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
La app Android incluye pantallas de estado del servicio, explorador NAS (listar, crear carpetas,
subir, descargar y borrar archivos), datos de conexión PostgreSQL y ajustes de permisos/servicio.
Usa componentes nativos y el selector de archivos de Android; conserva el mínimo Android 8 y el
soporte arm64. Para administrar desde otro dispositivo sigue disponible la API HTTP.

La contraseña inicial de Postgres y el token NAS se generan en el teléfono; los datos de conexión
se pueden copiar desde la pantalla PostgreSQL. La carpeta compartida sigue siendo
`/storage/emulated/0/NAS` cuando se otorga acceso a todos los archivos.

Desde una PC conectada a la misma LAN privada o a Tailscale, abre `http://<IP-del-telefono>:8000/`
e inicia sesión con el token NAS mostrado por la app. La interfaz permite navegar, crear carpetas,
subir, descargar y eliminar archivos; el servidor sigue aplicando su lista de redes permitidas.
El token no se añade a URLs: tras iniciar sesión, el navegador recibe una cookie de sesión `HttpOnly`
con vencimiento de siete días. La conexión es HTTP sin cifrado: úsala solo en una red confiable,
preferiblemente Tailscale, y no la publiques directamente en Internet.

Las subidas aceptan hasta **1 GiB** por defecto y reservan **64 MiB** de espacio libre antes de
empezar. Se pueden ajustar con `C67_MAX_UPLOAD_BYTES` y `C67_MIN_FREE_BYTES`. Los errores de API
usan JSON `{ "error": { "code": "...", "message": "..." } }`; entre los códigos HTTP están 400
(solicitud inválida), 401/403 (autenticación/red), 404 (no existe), 409 (conflicto), 411 (falta
Content-Length), 413 (límite excedido) y 507 (espacio insuficiente). Los temporales se borran si una
subida falla. Las escrituras se serializan: con `overwrite=0` solo una subida concurrente al mismo
nombre puede crearlo; sin esa opción, la última subida completada reemplaza el archivo.
Una subida sin datos durante 60 segundos expira y limpia su temporal.
Los eventos y fallos se guardan en `files/nas-api.log`, con rotación de 512 KiB y tres copias, sin
registrar tokens ni contenido.
```
psql -h 100.x.y.z -U postgres        # o DBeaver
curl -H "Authorization: Bearer TOKEN" http://100.x.y.z:8000/api/nas/list
curl -X PUT -H "Authorization: Bearer TOKEN" --data-binary @foto.jpg "http://100.x.y.z:8000/api/nas/upload?path=fotos/foto.jpg"
```
Cambiar password: `ALTER USER postgres PASSWORD '...';` (la app solo muestra la inicial).

## Estado de verificación (v1.0.0 — VERIFICADO Y OPERATIVO)
✅ **Probado y verificado en dispositivo físico real (Realme C67 - Snapdragon 685 - Android 14):**
- **G0 (Exec):** Binarios arm64-v8a validados, `initdb` vía `/system/bin/linker64` y `postgres` symlinked a `nativeLibraryDir` (bypass W^X de SELinux).
- **G1 (Initdb):** Cluster inicializado con éxito (`builtin` locale C.UTF-8, checksums activados, `shared_memory_type=mmap`).
- **G2 (Puertos):** PostgreSQL escuchando en `0.0.0.0:5432` con `scram-sha-256`. Comprobada conexión TCP remota.
- **G3 (NAS API):** HTTP API escuchando en `0.0.0.0:8000`. Verificados endpoints `/api/health`, `/api/nas/list`, `upload` y `download`.
- **G4 (Persistencia):** Foreground service persistente con WakeLock y recuperación tras reinicio.
