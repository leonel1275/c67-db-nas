#!/usr/bin/env bash
# Prueba la logica del shim de shmem en el host (procesos separados, creador vivo/muerto).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
gcc -Wall -O2 -DHAVE_SHM_SEGSZ -DHAVE_SHM_NATTCH -DHAVE_SHM_PERM_UID \
    "$HERE/shmem/test_shmem.c" "$HERE/shmem/android_shmem.c" -o "$T/t"
export TMPDIR="$T"
"$T/t" create 4242 4 & A=$!; sleep 1
R1="$("$T/t" excl 4242)";   echo "$R1" | grep -q "FAIL errno=17"  || { echo "FALLO: no dio EEXIST con creador vivo"; exit 1; }
echo "$R1" | grep -q "nattch=1"   || { echo "FALLO: nattch debe ser >=1"; exit 1; }
"$T/t" attach 4242 | grep -q PGMAGIC || { echo "FALLO: attach ajeno"; exit 1; }
kill -9 $A; wait $A 2>/dev/null || true
"$T/t" excl 4242 | grep -q "OK(created)" || { echo "FALLO: no limpio la clave huerfana"; exit 1; }
"$T/t" missing 999 | grep -q "errno=2" || { echo "FALLO: ENOENT"; exit 1; }
echo "shmem OK"
