#!/system/bin/sh
# Read-only CPU telemetry bridge for Chelus. Started by an authorized ADB shell.
# No agent commands are accepted. Nothing touches app secrets or conversations.
# This is per-boot only; it does not install an Android service.
set -u
PATH=/system/bin:/system/xbin
out=/data/local/tmp/chelus-telemetry.json
pidfile=/data/local/tmp/chelus-telemetry.pid
umask 022
if [ -r "$pidfile" ]; then
    read -r oldpid < "$pidfile"
    case "$oldpid" in
        ''|*[!0-9]*) ;;
        *) if kill -0 "$oldpid" 2>/dev/null; then
               echo 'A telemetry sampler is already running.' >&2
               exit 1
           fi ;;
    esac
fi
printf '%s\n' "$$" > "$pidfile"
chmod 600 "$pidfile"
cleanup() {
    rm -f "$out.$$"
    if [ -r "$pidfile" ]; then
        read -r owner < "$pidfile"
        if [ "$owner" = "$$" ]; then rm -f "$pidfile" "$out"; fi
    fi
}
trap cleanup EXIT
trap 'exit 0' TERM INT HUP
prev_total=0
prev_idle=0
cpu_temp=null
battery_percent=null
battery_temp=null
temp_ticks=6
while :; do
    # Linux aggregate fields 1..8: guest time is already counted in user/nice.
    counters=$(awk '/^cpu / { total=0; for(i=2;i<=9;i++)total+=$i; printf "%.0f %.0f\n",total,$5+$6; exit }' /proc/stat 2>/dev/null)
    cpu=null
    if [ -n "$counters" ]; then
        set -- $counters
        total=$1
        idle=$2
        if [ "$prev_total" -gt 0 ] && [ "$total" -gt "$prev_total" ]; then
            dt=$((total-prev_total))
            di=$((idle-prev_idle))
            cpu=$(awk -v t="$dt" -v i="$di" 'BEGIN { p=100*(t-i)/t; if(p<0)p=0; if(p>100)p=100; printf "%.1f",p }')
        fi
        prev_total=$total
        prev_idle=$idle
    fi
    # Ask the HAL for the CPU-class sensor (mType=0), not the battery sensor.
    # Thirty-second temperature cadence avoids repeated Binder calls per reply.
    if [ "$temp_ticks" -ge 6 ]; then
        temp=$(timeout 4 dumpsys thermalservice 2>/dev/null | awk '
            /Current temperatures from HAL:/ { live=1; next }
            live && /mType=0,/ {
                if(match($0,/mValue=[-0-9.]+/)) {
                    v=substr($0,RSTART+7,RLENGTH-7)
                    if(v+0>=-20 && v+0<=150) print v
                    exit
                }
            }')
        cpu_temp=${temp:-null}
        battery=$(timeout 4 dumpsys battery 2>/dev/null | awk '
            /level:/ { level=$2 }
            /scale:/ { scale=$2 }
            /temperature:/ { temp=$2 }
            END {
                percent="null"; celsius="null"
                if(level ~ /^[0-9]+$/ && scale ~ /^[0-9]+$/ && scale>0 && level<=scale)
                    percent=sprintf("%.0f",100*level/scale)
                if(temp ~ /^-?[0-9]+$/ && temp/10>=-20 && temp/10<=100)
                    celsius=sprintf("%.1f",temp/10)
                printf "%s %s",percent,celsius
            }')
        if [ -n "$battery" ]; then
            set -- $battery
            battery_percent=$1
            battery_temp=$2
        else
            battery_percent=null
            battery_temp=null
        fi
        temp_ticks=0
    fi
    uptime=$(awk '{ if($1 ~ /^[0-9]+([.][0-9]+)?$/) printf "%.1f",$1; exit }' /proc/uptime 2>/dev/null)
    uptime=${uptime:-null}
    now=$(date +%s)
    printf '{"version":1,"timestamp":%s,"cpu_percent":%s,"cpu_temp_c":%s,"battery_percent":%s,"battery_temp_c":%s,"uptime_seconds":%s}\n' "$now" "$cpu" "$cpu_temp" "$battery_percent" "$battery_temp" "$uptime" > "$out.$$"
    chmod 644 "$out.$$"
    mv -f "$out.$$" "$out"
    temp_ticks=$((temp_ticks+1))
    sleep 5
done
