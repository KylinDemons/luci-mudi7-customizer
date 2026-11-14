# LuCI po2lmo

These sources are from [OpenWrt LuCI](https://github.com/openwrt/luci/tree/openwrt-23.05/modules/luci-base/src)
(`po2lmo.c` and `lib/lmo.*`, `lib/plural_formula.*`). Original copyright
and Apache-2.0 license notices are retained in the source files.

`../build-i18n.sh` compiles this host tool in a temporary directory. The
resulting language IPKs contain only translations and language registration,
not the compiler. Set `PO2LMO` to use an existing LuCI compiler instead.
