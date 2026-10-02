#!/usr/bin/env bash
# Hardware-free-testable readiness checks for dfr-up.sh.
# These checks observe USB/sysfs and the DFR driver's device node only; they do
# not write to hardware and never replace the required USB 0/2 sequence.

_wait_for_value() {
    local path=$1 expected=$2 timeout_ms=$3 interval_ms=$4
    local deadline now value
    deadline=$(( $(date +%s%3N) + timeout_ms ))
    while :; do
        if [ -r "$path" ]; then
            value=$(<"$path")
            [ "$value" = "$expected" ] && return 0
        fi
        now=$(date +%s%3N)
        [ "$now" -ge "$deadline" ] && return 1
        sleep "0.$(printf '%03d' "$interval_ms")"
    done
}

wait_for_deauthorized() {
    local device=$1 timeout_ms=${2:-1000} interval_ms=${3:-20}
    local deadline now authorized
    deadline=$(( $(date +%s%3N) + timeout_ms ))
    while :; do
        authorized=1
        [ -r "$device/authorized" ] && authorized=$(<"$device/authorized")
        [ "$authorized" = 0 ] && return 0
        now=$(date +%s%3N)
        [ "$now" -ge "$deadline" ] && return 1
        sleep "0.$(printf '%03d' "$interval_ms")"
    done
}

_dfr_driver_bound() {
    local driver target
    for driver in "$1"/*/driver; do
        [ -L "$driver" ] || continue
        target=$(readlink "$driver")
        case "$target" in
            *barkeep-dfr) return 0 ;;
        esac
    done
    return 1
}

wait_for_display_ready() {
    local device=$1 devnode=${2:-/dev/dfr0} timeout_ms=${3:-3000} interval_ms=${4:-20}
    local deadline now config
    deadline=$(( $(date +%s%3N) + timeout_ms ))
    while :; do
        config=
        [ -r "$device/bConfigurationValue" ] && config=$(<"$device/bConfigurationValue")
        if [ "$config" = 2 ] && [ -e "$devnode" ] && _dfr_driver_bound "$device"; then
            return 0
        fi
        now=$(date +%s%3N)
        [ "$now" -ge "$deadline" ] && return 1
        sleep "0.$(printf '%03d' "$interval_ms")"
    done
}
