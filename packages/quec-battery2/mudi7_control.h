#ifndef MUDI7_CONTROL_H
#define MUDI7_CONTROL_H

#include <stdbool.h>

struct qb_manager;

enum mudi7_watchdog_state {
    MUDI7_WATCHDOG_PENDING,
    MUDI7_WATCHDOG_ACTIVE,
    MUDI7_WATCHDOG_DISABLED,
};

bool mudi7_reload_limit(struct qb_manager *cm);
void mudi7_publish_battery_state(const struct qb_manager *cm, bool online,
                                 bool telemetry_valid);
void mudi7_watchdog_state(enum mudi7_watchdog_state state);

#endif
