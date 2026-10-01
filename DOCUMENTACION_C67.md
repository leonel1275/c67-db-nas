# Documentación Técnica y Operativa: C67 DB+NAS

Sistema embebido de base de datos **PostgreSQL 18.6** y **NAS HTTP API** ejecutándose de forma nativa en un dispositivo **Realme C67 (Snapdragon 685 - arm64-v8a)** bajo Android 14.

---

## 1. Resumen Ejecutivo

El proyecto transforma un smartphone Realme C67 en un micro-servidor de alta eficiencia para red local (o VPN Tailscale), proporcionando:
- **Base de Datos Relacional Completa:** PostgreSQL versión 18.6 nativa compilada para arquitectura `arm64-v8a`.
- **Servidor NAS HTTP:** API REST ligera en Python 3.12 (mediante Chaquopy) para almacenamiento, subida, descarga y gestión de archivos.
- **Servicio en Primer Plano (Foreground Service):** Servicio Android persistente (`PgService`) con notificación persistente y gestión de supervisión automática (reinicio si el proceso cae).

---

## 2. Arquitectura del Sistema

```mermaid
flowchart TD
    subgraph Android_OS ["Realme C67 (Android 14 / SELinux Enforcing)"]
        FS["PgService (Foreground Service)"]
        
        subgraph NativeDir ["nativeLibraryDir (/data/app/.../lib/arm64) [apk_data_file]"]
            LIB_PG["libpostgres.so (Binario ejecutable)"]
            LIB_INIT["libinitdb.so (Binario ejecutable)"]
            LIB_SHMEM["libandroid-shmem.so (Shim memfd_create)"]
            LIB_OTHERS["libicu, libpq, libxml2, etc."]
        end
        
        subgraph FilesDir ["app_data_file (/data/user/0/com.c67.pg/files/)"]
            TMP["tmp/ (TMPDIR + shmem metadata)"]
            BIN_INIT["usr/bin/initdb (Copia real con +x)"]
            BIN_PG["usr/bin/postgres (SYMLINK -> libpostgres.so)"]
            PGDATA["pgdata/ (Cluster de BD)"]
            CREDS["pg_password.txt / nas_token.txt"]
        end
        
        subgraph ChaquopyPython ["Chaquopy Python Runtime"]
            PY_API["c67core.py (HTTP Server :8000)"]
        end
        
        subgraph Storage ["Almacenamiento (/storage/emulated/0/)"]
            NAS_STORE["NAS Storage (/Android/data/com.c67.pg/files/NAS)"]
        end
    end
    
    FS -->|"1. Prepara binarios & symlinks"| FilesDir
    FS -->|"2. Ejecuta /system/bin/linker64"| BIN_INIT
    BIN_INIT -.->|"valida vía sh -c 'postgres -V'"| BIN_PG
    BIN_PG -->|"Kernel resuelve symlink a"| LIB_PG
    FS -->|"3. Supervisa libpostgres.so :5432"| LIB_PG
    FS -->|"4. Inicia Chaquopy HTTP :8000"| PY_API
    PY_API -->|"Gestiona archivos"| NAS_STORE
```

### Componentes Clave

1. **`PgService.java`**:
   - Gestiona el ciclo de vida del servicio Android, extracción de `pgsupport.zip`, configuración del clúster `initdb` y supervisor del daemon `postgres`.
2. **`shmem/android_shmem.c`**:
   - Reemplazo para `libandroid-shmem` de Termux. Emplea la syscall `memfd_create` en lugar de `/dev/ashmem` (deprecado/eliminado en Android 11+), aislando los descriptores y evitando dependencias conflictivas con OpenSSL o Android NDK `ASharedMemory`.
3. **`c67core.py`**:
   - Servidor HTTP multi-hilo minimalista basado únicamente en la librería estándar de Python (`http.server`).
   - Autenticación con Bearer Token, filtrado estricto de rangos IP (Loopback, Tailscale, redes privadas LAN RFC 1918).

---

## 3. Diagnóstico Profundo y Resolución de Problemas (SELinux & W^X)

### El Problema Inicial
Al instalar la app y ejecutar `initdb` en el dispositivo físico, el logcat mostraba:
```text
[initdb] sh: /data/data/com.c67.pg/files/usr/bin/postgres: Permission denied
[initdb] no data was returned by command ""/data/data/com.c67.pg/files/usr/bin/postgres" -V"
[initdb] command not executable
[initdb] initdb: error: program "postgres" is needed by initdb but was not found in the same directory as "/data/data/com.c67.pg/files/usr/bin/initdb"
FATAL java.io.IOException: initdb fallo rc=1
```

