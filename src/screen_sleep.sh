#!/bin/sh
# Match GL-E5800's stock LPM backlight gate, and blank the SPI panel.
# Dashboard draws the wake frame while dark before invoking "on".
# On firmware 4.10.2, the touchscreen's display-suspend callback is not
# registered. These operations do not claim to suspend the touch IC.

BL=/sys/class/backlight/soc:backlight
FB_BLANK=/sys/class/graphics/fb0/blank
STATE_FILE=/etc/mudi7-customizer/backlight_saved
WAKE_REQUEST=/var/run/mudi7-customizer/screen-wake

stock_screen_power() {
    # Let GL's service track the screen state as it does for gl_screen.
    # Direct bl_power writes below also cover an absent/stopped LPM service.
    if command -v ubus >/dev/null 2>&1; then
        ubus call lpm set_screen "{\"action\":\"$1\"}" >/dev/null 2>&1 || true
    fi
}

case "$1" in
    off)
        rm -f "$WAKE_REQUEST"
        cur=$(cat "$BL/brightness" 2>/dev/null)
        if [ -n "$cur" ] && [ "$cur" -gt 0 ]; then
            mkdir -p "${STATE_FILE%/*}" || exit 1
            echo "$cur" > "$STATE_FILE" || exit 1
        fi
        # Darken first, then ask the same LPM gate used by the stock UI.
        echo 0 > "$BL/brightness" || exit 1
        stock_screen_power turnoff
        if [ -e "$BL/bl_power" ]; then
            echo 1 > "$BL/bl_power" || exit 1
        fi
        echo 4 > "$FB_BLANK" 2>/dev/null
        ;;
    on)
        # Keep the backlight dark while restoring the display and LPM gate.
        echo 0 > "$BL/brightness" || exit 1
        echo 0 > "$FB_BLANK" 2>/dev/null || exit 1
        stock_screen_power turnon
        if [ -e "$BL/bl_power" ]; then
            echo 0 > "$BL/bl_power" || exit 1
        fi
        if [ -f "$STATE_FILE" ]; then
            cat "$STATE_FILE" > "$BL/brightness"
        else
            cat "$BL/max_brightness" > "$BL/brightness"
        fi
        ;;
    toggle)
        cur=$(cat "$BL/brightness" 2>/dev/null)
        if [ -z "$cur" ] || [ "$cur" = "0" ]; then
            mkdir -p "${WAKE_REQUEST%/*}" && : > "$WAKE_REQUEST"
        else
            "$0" off
        fi
        ;;
    status)
        cur=$(cat "$BL/brightness" 2>/dev/null)
        [ "$cur" = "0" ] && echo "asleep" || echo "awake"
        ;;
    *)
        echo "usage: $0 on|off|toggle|status"
        exit 1
        ;;
esac
