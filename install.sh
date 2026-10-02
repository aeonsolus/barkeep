#!/usr/bin/env bash
# barkeep installer.
#   sudo ./install.sh              install (DKMS modules + scripts + systemd)
#   sudo ./install.sh uninstall    remove everything, restore the stock row
#        ./install.sh status       what is installed / running
set -u

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SRC/scripts/ibridge-common.sh"
LIBDIR=/usr/local/lib/barkeep
CFGDIR=/etc/barkeep
UNITS=(barkeep-display.service barkeep-bar.service)
MODULES=(barkeep-cfgsel barkeep-dfr)
VERSION=0.1.0

# Units from the older "make the stock firmware function row work" setup. They
# run /usr/local/bin/touchbar-rebind, which loads apple-ibridge, whose
# appleib_hid_probe() force-selects USB config 1 -- dragging the device right
# back out of display mode. touchbar.service is After=multi-user.target, so it
# lands AFTER our display unit and silently undoes it: config drops to 1, the
# panel-enable HID report finds no hidraw, and the bar renders into a dead
# session. They are mutually exclusive with this package.
LEGACY=(touchbar.service touchbar-resume.service)

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
info() { echo -e "${G}[*]${N} $*"; }
warn() { echo -e "${Y}[!]${N} $*"; }
die()  { echo -e "${R}[x]${N} $*"; exit 1; }

need_root() { [ "$EUID" -eq 0 ] || die "run as root: sudo $0 ${1:-install}"; }

check_hw() {
    local d
    d=$(ibridge_path) && info "found Apple iBridge at $d" \
        || warn "no Apple iBridge (05ac:8600) found - is this a T1 MacBook?"
}

