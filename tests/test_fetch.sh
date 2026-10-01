#!/usr/bin/env bash
# Test de scripts/fetch_termux.py contra un repo Termux FALSO (debs reales, ELF del host).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
P=data/data/com.termux/files/usr
R="$W/repo"; mkdir -p "$R/dists/stable/main/binary-aarch64" "$R/pool"
cd "$W"

cat > foo.c <<'C'
int foo(void){ return 42; }
C
cat > main.c <<'C'
#include <stdio.h>
int foo(void);
int main(void){ printf("foo=%d share=%s\n", foo(), "/data/data/com.termux/files/usr/share/postgresql"); return 0; }
C
cat > mod.c <<'C'
int foo(void); int mod(void){ return foo(); }
C
cat > bad.c <<'C'
int nolib(void); int bad(void){ return nolib(); }
C
gcc -shared -fPIC -Wl,-soname,libfoo.so.1 -o libfoo.so.1.0 foo.c
gcc -shared -fPIC -Wl,-soname,libmissing.so.9 -o libmissing.so.9 foo.c
gcc -o postgres main.c -L. -l:libfoo.so.1.0 -Wl,--no-as-needed
# NEEDED real = libfoo.so.1 (por soname)
gcc -o postgres main.c ./libfoo.so.1.0 2>/dev/null || true
cp libfoo.so.1.0 libfoo.so.1 && gcc -o postgres main.c -L. -l:libfoo.so.1
cp postgres initdb
gcc -shared -fPIC -o plpgsql.so mod.c -L. -l:libfoo.so.1
gcc -shared -fPIC -o badmod.so bad.c -L. -Wl,--no-as-needed -l:libmissing.so.9

mkpkg() { # nombre version depends  (el contenido sale de ./pkg_$nombre)
  local n=$1 v=$2 d=$3
  ( cd "pkg_$n" && tar -cJf ../data.tar.xz . )
  echo "Package: $n" > control; echo "Version: $v" >> control
  tar -cJf control.tar.xz control; echo 2.0 > debian-binary
  rm -f "$R/pool/$n.deb"; ar rc "$R/pool/$n.deb" debian-binary control.tar.xz data.tar.xz
  local sum; sum=$(sha256sum "$R/pool/$n.deb" | cut -d' ' -f1)
  printf 'Package: %s\nVersion: %s\nDepends: %s\nFilename: pool/%s.deb\nSHA256: %s\n\n' "$n" "$v" "$d" "$n" "$sum" >> "$R/dists/stable/main/binary-aarch64/Packages"
}
: > "$R/dists/stable/main/binary-aarch64/Packages"
mkdir -p pkg_libfoo/$P/lib; cp libfoo.so.1 libmissing.so.9 pkg_libfoo/$P/lib/ 2>/dev/null
rm pkg_libfoo/$P/lib/libmissing.so.9
ln -s libfoo.so.1 pkg_libfoo/$P/lib/libfoo.so
mkdir -p pkg_postgresql/$P/{bin,lib/postgresql,share/postgresql/extension}
cp postgres initdb pkg_postgresql/$P/bin/; cp plpgsql.so badmod.so pkg_postgresql/$P/lib/postgresql/
echo "CREATE FUNCTION x();" > pkg_postgresql/$P/share/postgresql/extension/plpgsql--1.0.sql
mkpkg libfoo 1.0 ""
mkpkg postgresql 18.2 "libfoo (>= 1.0), termux-tools"

C67_EXTRA_SYSTEM_LIBS=libc.so.6 python3 "$HERE/../scripts/fetch_termux.py" --repo "file://$R" --out "$W/out" --work "$W/work"

J="$W/out/android/jniLibs/arm64-v8a"; J="$W/out/jniLibs/arm64-v8a"
echo "--- jniLibs:"; ls "$J"
echo "--- NEEDED de libpostgres.so:"; readelf -d "$J/libpostgres.so" | grep -E 'NEEDED'
echo "--- SONAME de la lib renombrada:"; readelf -d "$J/libfoo.1.so" | grep SONAME
echo "--- ruta com.termux restante en binarios:"; (grep -c "com.termux" "$J"/*.so || true) | tr '\n' ' '; echo
echo "--- ruta nueva:"; strings "$J/libpostgres.so" | grep "com.c67.pg"
echo "--- EJECUTAR binario parcheado (el loader debe resolver libfoo.1.so):"
chmod +x "$J/libpostgres.so"; LD_LIBRARY_PATH="$J" "$J/libpostgres.so"
echo "--- pgsupport.zip:"; unzip -l "$W/out/assets/pgsupport.zip" | awk 'NR>3 && $4 {print $4}'
unzip -p "$W/out/assets/pgsupport.zip" usr/lib/postgresql/plpgsql.so | cmp -n 4 - plpgsql.so && echo "modulo OK"
echo "--- badmod.so NO debe estar:"; if unzip -l "$W/out/assets/pgsupport.zip" | grep -q badmod; then echo "FALLO: badmod incluido"; exit 1; else echo "omitido OK"; fi
python3 - <<PY
import sys; sys.path.insert(0, "$HERE/../scripts")
from fetch_termux import new_name, patch_bytes, parse_deps
assert new_name("libicuuc.so.78")=="libicuuc.78.so" and len("libicuuc.so.78")==len("libicuuc.78.so")
assert new_name("libz.so.1")=="libz.so" and new_name("libc.so")=="libc.so"
assert new_name("libssl.so.3")=="libssl.3.so" and new_name("libandroid-shmem.so")=="libandroid-shmem.so"
assert new_name("libfoo.so.1.2.3")=="libfoo.1.2.3.so"
assert parse_deps("libicu (>= 78), libx | liby, termux-tools")==["libicu","libx","termux-tools"]
b=patch_bytes(b"\0libz.so.1\0/data/data/com.termux/files/usr\0",{"libz.so.1":"libz.so"})
assert b==b"\0libz.so\0\0\0/data/data/com.c67.pg/files/usr\0", b
print("unit OK")
PY
