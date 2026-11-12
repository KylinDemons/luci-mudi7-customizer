#!/bin/sh
# Switch between the custom Dashboard and the stock GL.iNet UI.
# enable/disable also controls the power-button watcher.
#
# Usage: toggle.sh on|off|enable|disable|status

set -e
DISABLED=/etc/mudi7-customizer/screen-disabled
DASHBOARD=/etc/init.d/mudi7-dashboard
STOCK=/etc/init.d/gl_screen
WATCH=/etc/init.d/homebutton

wait_gone() {
    # wait briefly for a process name to fully exit; force-kill any
    # survivor immediately rather than waiting out procd's own ~5s
    # SIGTERM-then-SIGKILL grace period (gl_screen ignores SIGTERM, so
    # letting procd's stop run to completion made every "on" switch
    # take 5+ seconds)
    n=0
    while pgrep -f "$1" >/dev/null 2>&1 && [ "$n" -lt 6 ]; do
        sleep 0.2
        n=$((n + 1))
    done
    # no pkill on this busybox -- kill any survivors by PID instead
    pids=$(pgrep -f "$1" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        kill -9 $pids 2>/dev/null || true
        sleep 0.1
    fi
    if pgrep -f "$1" >/dev/null 2>&1; then
        echo "Could not stop $1" >&2
        return 1
    fi
}

show_dashboard() {
    if [ -e "$DISABLED" ] || ! "$WATCH" enabled; then
        echo "Third-party screen is disabled" >&2
        return 1
    fi
    "$STOCK" stop 2>/dev/null || true
    wait_gone '^/usr/bin/gl_screen( |$)' || return 1
    "$STOCK" disable || return 1
    "$DASHBOARD" enable || return 1
    "$DASHBOARD" running 2>/dev/null || "$DASHBOARD" start
}

show_stock() {
    "$DASHBOARD" disable
    "$DASHBOARD" stop 2>/dev/null || true
    wait_gone '^/bin/sh /usr/libexec/mudi7-customizer/run[.]sh( |$)'
    wait_gone '^/usr/bin/python3 /usr/libexec/mudi7-customizer/dashboard[.]py( |$)'
    "$STOCK" enable
    if ! "$STOCK" running 2>/dev/null; then
        "$STOCK" start
        /usr/libexec/mudi7-customizer/screen_sleep.sh on
    fi
    "$STOCK" running
}

case "$1" in
    on|off|enable|disable)
        # Use a package-owned inode so cleanup leaves no lock file behind.
        exec 9<"$DASHBOARD"
        flock -x 9
        ;;
esac

case "$1" in
    on) show_dashboard ;;
    off) show_stock ;;
    enable)
        "$WATCH" enable
        "$WATCH" running 2>/dev/null || "$WATCH" start
        rm -f "$DISABLED"
        if ! show_dashboard; then
            touch "$DISABLED"
            "$WATCH" disable
            "$WATCH" stop 2>/dev/null || true
            show_stock
            exit 1
        fi
        ;;
    disable)
        mkdir -p "${DISABLED%/*}"
        touch "$DISABLED"
        "$WATCH" disable
        "$WATCH" stop 2>/dev/null || true
        wait_gone '^/usr/bin/python3 /usr/libexec/mudi7-customizer/button_watch[.]py( |$)'
        show_stock
        ;;
    status)
        "$DASHBOARD" running 2>/dev/null && echo "dashboard: running" || echo "dashboard: stopped"
        "$STOCK" running 2>/dev/null && echo "stock gl_screen: running" || echo "stock gl_screen: stopped"
        ;;
    *)
        echo "usage: $0 on|off|enable|disable|status" >&2
        exit 1
        ;;
esac
