#!/usr/bin/env bash
# Bring up the Touch Bar display session and leave it running.
# Then push frames with:  scripts/dfr-play.py test | bars | FILE
. "$(dirname "$0")/ibridge-common.sh"
. "$(dirname "$0")/dfr-readiness.sh"
D=$(ibridge_path_or_die) || exit 1
R="$(cd "$(dirname "$0")/.." && pwd)"

# Keep the USB 0/2 sequence, but do not spend fixed sleeps on hardware that is
# already ready.  The final bound is deliberately explicit: a timeout fails
# closed instead of handing an unready DFR session to the panel-enable step.
USB_DEAUTH_TIMEOUT_MS=${USB_DEAUTH_TIMEOUT_MS:-1000}
DFR_READY_TIMEOUT_MS=${DFR_READY_TIMEOUT_MS:-3000}
POLL_INTERVAL_MS=${POLL_INTERVAL_MS:-20}
for m in apple_ib_tb apple_ib_als apple_ibridge; do rmmod $m 2>/dev/null; done
rmmod barkeep_dfr 2>/dev/null; rmmod barkeep_cfgsel 2>/dev/null
insmod "$R/barkeep-cfgsel/barkeep-cfgsel.ko" config=1 || exit 1
insmod "$R/barkeep-dfr/barkeep-dfr.ko" rect_w=2170 bpp=3 fbmode=1 period=1 colr=0 colg=0 colb=0 || exit 1
echo 2 > /sys/module/barkeep_cfgsel/parameters/config
echo 0 > "$D/authorized"
wait_for_deauthorized "$D" "$USB_DEAUTH_TIMEOUT_MS" "$POLL_INTERVAL_MS" || {
    echo "failed to observe USB deauthorization within ${USB_DEAUTH_TIMEOUT_MS}ms" >&2
    exit 1
}
echo 1 > "$D/authorized"
wait_for_display_ready "$D" /dev/dfr0 "$DFR_READY_TIMEOUT_MS" "$POLL_INTERVAL_MS" || {
    echo "DFR readiness timeout after ${DFR_READY_TIMEOUT_MS}ms (config 2, barkeep-dfr, /dev/dfr0)" >&2
    exit 1
}
echo "cfg=[$(cat "$D/bConfigurationValue" 2>/dev/null)] (want 2)"
python3 "$R/scripts/dispon.py" && echo "panel ON"
ls -l /dev/dfr0 2>/dev/null || echo "WARNING: /dev/dfr0 missing"
echo "ready - push frames with: python3 $R/scripts/dfr-play.py test"
