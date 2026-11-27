#!/bin/sh
# A trial is kept in RAM. If the router reboots during the handover, use stock.
provider=/usr/libexec/mudi7-customizer/quec_battery
if [ -x "$provider" ] && {
    [ -e /etc/mudi7-customizer/battery-provider.enabled ] ||
    [ -e /var/run/mudi7-customizer/battery-provider.trial ]
}; then
    exec "$provider" "$@"
fi
exec /usr/bin/quec_battery.mudi7-stock "$@"
