#!/bin/sh
# Usage: packages/build-i18n.sh [LANGUAGE ...]
# Each luci/po/LANGUAGE/mudi7-customizer.po produces a separate language IPK.
set -eu
cd "$(dirname "$0")"
VERSION=$(awk -F': ' '/^Version:/ {print $2}' control/control)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
if [ -n "${PO2LMO:-}" ]; then
    COMPILER=$PO2LMO
else
    ${CC:-cc} -O2 -o "$WORK/po2lmo" po2lmo/po2lmo.c po2lmo/lib/lmo.c po2lmo/lib/plural_formula.c
    COMPILER=$WORK/po2lmo
fi
if tar --version 2>/dev/null | grep -q 'GNU tar'; then
    TAR_FLAGS='--format=gnu --owner=root:0 --group=root:0'
else
    TAR_FLAGS='--format=gnutar --uid=0 --gid=0 --uname=root --gname=root'
fi
export COPYFILE_DISABLE=1
if [ "$#" -eq 0 ]; then
    set -- ../luci/po/*/mudi7-customizer.po
    for file do
        language=$(basename "$(dirname "$file")")
        "./$(basename "$0")" "$language"
    done
    exit 0
fi
for language do
    case "$language" in
        *[!a-zA-Z0-9_-]*|'') echo 'Invalid language code' >&2; exit 1 ;;
    esac
    # PO directory tags differ from the runtime tags on OpenWrt 23.05.
    case "$language" in
        zh_Hans) suffix=zh-cn; label='简体中文 (Chinese Simplified)' ;;
        zh_Hant) suffix=zh-tw; label='繁體中文 (Chinese Traditional)' ;;
        *) suffix=$(printf '%s' "$language" | tr 'A-Z_' 'a-z-'); label=$language ;;
    esac
    catalog=$suffix
    uci_language=$(printf '%s' "$catalog" | tr '-' '_')
    name=luci-i18n-mudi7-customizer-$suffix
    stage=$WORK/$language
    mkdir -p "$stage/data/usr/lib/lua/luci/i18n" "$stage/data/etc/uci-defaults" "$stage/control"
    "$COMPILER" "../luci/po/$language/mudi7-customizer.po" "$stage/data/usr/lib/lua/luci/i18n/mudi7-customizer.$catalog.lmo"
    cat > "$stage/data/etc/uci-defaults/$name" <<EOF
#!/bin/sh
# Clean up only the erroneous registration made by the old language package.
if [ "$language" = zh_Hans ] && [ "\$(uci -q get luci.languages.zh_Hans)" = '简体中文 (Chinese Simplified)' ]; then
    uci -q delete luci.languages.zh_Hans
fi
uci -q get luci.languages.$uci_language >/dev/null || uci -q set luci.languages.$uci_language='$label'
uci -q commit luci
exit 0
EOF
    chmod 755 "$stage/data/etc/uci-defaults/$name"
    cat > "$stage/control/control" <<EOF
Package: $name
Version: $VERSION
Architecture: all
Maintainer: KylinDemons <KylinDemons@foxmail.com>
Section: luci
Priority: optional
Depends: luci-mudi7-customizer
Description: $language translation for Mudi7 Customizer
EOF
    cat > "$stage/control/postinst" <<EOF
#!/bin/sh
[ -n "\$IPKG_INSTROOT" ] && exit 0
/etc/uci-defaults/$name && rm -f /etc/uci-defaults/$name
rm -f /tmp/luci-indexcache /tmp/luci-indexcache.*
exit 0
EOF
    cat > "$stage/control/postrm" <<'EOF'
#!/bin/sh
[ -n "$IPKG_INSTROOT" ] && exit 0
rm -f /tmp/luci-indexcache /tmp/luci-indexcache.*
exit 0
EOF
    chmod 755 "$stage/control/postinst" "$stage/control/postrm"
    printf '2.0\n' > "$stage/debian-binary"
    tar $TAR_FLAGS -czf "$stage/control.tar.gz" -C "$stage/control" .
    tar $TAR_FLAGS -czf "$stage/data.tar.gz" -C "$stage/data" .
    out=${name}_${VERSION}_all.ipk
    tar $TAR_FLAGS -czf "$out" -C "$stage" debian-binary data.tar.gz control.tar.gz
    echo "wrote $out"
done
