#!/usr/bin/env bash
set -u
set -o pipefail

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
# shellcheck source=/dev/null
. "$(dirname "$0")/dfr-readiness.sh"

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

# Success path: emulate deauthorization, then a config-2 DFR claim and device.
device="$ROOT/1-3"
mkdir -p "$device/1-3:2.3"
printf '0\n' > "$device/authorized"
printf '0\n' > "$device/bConfigurationValue"
wait_for_deauthorized "$device" 200 10 || fail 'deauthorization success path'
printf '2\n' > "$device/bConfigurationValue"
ln -s '/bus/usb/drivers/barkeep-dfr' "$device/1-3:2.3/driver"
touch "$ROOT/dfr0"
wait_for_display_ready "$device" "$ROOT/dfr0" 200 10 || fail 'display readiness success path'

# Timeout path: no config-2 claim, no driver, no device node.
timeout_device="$ROOT/timeout-device"
mkdir -p "$timeout_device"
printf '1\n' > "$timeout_device/authorized"
printf '1\n' > "$timeout_device/bConfigurationValue"
if wait_for_deauthorized "$timeout_device" 120 10; then
    fail 'deauthorization timeout unexpectedly succeeded'
fi
if wait_for_display_ready "$timeout_device" "$ROOT/missing-dfr0" 120 10; then
    fail 'display readiness timeout unexpectedly succeeded'
fi

printf 'PASS: success and timeout readiness paths\n'
