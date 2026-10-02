#!/usr/bin/env bash
# Roll the Touch Bar back from barkeep to the previous tiny-dfr stack.
# Created 2026-09-28, immediately before installing barkeep as the default.
#
#   sudo ./rollback-to-tiny-dfr.sh
#
# Everything barkeep owns is removed, then the pre-existing tiny-dfr
# configuration, binaries and units captured in this directory are restored.
# The appletbdrm-t1 DKMS module that tiny-dfr needs was never touched by
# barkeep, so it should still be in place; the script rebuilds it anyway.
set -euo pipefail

# The artifacts this restores live in the directory that holds the OTHER copy
# of this script, not necessarily next to this one. Prefer an explicit BK,
# else use this script's own directory.
BK="${BK:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
[ -d "$BK" ] || { echo "backup directory not found: $BK" >&2; exit 1; }

echo "==> stopping and disabling barkeep"
systemctl disable --now barkeep-bar.service barkeep-display.service 2>/dev/null || true
rm -f /etc/systemd/system/barkeep-resume.service
rm -f /usr/lib/systemd/system-sleep/barkeep
systemctl daemon-reload

echo "==> removing barkeep DKMS modules"
for name in barkeep-cfgsel barkeep-dfr; do
    dkms status "$name/0.1.0" >/dev/null 2>&1 && dkms remove "$name/0.1.0" --all || true
    rm -rf "/usr/src/${name}-0.1.0"
done
rm -rf /usr/local/lib/barkeep /usr/local/bin/barkeep /etc/barkeep

echo "==> restoring tiny-dfr configuration and binaries"
# $BK/tiny-dfr is the /etc/tiny-dfr CONFIG directory (layouts, SVGs, TOML) and
# is not the renderer. The renderer is a separate file in this backup dir.
cp -a "$BK/tiny-dfr" /etc/
cp -a "$BK/tiny-dfr.service" /etc/systemd/system/
cp -a "$BK/touchbar-t1.service" /etc/systemd/system/
cp -a "$BK/touchbar-t1-appmap.service" /etc/systemd/system/
install -m755 "$BK/tiny-dfr.bin" /usr/local/bin/tiny-dfr
install -m755 "$BK/touchbar-t1-appmap" /usr/local/bin/touchbar-t1-appmap
install -m755 "$BK/touchbar-t1-gen-seat-rules" /usr/local/bin/touchbar-t1-gen-seat-rules

echo "==> making sure the T1 display module is present"
dkms status appletbdrm-t1/1.0 >/dev/null 2>&1 || dkms install appletbdrm-t1/1.0
depmod -a

echo "==> re-enabling and starting tiny-dfr"
systemctl daemon-reload
systemctl enable --now tiny-dfr.service
systemctl enable    touchbar-t1.service touchbar-t1-appmap.service
systemctl start     touchbar-t1.service touchbar-t1-appmap.service

echo
echo "==> status"
systemctl --no-pager --lines=0 status tiny-dfr.service touchbar-t1-appmap.service || true
echo
echo "Done. Verify the Touch Bar shows the F-key row, then optionally reboot."
echo "The pre-barkeep barkeep trial renderer was: $BK/dfr-bar.py.tuned"
