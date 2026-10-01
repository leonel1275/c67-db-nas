#!/usr/bin/env python3
"""Baja PostgreSQL aarch64 de packages.termux.dev y lo deja listo para el APK.

No necesita com.g4pg, ni root, ni dispositivo. Requiere `readelf` (binutils).

Salida (dentro de android/app/src/main):
  jniLibs/arm64-v8a/libpostgres.so, libinitdb.so, + todas las libs .so necesarias
  assets/pgsupport.zip   usr/share/postgresql/** y usr/lib/postgresql/*.so (modulos)

Por que se parchean los binarios:
  1. Los .so de Termux llevan la ruta absoluta /data/data/com.termux/files/usr.
     "com.termux" tiene 10 caracteres, igual que el applicationId "com.c67.pg":
     el reemplazo conserva el largo (no rompe offsets ni cadenas).
  2. El APK solo empaqueta lib/<abi>/lib*.so. Los nombres versionados
     (libicuuc.so.78) se renombran a libicuuc.78.so, y se reescribe el
     SONAME/NEEDED dentro del ELF (mismo largo, in-place).
"""
import argparse
import gzip
import hashlib
import io
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

REPO_DEFAULT = "https://packages.termux.dev/apt/termux-main"
ARCH = "aarch64"
OLD_PKG = b"com.termux"
NEW_PKG = b"com.c67.pg"
assert len(OLD_PKG) == len(NEW_PKG)
PREFIX = "data/data/com.termux/files/usr"

# Libs que provee Android (NDK publico). Nunca se empaquetan.
SYSTEM_LIBS = {"libc.so", "libm.so", "libdl.so", "liblog.so", "libz.so", "libstdc++.so", "libandroid.so"}
# Solo para tests en un host glibc (tests/test_fetch.sh); en Android no se usa.
SYSTEM_LIBS |= set(filter(None, os.environ.get("C67_EXTRA_SYSTEM_LIBS", "").split(",")))
EXECUTABLES = {"postgres": "libpostgres.so", "initdb": "libinitdb.so"}
DENY_PKGS = {"termux-tools", "termux-exec", "termux-core", "termux-licenses", "bash", "coreutils",
             "dash", "busybox", "apt", "dpkg", "command-not-found"}


# ---------- utilidades puras (testeadas en tests/) ----------
def new_name(n):
    """Nombre empaquetable de una dependencia: libx.so.N -> libx.N.so (mismo largo)."""
    if n in SYSTEM_LIBS:
        return n
    if n == "libz.so.1":
        return "libz.so"
    m = re.fullmatch(r"(lib[^/]+?)\.so((?:\.\d+)+)", n)
    if m:
        return "{}{}.so".format(m.group(1), m.group(2))
    return n


def patch_bytes(data, mapping):
    for old, new in mapping.items():
        o = old.encode() + b"\0"
        n = new.encode() + b"\0" * (len(old) - len(new) + 1)
        assert len(o) == len(n), (old, new)
        data = data.replace(o, n)
    return data.replace(OLD_PKG, NEW_PKG)


def parse_packages(text):
    pkgs = {}
    for stanza in text.split("\n\n"):
        fields = dict(re.findall(r"^([A-Za-z0-9-]+): (.*)$", stanza, re.M))
        if "Package" in fields:
            pkgs[fields["Package"]] = fields
    return pkgs


def parse_deps(field):
    out = []
    for part in (field or "").split(","):
        name = part.split("|")[0].strip()
        name = re.sub(r"\s*\(.*?\)", "", name).strip()
        if name:
            out.append(name)
    return out


def ar_members(data):
    if data[:8] != b"!<arch>\n":
        raise ValueError("no es un archivo ar/deb")
    pos = 8
    while pos + 60 <= len(data):
        hdr = data[pos:pos + 60]
        name = hdr[:16].decode().strip().rstrip("/")
        size = int(hdr[48:58])
        pos += 60
        yield name, data[pos:pos + size]
        pos += size + (size & 1)


def needed(path):
    out = subprocess.run(["readelf", "-d", str(path)], capture_output=True, text=True)
    return re.findall(r"\(NEEDED\)\s+Shared library: \[(.+?)\]", out.stdout)


