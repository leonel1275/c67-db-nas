#!/usr/bin/env bash
# Verificacion G0-G3 por ADB (estilo cairn-ehr). Uso: scripts/verify-g0-g3.sh [IP_del_celu]
set -uo pipefail
ADB="${ADB:-adb}"; PKG=com.c67.pg; IP="${1:-}"; FAIL=0
ok()   { echo "  PASS  $*"; }
bad()  { echo "  FAIL  $*"; FAIL=1; }
$ADB get-state >/dev/null 2>&1 || { echo "adb sin dispositivo (adb devices)"; exit 2; }

APK="$($ADB shell pm path $PKG | head -1 | sed 's/package://' | tr -d '\r')"
LIB="$(dirname "$APK")/lib/arm64"
echo "G0 exec ($LIB)"
V="$($ADB shell "run-as $PKG env LD_LIBRARY_PATH=$LIB $LIB/libpostgres.so --version" 2>&1 | tr -d '\r')"
[[ "$V" == *"PostgreSQL) 18"* ]] && ok "$V" || bad "postgres --version: $V"

echo "G1 initdb"
PV="$($ADB shell "run-as $PKG cat files/pgdata/PG_VERSION" 2>&1 | tr -d '\r')"
[ "$PV" = "18" ] && ok "PG_VERSION=18" || bad "PG_VERSION='$PV' (initdb no corrio: logcat -s G5:*)"

echo "G2 postmaster TCP (estado LISTEN=0A)"
T="$($ADB shell 'cat /proc/net/tcp /proc/net/tcp6' | tr -d '\r')"
echo "$T" | grep -Eiq ':1538 .* 0A ' && ok "5432 escuchando" || bad "5432 NO escucha"
echo "$T" | grep -Eiq ':1F40 .* 0A ' && ok "8000 escuchando" || bad "8000 NO escucha"
L="$($ADB logcat -d -s G5:* | tr -d '\r')"
echo "$L" | grep -E 'FATAL|PANIC' | tail -5 | sed 's/^/        /'
echo "$L" | grep -qE 'FATAL|PANIC' && bad "hay FATAL/PANIC en logcat" || ok "sin FATAL/PANIC"
CPU="$($ADB shell 'top -b -n 1 | grep -i libpostgres | head -3' | tr -d '\r')"
[ -n "$CPU" ] && echo "        cpu: $CPU"
$ADB logcat -d | grep -c 'com.termux' | xargs -I{} echo "        lineas de logcat con com.termux (esperado 0): {}"

echo "G3 API NAS"
TOKEN="$($ADB shell "run-as $PKG cat files/nas_token.txt" | tr -d '\r\n')"
if [ -n "$IP" ]; then
  curl -s -m 8 "http://$IP:8000/api/health" | grep -q '"status": "ok"' && ok "GET /api/health" || bad "health en $IP:8000"
  curl -s -m 8 -H "Authorization: Bearer $TOKEN" "http://$IP:8000/api/nas/list" | grep -q '"items"' && ok "GET /api/nas/list" || bad "nas/list"
  (exec 3<>"/dev/tcp/$IP/5432") 2>/dev/null && ok "TCP $IP:5432 abierto (usa DBeaver: user postgres)" || bad "TCP $IP:5432 cerrado"
else
  echo "  (pasa la IP Tailscale para probar desde la PC)"
fi
$ADB shell 'ls /storage/emulated/0/NAS' 2>&1 | head -5 | sed 's/^/        NAS: /'
[ $FAIL = 0 ] && echo "== TODO OK" || echo "== HAY FALLAS"; exit $FAIL
