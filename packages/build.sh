#!/bin/sh
# Usage: packages/build.sh /absolute/path/to/dependency-ipks
# Stages the native libraries from pinned GL.iNet feed packages.
#
# ipk format note: despite looking similar to a .deb, this is NOT an ar
# archive -- it's a plain gzip-compressed tar containing debian-binary,
# data.tar.gz, and control.tar.gz, in that order. Confirmed by pulling a
# real .ipk from GL.iNet's own opkg feed and inspecting it byte-for-byte;
# building it as an ar archive (the classic ipkg/dpkg convention many
# guides describe) makes real opkg reject it as "Malformed package file".
set -e
if [ "$#" -ne 1 ] || [ ! -d "$1" ]; then
    echo "usage: $0 DIRECTORY_WITH_PILLOW_AND_FREETYPE_IPKS" >&2
    exit 1
fi
DEP_DIR=$(cd "$1" && pwd)
cd "$(dirname "$0")"

PKG_NAME=luci-mudi7-customizer
VERSION=$(awk -F': ' '/^Version:/ {print $2}' control/control)
ARCH=$(awk -F': ' '/^Architecture:/ {print $2}' control/control)
OUT="${PKG_NAME}_${VERSION}_${ARCH}.ipk"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/data/usr/libexec/mudi7-customizer" "$WORK/data/etc/init.d" "$WORK/data/lib/upgrade/keep.d"
cp ../src/*.py ../src/*.sh "$WORK/data/usr/libexec/mudi7-customizer/"
cp ../init.d/batterycontrol ../init.d/mudi7-dashboard ../init.d/homebutton "$WORK/data/etc/init.d/"
chmod 755 "$WORK/data/usr/libexec/mudi7-customizer/"* "$WORK/data/etc/init.d/"*

cp -R ../luci/root/. "$WORK/data/"
find "$WORK/data" -name .DS_Store -type f -delete
chmod 755 "$WORK/data/usr/libexec/rpcd/luci-mudi7-customizer" "$WORK/data/usr/libexec/rpcd/luci-mudi7-customizer-preserve"

python3 stage-private-deps.py "$WORK/data" "$DEP_DIR"

printf '%s\n' /etc/mudi7-customizer/ > "$WORK/data/lib/upgrade/keep.d/mudi7-customizer"

mkdir -p "$WORK/control"
cp control/control control/preinst control/postinst control/prerm control/postrm "$WORK/control/"
chmod 644 "$WORK/control/control"
chmod 755 "$WORK/control/preinst" "$WORK/control/postinst" "$WORK/control/prerm" "$WORK/control/postrm"

echo "2.0" > "$WORK/debian-binary"

export COPYFILE_DISABLE=1
# Same archive format and root ownership either way, but the two tars
# spell it differently: bsdtar (macOS) wants --format=gnutar and
# --uid/--gid/--uname/--gname, GNU tar (Linux, Git Bash on Windows)
# rejects both and wants --format=gnu and --owner/--group.
if tar --version 2>/dev/null | grep -q "GNU tar"; then
    TAR_FLAGS="--format=gnu --owner=root:0 --group=root:0"
else
    TAR_FLAGS="--format=gnutar --uid=0 --gid=0 --uname=root --gname=root"
fi
tar $TAR_FLAGS -czf "$WORK/control.tar.gz" -C "$WORK/control" .
tar $TAR_FLAGS -czf "$WORK/data.tar.gz" -C "$WORK/data" .
tar $TAR_FLAGS -czf "$OUT" -C "$WORK" debian-binary data.tar.gz control.tar.gz
echo "wrote $OUT"
