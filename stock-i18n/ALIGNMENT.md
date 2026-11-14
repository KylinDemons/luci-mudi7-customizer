# Stock lock-screen text alignment investigation

Investigated 2026-11-14 on GL-E5800 firmware 4.10.2, screen package
`git-2026.289.31233-a105142-1`. Conclusions apply to this examined version.

Read-only checks covered the device's reference, inch, screen and product
layout layers, default generic settings, UCI-generated runtime configuration,
platform/event scripts and ubus surface. Static AArch64 analysis of the exact
screen executable identified the string references and corresponding widget
configuration calls. The executable was not patched.

The only application configuration key ending in TEXT_ALIGN is
`LOCK_SCREEN_DATA_STYLE2_DAY_LABEL_TEXT_ALIGN`. It is read and passed into a
style setter during style 2 date-label creation (string reference at virtual
address 0xb5310, style setter call at 0xb532c).

Style 1 date-label creation reads x/y and width/height, format and color
(configuration references starting at 0xb4af4). Weekday creation follows the
same position/size/color pattern (starting at 0xb4c10). Neither block reads a
text-alignment setting. No generic configurable style inheritance or exposed
runtime widget alignment API was identified that can set both labels to
center alignment. The presence of LVGL style functions inside the executable
does not make them callable through the existing configuration format.

Shared x/width alone was tested on the device and only left-aligned both
strings; it did not center the shorter weekday within its label. A manual
weekday offset worked for a particular date but is not automatic alignment.
Changing to stock style 2 changes the clock design and still provides no
weekday text-alignment key, so it is not a solution for the user's existing
stacked layout.

Result: no supported configuration-only solution was found for automatic
centering across arbitrary date/weekday lengths in style 1. A future vendor
alignment option would be preferable to binary patches, runtime injection,
external drawing, or periodic coordinate-rewriting services.

Per user request, remove the experimental overrides:

- LOCK_SCREEN_DATA_STYLE1_DAY_LABEL_X 168 (redundant stock position)
- LOCK_SCREEN_DATA_STYLE1_WEEK_LABEL_X 184 (fixed visual offset)
- LOCK_SCREEN_DATA_STYLE1_WEEK_LABEL_WIDTH 40

Keep Chinese month/weekday translations, the `%s%d日` date formats, and the
64-pixel date label width that fixes wrapping. Weekday returns to the stock
x=174 and width=37. Style 2 date wrapping fixes remain unchanged. All changes
remain in the backed-up language file; no additional daemon or firmware
modification was introduced.