check_deps() {
    local miss=()
    command -v dkms  >/dev/null || miss+=(dkms)
    command -v gcc   >/dev/null || miss+=(gcc)
    [ -d "/lib/modules/$(uname -r)/build" ] || miss+=("linux-headers-$(uname -r)")
    python3 -c "import PIL"   2>/dev/null || miss+=(python-pillow)
    python3 -c "import evdev" 2>/dev/null || miss+=(python-evdev)
    [ ${#miss[@]} -eq 0 ] || die "missing: ${miss[*]}
  Arch:   pacman -S dkms gcc linux-headers python-pillow python-evdev
  Debian: apt install dkms build-essential linux-headers-\$(uname -r) python3-pil python3-evdev"
    command -v grim >/dev/null || warn "grim not found - '--source screen' will fall back to theme"
}

# A hand-installed .ko in /lib/modules/<ver>/extra blocks `dkms install`
# ("already installed (unversioned module)"). Clear those first.
clean_manual_modules() {
    local f found=0
    for f in /lib/modules/*/extra/barkeep-cfgsel.ko* /lib/modules/*/extra/barkeep-dfr.ko*; do
        [ -e "$f" ] || continue
        warn "removing hand-installed $f"
        rm -f "$f"; found=1
    done
    [ $found -eq 1 ] && depmod -a
    return 0
}

install_dkms() {
    local name src ver
    clean_manual_modules
    for pair in "barkeep-cfgsel:barkeep-cfgsel" "barkeep-dfr:barkeep-dfr"; do
        src="${pair%%:*}"; name="${pair##*:}"
        # Remove EVERY registered version, not just $VERSION: a version bump
        # would otherwise leave the old one installed and autoinstalling.
        dkms status "$name" 2>/dev/null | while read -r line; do
            v=$(echo "$line" | sed -n "s|^$name[/,] *\([^,:]*\).*|\1|p")
            [ -n "$v" ] || continue
            warn "removing existing $name/$v"
            dkms remove "$name/$v" --all >/dev/null 2>&1 || true
        done
        rm -rf /usr/src/${name}-*
        rm -rf "/usr/src/${name}-${VERSION}"
        mkdir -p "/usr/src/${name}-${VERSION}"
        cp "$SRC/$src"/*.c "$SRC/$src"/Makefile "$SRC/$src"/dkms.conf \
           "/usr/src/${name}-${VERSION}/"
        info "dkms add/build/install $name"
        dkms add     -m "$name" -v "$VERSION" >/dev/null || die "dkms add $name failed"
        dkms build   -m "$name" -v "$VERSION" >/dev/null || die "dkms build $name failed"
        dkms install -m "$name" -v "$VERSION" >/dev/null || die "dkms install $name failed"
    done
}

install_files() {
    info "installing scripts to $LIBDIR"
    install -d "$LIBDIR" "$CFGDIR"
    install -m755 "$SRC/scripts/dfr-bar.py"   "$LIBDIR/"
    install -m755 "$SRC/scripts/dfr-play.py"  "$LIBDIR/"
    install -m755 "$SRC/scripts/dispon.py"    "$LIBDIR/"
    install -m755 "$SRC/scripts/dfr-readiness.sh" "$LIBDIR/"
    install -m644 "$SRC/scripts/ibridge-common.sh" "$LIBDIR/"
    install -m644 "$SRC/scripts/t1hid.py"          "$LIBDIR/"
    # systemd ExecCondition: skip the unit cleanly (not "failed") on a machine
    # that has no iBridge, e.g. a shared config or the wrong Mac.
    cat > "$LIBDIR/have-ibridge.sh" <<EOS
#!/usr/bin/env bash
. "$LIBDIR/ibridge-common.sh"
ibridge_path >/dev/null
EOS
    # The bring-up/teardown scripts are generated fresh here so they run against
    # the DKMS-installed modules rather than a source tree that may move.
    cat > "$LIBDIR/dfr-up.sh" <<EOS
#!/usr/bin/env bash
# Bring up the Touch Bar display session (USB configuration 2).
. "$LIBDIR/ibridge-common.sh"
. "$LIBDIR/dfr-readiness.sh"
D=\$(ibridge_path_or_die) || exit 1
EOS
    cat >> "$LIBDIR/dfr-up.sh" <<'EOS'
USB_DEAUTH_TIMEOUT_MS=${USB_DEAUTH_TIMEOUT_MS:-1000}
DFR_READY_TIMEOUT_MS=${DFR_READY_TIMEOUT_MS:-3000}
POLL_INTERVAL_MS=${POLL_INTERVAL_MS:-20}
for m in apple_ib_tb apple_ib_als apple_ibridge; do rmmod $m 2>/dev/null; done
rmmod barkeep_dfr 2>/dev/null; rmmod barkeep_cfgsel 2>/dev/null
modprobe barkeep_cfgsel config=1 || exit 1
# explicit params so this never depends on module defaults
# colr/colg/colb=0: the module fills frames from load until the UI takes over,
# so a non-black default flashes that colour on every start.
modprobe barkeep_dfr rect_w=2170 bpp=3 fbmode=1 period=1 colr=0 colg=0 colb=0 || exit 1
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
cfg=$(cat "$D/bConfigurationValue" 2>/dev/null)
echo "config=$cfg (want 2)"
[ "$cfg" = "2" ] || { echo "failed to enter config 2"; exit 1; }
# The panel is deliberately left OFF here. dfr-bar.py turns it on immediately
# after writing its first real frame, so the bar lights up already showing the
# UI instead of flashing the module's placeholder fill first.
echo "display session up (panel lit by the UI)"
EOS
    cat > "$LIBDIR/dfr-reset.sh" <<EOS
#!/usr/bin/env bash
# Restore the stock firmware function row. Every step is time-bounded: this
# runs as ExecStop, and anything that hangs here gets SIGTERMed half-done and
# leaves the panel dark.
. "$LIBDIR/ibridge-common.sh"
D=\$(ibridge_path) || exit 0
EOS
    cat >> "$LIBDIR/dfr-reset.sh" <<'EOS'
pkill -f dfr-bar.py 2>/dev/null
sleep 1
timeout 10 rmmod barkeep_dfr 2>/dev/null
timeout 10 rmmod barkeep_cfgsel 2>/dev/null
if [ "$(timeout 5 cat $D/bConfigurationValue 2>/dev/null)" != "1" ]; then
    timeout 15 sh -c "echo 1 > $D/bConfigurationValue" 2>/dev/null
    sleep 3
fi
timeout 5 sh -c "echo on > $D/power/control" 2>/dev/null
timeout 15 modprobe apple-ibridge 2>/dev/null
timeout 15 modprobe apple-ib-tb   2>/dev/null
timeout 15 modprobe apple-ib-als  2>/dev/null
[ -x /usr/local/bin/touchbar-rebind ] && timeout 30 /usr/local/bin/touchbar-rebind >/dev/null 2>&1
echo "config=$(timeout 5 cat $D/bConfigurationValue 2>/dev/null)"
EOS
    chmod 755 "$LIBDIR/dfr-up.sh" "$LIBDIR/dfr-reset.sh" "$LIBDIR/have-ibridge.sh"
    [ -f "$CFGDIR/config" ] && warn "keeping existing $CFGDIR/config" \
                            || install -m644 "$SRC/etc/config" "$CFGDIR/config"
    # convenience CLI
    cat > /usr/local/bin/barkeep <<'EOS'
#!/usr/bin/env bash
case "${1:-}" in
  start)  systemctl start  barkeep-display barkeep-bar ;;
  stop)   systemctl stop   barkeep-bar barkeep-display ;;
  status) systemctl --no-pager status barkeep-display barkeep-bar ;;
  play)   shift; python3 /usr/local/lib/barkeep/dfr-play.py "$@" ;;
  brightness|bright)
          shift
          python3 -c "
import sys; sys.path.insert(0, '/usr/local/lib/barkeep')
import t1hid
a = sys.argv[1] if len(sys.argv) > 1 else None
if a is None:
    lo, hi, auto, ok = t1hid.caps()
    if not ok:
        sys.exit('cannot read the panel (needs root): try sudo barkeep brightness')
    print(f'nits {lo}-{hi}, auto={auto}')
elif a == 'auto':
    print('auto:', t1hid.set_auto(True))
else:
    print(f'{a}%:', t1hid.set_percent(float(a)))
" "$@" ;;
  preview) shift; python3 /usr/local/lib/barkeep/dfr-bar.py --preview "$@" ;;
  *) echo "usage: barkeep {start|stop|status|play <file>|brightness [0-100|auto]|preview <out.png> [--preview-layer NAME]}" ;;
esac
EOS
    chmod 755 /usr/local/bin/barkeep
    # Sleep hook: tear the display session down BEFORE the kernel suspends.
    # barkeep-dfr streams frames continuously to hold the session open, and a USB
    # driver still submitting URBs while the USB core is quiescing the device
    # wedges the suspend - the machine stops at "PM: suspend entry (deep)" and
    # never comes back. systemd waits for this hook, so by the time the kernel
    # starts suspending there is no module bound to the device at all.
    install -d /usr/lib/systemd/system-sleep
    install -m755 "$SRC/systemd/barkeep-sleep.sh" /usr/lib/systemd/system-sleep/barkeep
}

# Disable the legacy stock-row units and remember which ones we touched, so
# uninstall can put them back exactly as they were.
disable_legacy() {
    local u recorded=()
    for u in "${LEGACY[@]}"; do
        [ "$(systemctl is-enabled "$u" 2>/dev/null)" = "enabled" ] || continue
        warn "disabling $u - it re-loads apple-ibridge and forces USB config 1"
        systemctl disable --now "$u" >/dev/null 2>&1 || true
        recorded+=("$u")
    done
    if [ ${#recorded[@]} -gt 0 ]; then
        printf '%s\n' "${recorded[@]}" > "$CFGDIR/legacy-disabled"
        info "recorded in $CFGDIR/legacy-disabled (uninstall re-enables them)"
    fi
}

restore_legacy() {
    local f="$CFGDIR/legacy-disabled" u
    [ -f "$f" ] || return 0
    while read -r u; do
        [ -n "$u" ] || continue
        [ -f "/etc/systemd/system/$u" ] || continue
        info "re-enabling $u"
        systemctl enable "$u" >/dev/null 2>&1 || true
    done < "$f"
    rm -f "$f"
}

install_units() {
    info "installing systemd units"
    for u in "${UNITS[@]}"; do install -m644 "$SRC/systemd/$u" "/etc/systemd/system/$u"; done
    systemctl daemon-reload
    disable_legacy
    systemctl enable "${UNITS[@]}" >/dev/null
}

do_install() {
    need_root install
    check_hw
    check_deps
    install_dkms
    install_files
    install_units
    echo
    info "installed. start now with:  sudo systemctl start barkeep-display barkeep-bar"
    info "or just reboot - it is enabled at boot."
    warn "NOTE: in display mode apple-ibridge is unloaded, so the stock firmware"
    warn "      function row is replaced by this one. 'sudo ./install.sh uninstall' reverts."
    echo "config: $CFGDIR/config   cli: barkeep {start|stop|status|play ...}"
}

do_uninstall() {
    need_root uninstall
    info "stopping and disabling units"
    systemctl disable --now "${UNITS[@]}" barkeep-resume.service >/dev/null 2>&1 || true
    rm -f "${UNITS[@]/#//etc/systemd/system/}" /etc/systemd/system/barkeep-resume.service
    systemctl daemon-reload
    for name in "${MODULES[@]}"; do
        dkms status "$name/$VERSION" >/dev/null 2>&1 && {
            info "removing dkms $name"
            dkms remove "$name/$VERSION" --all >/dev/null 2>&1 || true
        }
        rm -rf "/usr/src/${name}-${VERSION}"
    done
    info "restoring the stock function row"
    [ -x "$LIBDIR/dfr-reset.sh" ] && "$LIBDIR/dfr-reset.sh" || true
    restore_legacy
    systemctl daemon-reload
    rm -rf "$LIBDIR" /usr/local/bin/barkeep /usr/lib/systemd/system-sleep/barkeep
    warn "left $CFGDIR alone (your settings); remove it by hand if you want"
    info "done - reboot for a fully clean state"
}

do_status() {
    echo "modules (dkms):"
    for name in "${MODULES[@]}"; do
        st=$(dkms status "$name/$VERSION" 2>/dev/null | head -1)
        printf "  %-16s %s\n" "$name" "${st:-not installed}"
    done
    echo "loaded:"
    lsmod | grep -E '^(barkeep_cfgsel|barkeep_dfr|apple_ib)' | awk '{printf "  %-16s refcount %s\n",$1,$3}' || echo "  none"
    echo "units:"
    for u in "${UNITS[@]}"; do
        en=$(systemctl is-enabled "$u" 2>/dev/null | head -1); en=${en:--}
        ac=$(systemctl is-active  "$u" 2>/dev/null | head -1); ac=${ac:--}
        printf "  %-32s %s / %s\n" "$u" "$en" "$ac"
    done
    local D
    D=$(ibridge_path) || D=""
    echo "device:"
    echo "  path=${D:-not found}"
    echo "  config=$(cat "$D/bConfigurationValue" 2>/dev/null || echo '?') (2 = display mode)"
    echo "  /dev/dfr0 $([ -e /dev/dfr0 ] && echo present || echo missing)"
    for u in "${LEGACY[@]}"; do
        [ "$(systemctl is-enabled "$u" 2>/dev/null)" = "enabled" ] && \
            warn "$u is ENABLED - it will force USB config 1 and blank the bar."
    done
    return 0
}

case "${1:-install}" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
    status)    do_status ;;
    *) echo "usage: $0 {install|uninstall|status}"; exit 1 ;;
esac
