#!/usr/bin/env bash
# Uso: scripts/build-c67.sh [--install] [--skip-fetch]
# Requiere: JDK 17, Gradle 8.9 (o ./gradlew), Android SDK 34, NDK r27d, python3, readelf, Python 3.12 (Chaquopy).
set -euo pipefail
cd "$(dirname "$0")/.."
INSTALL=0; FETCH=1
for a in "$@"; do case "$a" in --install) INSTALL=1;; --skip-fetch) FETCH=0;; esac; done

die() { echo "ERROR: $*" >&2; exit 1; }
for c in python3 readelf java; do command -v "$c" >/dev/null || die "falta $c"; done
: "${ANDROID_HOME:=${ANDROID_SDK_ROOT:-}}"
[ -d "${ANDROID_HOME:-/nonexistent}" ] || die "defini ANDROID_HOME (SDK con platform-34 y build-tools 34)"
[ -d "${ANDROID_NDK_HOME:-/nonexistent}" ] || die "defini ANDROID_NDK_HOME (NDK r27d)"
export ANDROID_HOME
export CHAQUOPY_BUILD_PYTHON="${CHAQUOPY_BUILD_PYTHON:-$(command -v python3.12 || command -v python3)}"
echo "sdk.dir=$ANDROID_HOME" > android/local.properties

if [ "$FETCH" = 1 ]; then
  python3 scripts/fetch_termux.py
  scripts/build-shmem-fix.sh
fi

GRADLE="${GRADLE:-gradle}"; [ -x android/gradlew ] && GRADLE=./gradlew
( cd android && $GRADLE --no-daemon :app:assembleDebug )

APK=android/app/build/outputs/apk/debug/app-debug.apk
[ -f "$APK" ] || die "no se genero $APK"
for lib in libpostgres.so libinitdb.so; do
  unzip -l "$APK" | grep -q "lib/arm64-v8a/$lib" || die "$lib no esta en el APK"
done
ls -lh "$APK"
if [ "$INSTALL" = 1 ]; then
  adb install -r "$APK"
  adb shell am start -n com.c67.pg/com.systutor.g5.MainActivity
  echo "Siguiente paso: scripts/verify-g0-g3.sh [IP_TAILSCALE]"
fi
