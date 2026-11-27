#!/bin/sh
set -eu

REQUIRED_COMMIT=6760f4b711ae670db7ea8b9965f8e70e38242afd
SOURCE=$1
OUTPUT=$2
HERE=$(cd "$(dirname "$0")" && pwd)
CC=${QUEC_BATTERY_CC:-aarch64-linux-musl-gcc}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Use a pristine upstream snapshot so the integration applies to known code.
git -C "$SOURCE" archive "$REQUIRED_COMMIT" > "$WORK/upstream.tar"
tar -xf "$WORK/upstream.tar" -C "$WORK"
patch -d "$WORK" -p1 < "$HERE/control.patch"
cp "$HERE/mudi7_control.c" "$HERE/mudi7_control.h" "$WORK/src/"
make -C "$WORK" CC="$CC"
if ! file -b "$WORK/quec_battery" | grep -Eq 'ELF 64-bit .*(ARM aarch64|AArch64).*ld-musl-aarch64\.so\.1'; then
    echo 'quec-battery2 must be built for AArch64 Linux musl.' >&2
    exit 1
fi
install -m 755 "$WORK/quec_battery" "$OUTPUT"
