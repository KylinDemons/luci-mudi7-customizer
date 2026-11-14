# Stock UI Chinese support

Adapted from tutugreen/gl-screen-e5800-i18n-zh-cn, commit d9b3b3e
(MIT; see LICENSE.tutugreen). GL.iNet owns the original UI strings.
Modified IBM Plex Sans SC 1.1.0 fonts retain the complete glyph set and use
new family names; their license is OFL.txt. No GL.iNet font binaries or
screen executables are redistributed.

## Resource matching (plugin 1.0.3 and later)

Firmware and screen package version numbers are informational, not an install
allowlist. The verified layout snapshot remains GL-E5800 4.10.2 with screen
package `git-2026.289.31233-a105142-1`, read on 2026-11-14.

The helper matches each key AND original raw value against catalog.json,
checks printf placeholders and preserves unchanged reference relationships.
It emits only entries present in the device's original text. Unknown keys
and changed source values stay verbatim, so partial compatibility may leave
some English. Counts are exposed by the status API.

An original-language provenance check requires its hash to match the device's
read-only /rom file or the known reviewed original hash. Unknown modifications
and occupied custom-font paths are rejected; they are not accepted just
because their text parses. The helper also requires GL-E5800, an installed
official screen package, a valid resource structure and known font mappings.
Other devices, unsupported fonts, conflicting/malformed entries, damaged
payloads and damaged backups are blocked.

If the screen executable and original fonts match the verified fingerprints,
the reviewed date-layout and SMS-spacing overrides are eligible. Otherwise
only translation/font mappings are applied, preserving original layouts.
Unverified implementations or unmatched entries display:

> 当前屏幕版本或部分语言条目尚未完全适配，可能出现问题。出现问题请还原并等待适配。

This warning allows a reversible partial localization; it is not proof that
an untested firmware renders correctly. Structurally unsafe resources remain
blocked rather than being converted into warnings. An official screen-package
update installed into overlay can differ from /rom; absent another verified
original hash it is conservatively rejected as an unknown modification.

## Rebuild

Install `fonttools==4.60.2` in a temporary virtual environment. Obtain the
IBM release archive `@ibm/plex-sans-sc@1.1.0/ibm-plex-sans-sc.zip` from
https://github.com/IBM/plex/releases . Collect a read-only device snapshot
containing /etc/glversion, /usr/lib/opkg/status, /usr/bin/gl_screen and
/etc/gl_screen/language. Keep the snapshot outside the repository.

```
python tools/build-stock-i18n.py /path/to/device-root /path/to/ibm-plex-sans-sc.zip
python tools/build-stock-catalog.py  # also run after translation-only edits
python tools/validate-stock-i18n.py
```

To adapt another firmware, compare its original text against sources/en,
update sources/en and sources/zh-cn together, then rebuild. Review changed
meanings, keys, printf placeholders and layout on the device before shipping.
The current source splits the upstream Tor/Fast Charging joined entry,
translates the two new firmware entries and includes SMS line spacing.
Months and weekdays are localized. Both lock-screen date formats use
`%s%d日`; style 1 uses a 64-pixel date label, while style 2 also moves
its 64-pixel label to x=70 so it ends before the weekday at x=139.
The experimental fixed weekday centering was removed: stock style 1 has no
configurable text-alignment property for its date and weekday labels. They
retain their stock positions (weekday x=174, width=37). See ALIGNMENT.md.
These overrides remain inside the backed-up language file.
All five mappings use independent *_zh-cn.ttf files with Latin UI vertical
metrics. Original fonts remain untouched. Full CJK fonts allow arbitrary SMS
text; payload is approximately 40 MiB before compression.

## Recovery and lifecycle

The helper stores verified pre-change files and a transaction record in
/etc/mudi7-stock-i18n/ before changing any live resource. Absent font files
are recorded as absent and deleted on restoration. It never backs up an
unknown pre-existing localization as if it were the original firmware.

An interrupted transaction or external edit displays Unknown. A valid backup
can still be restored while the original screen executable, fonts and /rom
language generation match its recorded resource guards, even if version
metadata changes. Resource changes or backup corruption block restoration
and keep recovery data. Old plugin backups retain their conservative original
version/fingerprint restoration checks. Restore before a
firmware upgrade; do not replay old resource backups onto a new firmware.
The keep rule preserves recovery data but does not automatically reapply the
language pack. Cleanup and actual package removal restore first; package
upgrades retain the active resources and backup. If removal cannot safely
restore, it aborts instead of deleting recovery tools.

Stock service is restarted only when already running, leaving the custom
screen in control otherwise. Hashes establish file state; they cannot prove
that every screen layout renders correctly. UI rendering needs device QA.

## Screenshot QA

`python3 tools/capture-stock-screen.py /tmp/stock-screen.png` reads the
240×320 RGB565 framebuffer through SSH and saves PNG without changing power
or display settings. Screen startup can leave a stale frame briefly; check
that the clock and translated content have refreshed before judging layout.
