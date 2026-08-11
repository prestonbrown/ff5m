#!/bin/sh

## Bounded printer resource sampling for host-orchestrated diagnostics.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

output=${1:-}
interval=${2:-1}
duration=${3:-3600}

case "$output" in
    /data/feather-ui-tests/host-monitor-[0-9]*.tsv) ;;
    *) exit 2 ;;
esac
case "$interval:$duration" in
    *[!0-9.:]*) exit 2 ;;
esac

mkdir -p /data/feather-ui-tests || exit 1
printf 'epoch\tuptime\tload1\tmem_available_kb\tswap_free_kb\trole\tpid\tcpu_ticks\trss_kb\tstate\tthreads\tvoluntary_ctxt_switches\tnonvoluntary_ctxt_switches\tminor_faults\tmajor_faults\tscheduler_runtime_ns\tscheduler_wait_ns\tscheduler_timeslices\tread_bytes\twrite_bytes\tsyscr\tsyscw\twchan\tcommand\n' > "$output" || exit 1

start=$(date +%s)
trap 'exit 0' HUP INT TERM

while :; do
    epoch=$(date +%s)
    elapsed=$((epoch - start))
    [ "$elapsed" -ge "$duration" ] && break
    uptime=$(awk '{print $1}' /proc/uptime 2>/dev/null)
    load1=$(awk '{print $1}' /proc/loadavg 2>/dev/null)
    mem_available=$(awk '
        /^MemAvailable:/ {print $2; found=1}
        END {if (!found) print 0}' /proc/meminfo 2>/dev/null)
    swap_free=$(awk '/^SwapFree:/ {print $2}' /proc/meminfo 2>/dev/null)
    total_ticks=$(awk '
        /^cpu / {sum=0; for (i=2; i<=NF; i++) sum+=$i; print sum; exit}' \
        /proc/stat 2>/dev/null)
    printf '%s\t%s\t%s\t%s\t%s\tsystem\t0\t%s\t0\t-\t0\t0\t0\t0\t0\t0\t0\t0\t0\t0\t0\t0\t-\t-\n' \
        "$epoch" "${uptime:-0}" "${load1:-0}" \
        "${mem_available:-0}" "${swap_free:-0}" "${total_ticks:-0}" \
        >> "$output"

    for procdir in /proc/[0-9]*; do
        [ -r "$procdir/stat" ] || continue
        pid=${procdir#/proc/}
        IFS= read -r process_name < "$procdir/comm" 2>/dev/null
        command=$process_name
        case "$process_name" in
            *dropbear*) role=dropbear ;;
            *Typer*|*typer*) role=typer ;;
            python*|Python*)
                command=$(tr '\000\t\n' '   ' \
                    < "$procdir/cmdline" 2>/dev/null)
                case "$command" in
                    *klippy.py*) role=klippy ;;
                    *moonraker.py*) role=moonraker ;;
                    *) continue ;;
                esac
                ;;
            *)
                continue
                ;;
        esac
        IFS= read -r statline < "$procdir/stat" 2>/dev/null
        [ -n "$statline" ] || continue
        fields=${statline#*) }
        set -- $fields
        state=${1:--}
        minor_faults=${8:-0}
        major_faults=${10:-0}
        user_ticks=${12:-0}
        system_ticks=${13:-0}
        threads=${18:-0}
        cpu_ticks=$((user_ticks + system_ticks))
        status_values=$(awk '
            /^VmRSS:/ {rss=$2}
            /^voluntary_ctxt_switches:/ {voluntary=$2}
            /^nonvoluntary_ctxt_switches:/ {nonvoluntary=$2}
            END {printf "%d %d %d", rss, voluntary, nonvoluntary}' \
            "$procdir/status" 2>/dev/null)
        set -- ${status_values:-0 0 0}
        rss=${1:-0}
        voluntary=${2:-0}
        nonvoluntary=${3:-0}
        sched_runtime=0
        sched_wait=0
        sched_timeslices=0
        if [ -r "$procdir/schedstat" ]; then
            IFS=' ' read -r sched_runtime sched_wait sched_timeslices \
                < "$procdir/schedstat" 2>/dev/null
        fi
        io_values=$(awk '
            /^read_bytes:/ {read_bytes=$2}
            /^write_bytes:/ {write_bytes=$2}
            /^syscr:/ {syscr=$2}
            /^syscw:/ {syscw=$2}
            END {printf "%d %d %d %d", read_bytes, write_bytes, syscr, syscw}' \
            "$procdir/io" 2>/dev/null)
        set -- ${io_values:-0 0 0 0}
        read_bytes=${1:-0}
        write_bytes=${2:-0}
        syscr=${3:-0}
        syscw=${4:-0}
        wchan=-
        if [ -r "$procdir/wchan" ]; then
            IFS= read -r wchan < "$procdir/wchan" 2>/dev/null
        fi
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$epoch" "${uptime:-0}" "${load1:-0}" \
            "${mem_available:-0}" "${swap_free:-0}" "$role" "$pid" \
            "$cpu_ticks" "$rss" "$state" "$threads" "$voluntary" \
            "$nonvoluntary" "$minor_faults" "$major_faults" \
            "${sched_runtime:-0}" "${sched_wait:-0}" \
            "${sched_timeslices:-0}" "$read_bytes" "$write_bytes" \
            "$syscr" "$syscw" "${wchan:--}" "$command" >> "$output"
    done
    sleep "$interval" || break
done
