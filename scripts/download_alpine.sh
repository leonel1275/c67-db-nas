#!/usr/bin/env bash
set -eux
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ROOTFS_DIR="$PROJECT_ROOT/vm/alpine-rootfs"
ALPINE_VERSION="3.19.1"
ARCH="aarch64"
TARBALL="alpine-minirootfs-${ALPINE_VERSION}-${ARCH}.tar.gz"
URL="https://dl-cdn.alpinelinux.org/alpine/v3.19/releases/${ARCH}/${TARBALL}"
mkdir -p "$ROOTFS_DIR"
mkdir -p "$PROJECT_ROOT/vm"
if [ ! -f "$PROJECT_ROOT/vm/$TARBALL" ]; then
  curl -L -o "$PROJECT_ROOT/vm/$TARBALL" "$URL"
fi
cp "$PROJECT_ROOT/vm/$TARBALL" "$PROJECT_ROOT/vm/alpine-rootfs.tar.gz"
echo "[OK] listo"
