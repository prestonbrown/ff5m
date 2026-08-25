#!/bin/sh

## Launch Klipper, optionally under SCHED_RR real-time scheduling.
##
## Copyright (C) 2025, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

## Gated by the `klipper_rt` mod parameter (default 0/off). When enabled, the
## klippy host is launched under SCHED_RR, so the process and every thread it
## spawns (threads inherit the parent's scheduling policy) run real-time. On the
## dual-core T113 this keeps timing-critical MCU communication from being
## preempted by Moonraker, the web UI, the camera stream, etc., which is the
## usual cause of "Timer too close" / MCU timeout underruns under load.
##
## A low priority (5) plus the kernel RT throttle (sched_rt_runtime_us, 95% by
## default) ensures a misbehaving klippy can never hard-lock a core.

# platform.sh, not common.sh: this script is #!/bin/sh and runs under
# BusyBox ash, which cannot parse common.sh. Sourcing that here would
# abort the script and Klipper would never start.
# shellcheck disable=SC1090,SC1091
. /opt/config/mod/.shell/platform.sh

CFG_SCRIPT="/opt/config/mod/.shell/commands/zconf.sh"
VAR_PATH="/opt/config/mod_data/variables.cfg"
KLIPPER_START="$KLIPPER_DIR/start.sh"
RT_PRIO=5

rt=$("$CFG_SCRIPT" "$VAR_PATH" --get "klipper_rt" "0")

if [ "$rt" = "1" ] && command -v chrt >/dev/null 2>&1; then
    echo "// Klipper: real-time scheduling (SCHED_RR, priority $RT_PRIO)"
    exec chrt -r "$RT_PRIO" "$KLIPPER_START"
fi

exec "$KLIPPER_START"
