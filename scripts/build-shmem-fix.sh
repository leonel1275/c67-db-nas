#!/usr/bin/env bash
# Compila libandroid-shmem.so de reemplazo (memfd_create + TMPDIR) con el NDK r27d.
# Corre DESPUES de scripts/fetch_termux.py (usa sus headers para que el layout de
# struct shmid_ds coincida con el que uso postgres al compilarse).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MAN="$ROOT/build/termux/manifest.json"
OUT="$ROOT/android/app/src/main/jniLibs/arm64-v8a"
INC="$ROOT/build/termux/stage/data/data/com.termux/files/usr/include"

if ! python3 -c "import json,sys; sys.exit(0 if 'libandroid-shmem.so' in json.load(open('$MAN'))['libs'] else 1)"; then
  echo "== este PostgreSQL no usa libandroid-shmem.so: nada que reemplazar"; exit 0
fi

NDK="${ANDROID_NDK_HOME:-${ANDROID_NDK_ROOT:-}}"
[ -d "$NDK" ] || { echo "Falta ANDROID_NDK_HOME (NDK r27d)" >&2; exit 1; }
CC="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android26-clang"
[ -x "$CC" ] || { echo "No encuentro $CC" >&2; exit 1; }
[ -f "$INC/sys/shm.h" ] || { echo "Falta $INC/sys/shm.h (headers del paquete libandroid-shmem de Termux)" >&2; exit 1; }

DEFS=()
grep -q 'shm_segsz' "$INC/sys/shm.h" && DEFS+=(-DHAVE_SHM_SEGSZ)
grep -q 'shm_nattch' "$INC/sys/shm.h" && DEFS+=(-DHAVE_SHM_NATTCH)
grep -q 'shm_perm' "$INC/sys/shm.h" && DEFS+=(-DHAVE_SHM_PERM_UID)
echo "== defs: ${DEFS[*]:-(ninguna)}"

ORIG="$OUT/libandroid-shmem.so"
cp "$ORIG" /tmp/libandroid-shmem.termux.so
"$CC" -shared -fPIC -O2 -Wall -Wno-unused-function \
  -Wl,-soname,libandroid-shmem.so -Wl,-z,max-page-size=16384 -Wl,-z,noexecstack \
  -I"$INC" "${DEFS[@]}" -o "$ORIG" "$ROOT/shmem/android_shmem.c"

echo "== NEEDED (solo libc/libdl permitidas):"
readelf -d "$ORIG" | grep NEEDED
if readelf -d "$ORIG" | grep NEEDED | grep -Eq 'libandroid\.so|libcutils|libbinder'; then
  echo "ERROR: el shim arrastra libandroid.so/cutils (choca con OpenSSL)" >&2; exit 1
fi
echo "== simbolos exportados que tenia la libreria de Termux y faltan en la nuestra:"
exp() { readelf --dyn-syms --wide "$1" | awk '$5=="GLOBAL" && $7!="UND" && $4=="FUNC" {print $8}' | sed 's/@.*//' | sort -u; }
comm -23 <(exp /tmp/libandroid-shmem.termux.so) <(exp "$ORIG") | tee /tmp/shmem-missing.txt
[ ! -s /tmp/shmem-missing.txt ] && echo "(ninguno)"
echo "== listo: $ORIG"