# ---------- red ----------
def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def load_index(repo):
    base = "{}/dists/stable/main/binary-{}/".format(repo, ARCH)
    for suffix, opener in (("Packages.xz", lzma.decompress), ("Packages.gz", gzip.decompress),
                           ("Packages", lambda b: b)):
        try:
            return parse_packages(opener(fetch(base + suffix)).decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            print("  index {} no disponible: {}".format(suffix, exc))
    sys.exit("no pude leer el indice de paquetes de " + base)


def closure(index, roots):
    seen, queue = [], list(roots)
    while queue:
        name = queue.pop(0)
        if name in seen or name in DENY_PKGS:
            continue
        if name not in index:
            print("  (aviso) paquete sin indice, se omite:", name)
            continue
        seen.append(name)
        f = index[name]
        queue += parse_deps(f.get("Depends")) + parse_deps(f.get("Pre-Depends"))
    return seen


def download_deb(repo, fields, cache):
    dest = cache / Path(fields["Filename"]).name
    if not dest.exists():
        print("  bajando", fields["Filename"])
        dest.write_bytes(fetch("{}/{}".format(repo, fields["Filename"])))
    want = fields.get("SHA256")
    if want and hashlib.sha256(dest.read_bytes()).hexdigest() != want:
        dest.unlink()
        sys.exit("SHA256 no coincide para " + dest.name)
    return dest


def extract_deb(deb, stage):
    for name, blob in ar_members(deb.read_bytes()):
        if name.startswith("data.tar"):
            if name.endswith(".zst"):
                sys.exit("deb con zstd no soportado: " + deb.name)
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
                try:
                    tf.extractall(stage, filter="fully_trusted")
                except TypeError:
                    tf.extractall(stage)
            return
    sys.exit("deb sin data.tar: " + deb.name)


# ---------- principal ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO_DEFAULT)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "android/app/src/main"))
    ap.add_argument("--work", default=str(Path(__file__).resolve().parent.parent / "build/termux"))
    ap.add_argument("--package", default="postgresql")
    args = ap.parse_args()

    out, work = Path(args.out), Path(args.work)
    cache, stage = work / "debs", work / "stage"
    shutil.rmtree(stage, ignore_errors=True)
    cache.mkdir(parents=True, exist_ok=True)
    stage.mkdir(parents=True)

    print("== indice Termux ({})".format(args.repo))
    index = load_index(args.repo)
    pkgs = closure(index, [args.package])
    print("== paquetes ({}): {}".format(len(pkgs), ", ".join(pkgs)))
    for p in pkgs:
        extract_deb(download_deb(args.repo, index[p], cache), stage)

    usr = stage / PREFIX
    if not (usr / "bin/postgres").exists():
        sys.exit("el paquete no trajo bin/postgres")

    # mapa NEEDED-name -> archivo real
    libmap = {}
    if (usr / "lib").is_dir():
        for p in (usr / "lib").iterdir():
            if p.name.startswith("lib") and ".so" in p.name and p.resolve().is_file():
                libmap[p.name] = p.resolve()

    bundle, names, missing = {}, set(), {}

    def visit(path, owner):
        ok = True
        for n in needed(path):
            if n in SYSTEM_LIBS or n == "libz.so.1":
                names.add(n)
                continue
            if n not in libmap:
                missing.setdefault(owner, []).append(n)
                ok = False
                continue
            names.add(n)
            tgt = new_name(n)
            if tgt not in bundle:
                bundle[tgt] = libmap[n]
                visit(libmap[n], tgt)
        return ok

    for exe in EXECUTABLES:
        if not visit(usr / "bin" / exe, exe):
            sys.exit("dependencias sin resolver para {}: {}".format(exe, missing[exe]))

    modules = []
    moddir = usr / "lib/postgresql"
    for m in sorted(moddir.glob("*.so")) if moddir.is_dir() else []:
        if visit(m, m.name):
            modules.append(m)
        else:
            print("  (aviso) modulo omitido por deps faltantes:", m.name, missing[m.name])

    mapping = {n: new_name(n) for n in names if new_name(n) != n}

    jni = out / "jniLibs/arm64-v8a"
    shutil.rmtree(jni, ignore_errors=True)
    jni.mkdir(parents=True)
    for exe, dst in EXECUTABLES.items():
        (jni / dst).write_bytes(patch_bytes((usr / "bin" / exe).read_bytes(), mapping))
    for dst, src in bundle.items():
        (jni / dst).write_bytes(patch_bytes(src.read_bytes(), mapping))

    assets = out / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    zpath = assets / "pgsupport.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for m in modules:
            z.writestr("usr/lib/postgresql/" + m.name, patch_bytes(m.read_bytes(), mapping))
        share = usr / "share/postgresql"
        for f in sorted(share.rglob("*")) if share.is_dir() else []:
            if f.is_file():
                z.write(f.resolve(), "usr/share/postgresql/" + f.relative_to(share).as_posix())

    meta = {"postgresql": index["postgresql"].get("Version"), "packages": pkgs,
            "libs": sorted(bundle), "modules": [m.name for m in modules], "rename": mapping}
    (work / "manifest.json").write_text(json.dumps(meta, indent=2))
    total = sum(f.stat().st_size for f in jni.iterdir())
    print("== PostgreSQL {} | {} libs ({:.1f} MB sin comprimir) | {} modulos | pgsupport.zip {:.1f} MB".format(
        meta["postgresql"], len(bundle) + len(EXECUTABLES), total / 1e6, len(modules), zpath.stat().st_size / 1e6))
    if "libandroid-shmem.so" in bundle:
        print("== OJO: este PG usa libandroid-shmem.so de Termux -> reemplazala con scripts/build-shmem-fix.sh")
    else:
        print("== este PG no usa libandroid-shmem.so (no hace falta el fix de shmem)")


if __name__ == "__main__":
    main()
