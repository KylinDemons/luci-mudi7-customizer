#include "qb.h"
#include "mudi7_control.h"

#include <fcntl.h>
#include <stdatomic.h>
#include <stdio.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define MUDI7_LIMIT "/etc/mudi7-customizer/battery-limit"
#define MUDI7_RUN "/var/run/mudi7-customizer"
#define MUDI7_STATUS MUDI7_RUN "/battery-provider.json"
#define MUDI7_TEMP MUDI7_STATUS ".new"

static atomic_int watchdog_state = MUDI7_WATCHDOG_PENDING;

bool mudi7_reload_limit(struct qb_manager *cm)
{
    FILE *config = fopen(MUDI7_LIMIT, "r");
    int display, limit;
    char extra;
    bool valid;

    if (!config)
        return false;
    valid = fscanf(config, "%d %d %c", &display, &limit, &extra) == 2 &&
            display >= 50 && display <= 100 && limit >= 0 && limit <= 100;
    fclose(config);
    if (!valid || limit == cm->charge_limit_percent)
        return false;

    cm->charge_limit_percent = limit;
    return true;
}

void mudi7_watchdog_state(enum mudi7_watchdog_state state)
{
    atomic_store_explicit(&watchdog_state, state, memory_order_relaxed);
}

void mudi7_publish_battery_state(const struct qb_manager *cm, bool online,
                                 bool telemetry_valid)
{
    int fd;
    FILE *out;
    bool written;

    mkdir(MUDI7_RUN, 0755);
    fd = open(MUDI7_TEMP, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC |
              O_NOFOLLOW, 0644);
    if (fd < 0)
        return;
    out = fdopen(fd, "w");
    if (!out) {
        close(fd);
        unlink(MUDI7_TEMP);
        return;
    }
    written = fprintf(out,
        "{\"pid\":%ld,\"updated\":%lld,\"gauge_stop\":%d,"
        "\"blocked\":%d,\"online\":%d,\"valid\":%d,\"watchdog\":%d}\n",
        (long)getpid(), (long long)time(NULL), cm->charge_limit_percent,
        cm->capacity_charge_hold && online, online, telemetry_valid,
        atomic_load_explicit(&watchdog_state, memory_order_relaxed)) >= 0;
    if (fclose(out) == 0 && written && rename(MUDI7_TEMP, MUDI7_STATUS) == 0)
        return;
    unlink(MUDI7_TEMP);
}