### Anatomía del Fallo
1. **Verificación interna de PostgreSQL (`find_other_exec`):**
   `initdb` necesita verificar que su binario hermano `postgres` existe y funciona. Para ello llama a `popen("\"/path/to/postgres\" -V")`, lo cual delega en `/system/bin/sh -c ...`.
2. **Restricción W^X (Write XOR Execute) en Android 10+ (API 29+):**
   La política SELinux de Android impone `neverallow untrusted_app app_data_file:file { execute execute_no_trans }`.
   Cualquier llamada directa a `execve()` sobre archivos en `/data/data/...` o `/data/user/0/...` es rechazada por el kernel con `EACCES` (`Permission denied`), incluso teniendo permisos Unix `0755` (`chmod +x`).
3. **Comportamiento con Linker64 vs Execve directo:**
   - `/system/bin/linker64 /path/to/binary` funciona porque `execve` ejecuta el linker del sistema (permitido), y este hace `open()` + `mmap(PROT_EXEC)` del binario.
   - Sin embargo, cuando `sh` intenta correr `postgres -V`, no usa `linker64`, sino `execve()` directo.

### La Solución Definitiva (Doble Estrategia)

| Binario | Estrategia de Ubicación | Justificación Técnica |
|---|---|---|
| **`initdb`** | **Copia real con `chmod 755`** (`usr/bin/initdb`) | Se invoca mediante `/system/bin/linker64`. `initdb` ejecuta `access(argv[0], X_OK)` para validarse a sí mismo; requiere el bit `+x` activo. |
| **`postgres`** | **Symlink apuntando a `libpostgres.so`** (`usr/bin/postgres -> nativeLibraryDir/libpostgres.so`) | Cuando `/system/bin/sh` hace `execve()` sobre el symlink, el kernel de Linux resuelve el inodo en `nativeLibraryDir` (`apk_data_file`), donde SELinux **SÍ** autoriza ejecución directa. |
| **`TMPDIR`** | **Ruta interna `/data/user/0/com.c67.pg/files/tmp`** | El shim de memoria compartida (`libandroid-shmem`) crea los archivos de sincronización ahí. `/data/local/tmp` está restringido para apps normales. |

---

## 4. Pipeline CI/CD en GitHub Actions

- **Repositorio:** [leonel1275/c67-db-nas](https://github.com/leonel1275/c67-db-nas) (Privado)
- **Workflow:** `.github/workflows/build-apk.yml`
- **Cadena de Compilación:**
  1. Java 17 + Python 3.12 + Android NDK r27d.
  2. Scripts bash: descarga paquetes Debian Termux (`postgresql 18.6`, `libpq`, `libicu`, `libxml2`, etc.).
  3. Parche in-place de rutas de 10 caracteres: `/data/data/com.termux` ➡️ `/data/data/com.c67.pg`.
  4. Compilación del shim de memoria compartida con Clang NDK (`scripts/build-shmem-fix.sh`).
  5. Gradle build con Chaquopy (`./gradlew assembleDebug`).
  6. Publicación del artefacto `c67-db-nas-apk` (`app-debug.apk`).

---

## 5. Parámetros de Operación y Credenciales en Vivo

Estado operativo confirmado en el dispositivo **Realme C67**:

### Conexión de Red
- **Dirección IP Wi-Fi:** `192.168.0.229` (Subred `192.168.0.0/24`)
- **Máscara de red:** `255.255.255.0`

### PostgreSQL
- **Host:** `192.168.0.229`
- **Puerto:** `5432`
- **Usuario:** `postgres`
- **Base de datos:** `postgres`
- **Contraseña:** `hEg3xuRjv4yk4wesBT97hvxt`
- **Método de Autenticación:** `SCRAM-SHA-256`
- **Shared Memory:** `mmap` (Dynamic Shared Memory: `mmap`)
- **Rango permitido en `pg_hba.conf`:** `127.0.0.1/32`, `100.64.0.0/10` (Tailscale), `192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`.

### Servidor NAS HTTP API
- **Host:** `http://192.168.0.229:8000`
- **Bearer Token:** `k7EYed9pZZxCzybRikD4vaZC9fH6aYK5`
- **Ruta física en dispositivo:** `/storage/emulated/0/Android/data/com.c67.pg/files/NAS`

---

## 6. Manual de Comandos de Prueba y Operación

### 6.1. Verificación de Conectividad desde PC

**Verificar puerto PostgreSQL (PowerShell):**
```powershell
Test-NetConnection -ComputerName 192.168.0.229 -Port 5432
# Salida esperada: TcpTestSucceeded : True
```

**Verificar API Health (PowerShell / cURL):**
```powershell
Invoke-RestMethod -Uri "http://192.168.0.229:8000/api/health"
```
*Respuesta:*
```json
{
  "status": "ok",
  "postgres": true,
  "nas_dir": "/storage/emulated/0/Android/data/com.c67.pg/files/NAS",
  "nas_writable": true,
  "uptime_s": 120
}
```

---

### 6.2. Conexión a PostgreSQL con `psql`

**Desde terminal bash/zsh:**
```bash
PGPASSWORD="hEg3xuRjv4yk4wesBT97hvxt" psql -h 192.168.0.229 -p 5432 -U postgres -d postgres
```

**Desde PowerShell:**
```powershell
$env:PGPASSWORD = "hEg3xuRjv4yk4wesBT97hvxt"
psql -h 192.168.0.229 -p 5432 -U postgres -d postgres
```

**Ejemplo de consulta SQL:**
```sql
SELECT version();
CREATE TABLE sensores (id SERIAL PRIMARY KEY, nombre TEXT, valor NUMERIC, fecha TIMESTAMP DEFAULT NOW());
INSERT INTO sensores (nombre, valor) VALUES ('temperatura_sala', 24.5);
SELECT * FROM sensores;
```

---

### 6.3. Operaciones en la API del NAS

#### 1. Listar Archivos
```powershell
curl.exe -s -H "Authorization: Bearer k7EYed9pZZxCzybRikD4vaZC9fH6aYK5" "http://192.168.0.229:8000/api/nas/list"
```

#### 2. Subir Archivo (Upload)
```powershell
curl.exe -s -X PUT "http://192.168.0.229:8000/api/nas/upload?path=reportes/datos.csv" `
  -H "Authorization: Bearer k7EYed9pZZxCzybRikD4vaZC9fH6aYK5" `
  -H "Content-Type: text/plain" `
  --data-raw "id,sensor,lectura`n1,presion,1013`n2,humedad,65"
```

#### 3. Descargar Archivo (Download)
```powershell
curl.exe -s "http://192.168.0.229:8000/api/nas/download?path=reportes/datos.csv" `
  -H "Authorization: Bearer k7EYed9pZZxCzybRikD4vaZC9fH6aYK5" `
  -o "datos_descargados.csv"
```

#### 4. Eliminar Archivo
```powershell
curl.exe -s -X DELETE "http://192.168.0.229:8000/api/nas/delete?path=reportes/datos.csv" `
  -H "Authorization: Bearer k7EYed9pZZxCzybRikD4vaZC9fH6aYK5"
```

---

### 6.4. Comandos de Gestión y Diagnóstico por ADB

**Ver logs del servicio en tiempo real:**
```powershell
adb logcat -s G5:*
```

**Reiniciar la aplicación:**
```powershell
adb shell am force-stop com.c67.pg
adb shell am start -n com.c67.pg/com.systutor.g5.MainActivity
```

**Consultar credenciales directamente del teléfono:**
```powershell
adb shell run-as com.c67.pg cat /data/user/0/com.c67.pg/files/pg_password.txt
adb shell run-as com.c67.pg cat /data/user/0/com.c67.pg/files/nas_token.txt
```

---

## 7. Mantenimiento y Buenas Prácticas

1. **Exclusión de Ahorro de Batería:**
   En el Realme C67, asegurarse de otorgar *Ajustes ➡️ Batería ➡️ Más ajustes ➡️ Optimizar uso de la batería ➡️ C67 DB+NAS ➡️ No optimizar* y permitir ejecución en segundo plano para evitar que ColorOS/Android congele el servicio al bloquear la pantalla.
2. **Persistencia de Datos:**
   Los datos de la base de datos residen en `/data/user/0/com.c67.pg/files/pgdata`. Se mantienen intactos entre reinicios del teléfono. Solo se eliminarían si el usuario pulsa *"Borrar datos"* en la configuración del sistema.
3. **Acceso a Almacenamiento NAS:**
   Si se requiere que la carpeta del NAS sea visible directamente en el explorador de archivos del teléfono (`/storage/emulated/0/NAS`), otorgar el permiso de sistema *"Acceso a todos los archivos"* (`MANAGE_EXTERNAL_STORAGE`).
