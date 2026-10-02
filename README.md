# barkeep — Apple T1 Touch Bar display driver for Linux

Target: **MacBookPro13,2** (2016 13" Touch Bar), Apple **T1** iBridge, USB `05ac:8600`.

`tiny-dfr` and `hid-appletb-*` are T2/Apple-Silicon only and do nothing on a T1.
**`sunplex07/appletbdrm` does support the T1** — it binds `05ac:8600` and exposes the bar as
a DRM device — as does the kernel driver in `xeeban/macbook-t1-linux`. This project is a
separate implementation with a userspace UI on top; see Credits.

Status: **working, and it survives a reboot.** The bar is a real display — arbitrary images
and 30fps video — plus a full function row on top of it:

- drawn icons over a gradient sampled from what is on screen (or the wallpaper, or the theme)
- **buttons that work** — touch is read from the digitizer and injected as real keys
- **five layers**, selected by Fn plus a modifier — media, F1–F12, a live system
  row, F13–F24 and a transport strip
- **live readouts** — battery from UPower (matching Omarchy's battery UI), wifi, bluetooth and keyboard backlight from `/sys`
- **Touch Bar backlight control**, including handing it back to the ambient light sensor
- **keys and background fade to black after 30s idle**, then fade back on keyboard, mouse or bar activity
- **offline preview** — render any layer to a PNG with no hardware and no root
- DKMS modules + systemd units, so it comes up on every boot

## Layout

    barkeep-cfgsel/   kernel module: selects USB configuration 2 at enumeration
    barkeep-dfr/     kernel module: claims the DFR display interface, speaks the protocol,
                   exposes /dev/dfr0
    scripts/       dfr-bar.py    the function-row UI (icons, touch, Fn, idle fade)
                   dfr-play.py   stills, video and test patterns
                   ibridge-common.sh  locates the iBridge in sysfs by USB id
                   t1hid.py      Touch Bar backlight over HID (interface 6)
                   dfr-up.sh     bring the display session up
                   dfr-reset.sh  put the stock firmware row back, no reboot
                   dispon.py     panel enable on its own
                   build.sh      build both modules out of tree
                   dfr-go.sh     original bring-up + red/green/blue self-test
                   dfr-bar-run.sh  run the UI from the source tree
    systemd/       display + bar units
    etc/config     installed to /etc/barkeep/config
    install.sh     install | uninstall | status
    reference/     imbushuo/DFRDisplayKm — the working Windows driver (MIT). READ THE SOURCE.

## Requirements

    dkms gcc linux-headers-$(uname -r) python-pillow python-evdev

`grim` is optional and only used by `--source screen`; without it that source falls back to
the theme palette. `install.sh` checks all of this before touching anything.

## Install

    sudo ./install.sh              # DKMS modules + scripts + systemd, enabled at boot
    ./install.sh status
    sudo ./install.sh uninstall    # removes everything, restores the stock function row

Config lives in `/etc/barkeep/config`:

    DFR_ARGS="--source screen --flow 15 --fade 2 --poll 10 --threshold 30 --idle 30 --idle-out 2 --idle-in 1"

    sudo systemctl restart barkeep-bar     # after editing

    barkeep start | stop | status
    barkeep play <file|test|bars|flow>
    sudo barkeep brightness            # report the panel's nits range
    sudo barkeep brightness 40         # 0-100 across that range
    sudo barkeep brightness auto       # hand back to the light sensor
    barkeep preview /tmp/bar.png --preview-layer system

Units:

- `barkeep-display.service` — enters USB config 2 and loads the modules
- `barkeep-bar.service` — the UI (requires the display unit)

Modules are DKMS, so they rebuild on kernel updates.

**In display mode `apple-ibridge` is unloaded**, so the stock firmware function row is
replaced by this one. `uninstall` puts it back.

### If you previously set up the stock function row, read this

An earlier `touchbar.service` / `touchbar-resume.service` (running
`/usr/local/bin/touchbar-rebind`) is **mutually exclusive** with display mode. Those units
load `apple-ibridge`, which force-selects USB config 1 — see "What was hard" below — and
`touchbar.service` is `After=multi-user.target`, so at boot it lands *after* the display unit
and silently undoes it.

`install.sh` disables them and records what it disabled in `/etc/barkeep/legacy-disabled`;
`uninstall` re-enables them. The display unit also declares `Conflicts=` on both, so starting
one by hand cannot quietly blank the bar. `./install.sh status` warns if either is enabled.

## Run from the source tree

    bash scripts/build.sh
    sudo bash scripts/dfr-up.sh             # bring up the session, leave it running
    python3 scripts/dfr-play.py test        # gradient
    python3 scripts/dfr-play.py bars        # colour bars
    python3 scripts/dfr-play.py pic.png     # a still image
    python3 scripts/dfr-play.py clip.mp4    # video, looping (--once, --fps N)
    sudo bash scripts/dfr-bar-run.sh --flow 30   # the function-row UI
    sudo bash scripts/dfr-reset.sh          # restore the stock function row, no reboot

## Troubleshooting

**The bar is black but both units say `active`.** The display session was torn down under a
UI that is happily still rendering into it. Check the bar's log:

    journalctl -u barkeep-bar -b | grep -v Deprecat

These three lines together are the signature:

    panel on showing black (0 report(s))        <- 0, not 1: something took the hidraw
    touch: digitizer hidraw not found (need config 2)
    $ cat /sys/bus/usb/devices/1-3/bConfigurationValue   -> 1

Almost always `apple-ibridge` got loaded by something — see the legacy-units note above.
`lsmod | grep apple_ib` should print nothing while the bar is running.

**Nothing at all, `/dev/dfr0` missing.** The display unit failed to reach config 2:
`journalctl -u barkeep-display -b`. It prints `config=N (want 2)`.

**The display unit is skipped, not failed.** Its `ExecCondition` found no `05ac:8600` in
`/sys/bus/usb/devices/*`. The device is located by USB id, not by a fixed port, so this means
the hardware genuinely is not there. `./install.sh status` prints the path it found.

**The machine hangs on suspend — UNRESOLVED, see "Still to do".** The signature is a log
that simply ends at `PM: suspend entry (deep)` with `barkeep-dfr` traffic on the line before
and nothing after; the machine needs a hard reset. Note that everything after
`printk: Suspending console(s)` is only written to the journal *if the machine resumes*, so
absence of later messages is not evidence about how far it got.

Two mitigations are in place. `barkeep-dfr` implements `.suspend`/`.resume`/`.reset_resume`:
suspend clears the stop flag, cancels the frame work and waits on the URB anchor before
killing what is left; resume redoes the whole handshake, because the panel loses its session.
On top of that, `/usr/lib/systemd/system-sleep/barkeep` tears the display session down
*before* the kernel begins suspending, so nothing is bound to the device at that point.
Neither is confirmed to fix it.

**"another dfr-bar is already running".** Two writers on `/dev/dfr0` fight and the bar
flickers, so the UI takes an exclusive lock on `/run/barkeep.lock`. Stop the service before
running it by hand.

## What was hard, and why

**1. The DFR display lives only in USB configuration 2.** Config 1 (the Linux default) has
*no bulk OUT endpoint at all*. Config 2 interface 3 is `class 0x10 / sub 0 / proto 0`,
bulk OUT `0x02` + IN `0x85`.

**2. Configuration can only be chosen at enumeration.** Userspace `SET_CONFIGURATION`
(sysfs or libusb) never works — Microsoft documents the same rule for `usbccgp`. Linux's
hook is `usb_device_driver.choose_configuration` (+ `generic_subclass = 1`), which is what
`barkeep-cfgsel` uses. Equivalent to macOS `kUSBPreferredConfiguration` and Windows
`OriginalConfigurationValue`.

**3. `apple-ibridge` actively fights this.** `appleib_hid_probe()` does:

    if (udev->actconfig->desc.bConfigurationValue != APPLEIB_BASIC_CONFIG)
            usb_driver_set_configuration(udev, APPLEIB_BASIC_CONFIG);   /* = 1 */

so in config 2 it forces the device straight back to config 1, ~30ms after entry. Found with
ftrace on `usb_driver_set_configuration`. **You must `rmmod apple_ib_tb apple_ib_als
apple_ibridge` first.** Every earlier theory (cdc_ncm, packet rate, URB pools, frame format)
was chasing this one line. It is also why the legacy rebind units break everything.

## Protocol

Generic request, 32 bytes, little-endian:

| off | field | value |
|----|-------|-------|
| 0x00 | RequestHeader | `0x15120002` |
| 0x0C | RequestLength | `0x10` |
| 0x10 | RequestKey | FourCC |
| 0x1C | End | `0x10` |

Keys: `GINF` get info, `REDY` host ready, `CLDR` clear screen, `UDCL` framebuffer updated.
Responses use header `0x01140000`. A response echoes the request header with **bit 31 set**
(`0x15120002` -> `0x95120002`) — that is an ACK, not an error.

Bring-up order (from `Device.c` `EvtDeviceD0Entry`): reset both bulk pipes -> `GINF`
(retry up to 100, the first reply is a short UDCL) -> validate -> `REDY` -> `CLDR`.

GINF reply gives **2170 x 60**, pixel format **ABGR**, plus width/height in inches as floats.

Framebuffer update — ONE bulk write of `[60-byte request][pixels][88-byte padding]`:

| off | field | value |
|----|-------|-------|
| 0x00 | RequestHeader | `0x00120002` |
| 0x04 | Reserved1 | **`0x00000009`** |
| 0x0C | RequestLength | **total - 16** |
| 0x10 | Reserved0 (u16) | **`0x0001`** |
| 0x12 | FrameId (u8) | increments, wraps at 0xff |
| 0x30 | BeginX/BeginY/Width/Height | u16 each |
| 0x38 | BufferSize | W*H*3 |

`DFR_FRAMEBUFFER_PIXEL_BYTES = 3`. The padding is **88 bytes and not zeros** — see
`dfr_update_padding[]`, copied verbatim from `reference/.../DfrDisplay.c`.

Those four bolded values are what made it work, and every one of them was wrong at first
because it came from *summaries* of the Windows driver rather than its source — including an
invented padding length (96, actually 88) and invented contents (zeros, actually a pattern).

> Lesson: read the vendored source directly. Never trust a summary of a binary protocol.

Panel **2170 x 60**, **3 bytes per pixel**, byte order `r,g,b` as written (the "ABGR" name in
the descriptor does not imply a swap). Colours can be changed live:

    echo 255 | sudo tee /sys/module/barkeep_dfr/parameters/colr

## Pushing pixels

`barkeep-dfr` exposes **`/dev/dfr0`**. Write exactly one frame: **2170 x 60 RGB888 =
390600 bytes**. Short writes are zero-padded. The driver re-sends the current buffer
continuously, which is also what holds the display session open.

Anything ffmpeg can decode works:

    ffmpeg -i clip.mp4 -vf scale=2170:60 -f rawvideo -pix_fmt rgb24 - > /dev/dfr0

### Panel orientation

The panel is physically **2170x60 landscape** but the framebuffer is **60 wide x 2170 tall,
rotated 90 degrees** — the wire buffer has 60-pixel rows. `dfr-play.py` transposes
(`ffmpeg ...,transpose=1`). Solid fills and column-uniform patterns look correct either way,
which is why the first colour-bar test passed despite the wrong layout.

## The bar UI

    sudo bash scripts/dfr-bar-run.sh --flow 30      # start it
    sudo python3 scripts/dfr-bar.py --no-touch      # render only
    sudo python3 scripts/dfr-bar.py --wallpaper X.jpg

`dfr-bar.py` draws esc / brightness / mission-control / launchpad / keyboard-illum /
prev-play-next / mute / volume as **vector icons** (font glyph coverage for emoji and media
symbols is unreliable — they render as tofu). `--flow N` drifts the gradient N px/sec.

### Colour sources

    --source screen      (default) colours currently ON SCREEN, via grim
    --source theme       the desktop's own palette (colors.toml) — zero cost, no capture
    --source wallpaper   the backdrop image
    --fade 2 --poll 3 --threshold 18

`screen` samples with `grim` at 5% scale (~0.45 s) straight into memory — **nothing is ever
written to disk**. A palette-distance threshold stops it re-fading on noise (cursor blink, a
scrolling line); only a real shift in what is on screen triggers a change. Be aware it is
capturing your desktop on a timer; `theme` gives a similar effect with no capture at all.
`theme` and `wallpaper` watch mtime/symlink target and re-sample automatically.

On Wayland there is no readable screen buffer — any pixel access goes through a capture
protocol (grim uses `wlr-screencopy`) or the portal. There is no cheaper way to do this.

Getting the palette to look right took two fixes:

- **Merge near-duplicate colours.** Quantizing alone picks up anti-aliased edge pixels — a
  logo outline yields blend shades that exist nowhere as a real region. Colours within a
  distance of 60 are clustered and only the representative kept.
- **Hold each colour, then blend briefly.** Continuous interpolation between N colours means
  most of the bar shows invented in-between shades. Each sampled colour now occupies ~55% of
  its segment before a short smoothstep transition.

### Transitions

Every transition uses one shared `ease()` (smoothstep), so overlapping ones move on the same
curve and read as a single motion:

- **palette change** — cross-fades the whole bar at once (an eased blend of the two 1-row
  gradients), not a left-to-right wipe
- **startup** — the panel is lit while still showing black, then the first frame rises out of
  it, so there is no pop
- **idle auto-hide** — the key overlay fades out and back

A cross-fade starts from *what is on the panel right now*, not from the previous fade's
destination. Screen sampling polls every 3s while a fade runs 2s, so interruptions are
routine; starting from the old target snapped a channel 85/255 in a single frame. Now the
worst frame is 5/255, and it falls at the curve's natural steepest point.

### Idle auto-hide

    --idle 30        hide the keys after 30s with no activity (0 disables)
    --idle-out 2     seconds to fade them away
    --idle-in 1      seconds to bring them back

With the keys hidden only the gradient shows. Activity means a real keyboard or pointer: a
device with a letter key, `EV_REL`, or `EV_ABS` plus a touch/click button — that last case is
the bar's own digitizer, so touching the bar wakes it too. Power/Sleep/Lid/Video Bus carry
`EV_KEY` but are not typing, and the UI's own uinput node is skipped by name so injected keys
cannot wake the bar through a second path.

The opacity ramp advances a phase 0..1 *linearly* by elapsed time and eases the phase, rather
than easing from a fixed start time. That is what lets an interrupted fade reverse from
exactly where it is — type halfway through the fade-out and the keys come back from half
opacity with no jump.

`render()` takes a `buttons` opacity and draws the keys onto a copy of the gradient, then
blends the two. One C-speed composite, and the plates, outlines and glyphs fade together as a
single layer.

### Layers

Fn selects a layer together with whatever modifier is held:

| Hold | Layer | Contents |
|---|---|---|
| — | media | esc, brightness, mission control, launchpad, kbd illum, transport, volume |
| **Fn** | fn | Esc + F1–F12 |
| **Ctrl+Fn** | system | live battery / wifi / bluetooth / kbd-backlight, plus bar brightness |
| **Alt+Fn** | f13-f24 | F13–F24, unbound by default — bind them in your compositor |
| **Meta+Fn** | transport | prev / play / next / mute / volume, larger targets |

The keyboard node is found by capability (it reports `KEY_FN`), not by a fixed
`/dev/input/eventN` — the number moves between boots. The Apple SPI keyboard
**autorepeats Fn while held** (value 2), so treat 1 and 2 as down and 0 as up. If the layer
flips while a finger is down, the old layer's key is released first so it cannot stick.
Reading the keyboard needs no root if you are in the `input` group.

### Live indicators and command keys

A key may carry an indicator, whose label is recomputed every 2.5s, or a command, which it
runs instead of injecting a keycode. Battery percentage uses UPower's aggregate display device
to stay aligned with the Omarchy status bar; radio and keyboard-backlight state come from `/sys`:

| Indicator | Source |
|---|---|
| battery | UPower `DisplayDevice` percentage; falls back to `energy_now/energy_full`, then `charge_now/charge_full`, then `capacity` |
| wifi | `operstate` of the interface that has a `wireless/` directory |
| bluetooth | `/sys/class/rfkill/*` where `type` is `bluetooth` |
| kbd backlight | `/sys/class/leds/*kbd_backlight*/{brightness,max_brightness}` |

Commands run in the **desktop user's** session via `setpriv`, never as root. The system row opens Omarchy's power, network and Bluetooth panels; media-layer Mission Control sends the existing `Super+Tab` binding, and Apps opens the Omarchy menu. Machines without a battery or an rfkill node simply render a plain icon. Edit `KEYS_SYS` in `dfr-bar.py` to change what the row contains.

### Touch Bar backlight

The panel's own backlight is separate from the screen's, and is driven by HID feature reports
on **USB interface 6** — the protocol comes from `appletbdrm`:

| Report | Length | Meaning |
|---|---|---|
| 5 | 116 | AutoBrightness (`byte[3]`: 1 = manual, 2 = ALS) + MinNits/MaxNits at `[4:8]`/`[8:12]` |
| 4 | 14 | absolute nits, u32 LE at `byte[2:6]`, with `byte[1] = 2` |
| 3 | 15 | display state, 1 = off, 2 = on |

**AutoBrightness must be cleared before manual brightness does anything.** This panel reports
a range of **11899–357099** and ships ALS-driven. Reading the reports needs root; without it
`t1hid.caps()` returns `ok=False` rather than handing back firmware defaults that are not
this panel's real range.

### Offline preview

    python3 scripts/dfr-bar.py --preview /tmp/bar.png --preview-layer system

Renders one frame of any layer to an image with no `/dev/dfr0`, no root and no panel. Layers
can be named (`media`, `fn`, `system`, `f13-f24`, `transport`) or given by index. This is the
fastest way to iterate on layout and icons, and it works on a machine with no Touch Bar
at all.

Note that most F-keys do nothing at the desktop level until an app has focus — that is
correct behaviour, not a broken injection. To check the keys really work, watch the uinput
device (`Apple T1 Touch Bar`) with `python-evdev`, or use `wev`, or `cat -v` in a terminal
(`^[OP` = F1).

### Touch

The digitizer is a non-standard HID on **config-2 interface 2, EP 0x83**; reports are ~52
bytes with a **little-endian float32 X in [0.5, 1.0]** in the first 4 bytes. Read via hidraw
*alongside* `hid-generic` (no unbinding), mapped to key zones, injected with uinput. Release
is inferred from a ~120ms gap in reports. Protocol credit: `xeeban/macbook-t1-linux`.

### Two traps worth knowing

- **Under `sudo`, `~` is `/root`** — resolve the desktop user's home via `SUDO_USER`, or the
  wallpaper silently falls back to a default palette.
- **Never build frames pixel-by-pixel in Python.** 2170x60 per frame took *seconds*; making a
  1-row image and letting Pillow `resize()` stretch it to full height is ~4 ms (~229 fps).

## Credits and licensing

**`imbushuo/DFRDisplayKm`** (MIT) — the Windows Touch Bar driver, vendored in full under
`reference/DFRDisplayKm` with its original LICENSE. The DFR protocol in `barkeep-dfr.c` is
**derived from it**: the request/response envelope, the framebuffer update layout and field
values, the FourCC keys, the bring-up order, and `dfr_update_padding[]`, which is copied
byte-for-byte from their `DfrUpdatePadding[]`. MIT is GPL-compatible, so the kernel modules
ship as GPL-2.0 with the MIT notice retained. Without this driver none of this would exist.

**`xeeban/macbook-t1-linux`** (GPL-2.0) — no code taken, but the touch digitizer protocol came
from reading it: interface 2 / EP 0x83, reports whose first 4 bytes are a little-endian
float32 X in [0.5, 1.0]. The live system indicators, keys that run a command instead of
injecting a keycode, and modifier-selected layers are all ideas from their `dfrd`,
reimplemented here. They also independently found the same `apple-ibridge` config-1 root
cause.

**`sunplex07/appletbdrm`** (GPL-2.0) — Copyright (c) 2023-2026 Kerem Karabay
<kekrby@gmail.com>, Copyright (c) 2025-2026 sunplex07. The **Touch Bar backlight protocol**
in `scripts/t1hid.py` is derived from it: the HID feature reports on interface 6, the
MinNits/MaxNits capability block, and the requirement that AutoBrightness be cleared before
manual control takes effect. Ours is an independent Python implementation against hidraw
rather than a copy of their in-kernel C, but the report layout is theirs. GPL-2.0 either
way, so the terms are unchanged.

This project is **GPL-2.0** (see `LICENSE`) — the kernel modules are already marked as such
via SPDX and `MODULE_LICENSE("GPL")`, and `scripts/` follows for consistency. MIT is
GPL-compatible, so the derivation above is fine with the notice retained.

## Prior art — read these

Two repos cover this same machine and go further in places:

- **`sunplex07/appletbdrm`** — unified T1/T2 **DRM** driver (`/dev/dri/card*`), `ibridge-switcher`
  + systemd for config 2, backlight via HID reports 3/4/5 on interface 6, DKMS install.
  Note: it does **not** handle `apple-ibridge`, which force-selects config 1 and will fight it.
- **`xeeban/macbook-t1-linux`** (`touch-bar/`) — same machine (MacBookPro13,2). Independently
  found the same config-1 force-select root cause and the same `usb_device_driver` fix. Also has
  touch input (the digitizer protocol above) and `dfrd/`, a full userspace UI with FreeType
  labels and macOS-style layers. Also fixed a heap out-of-bounds read in `apple_ibridge`
  (upstream `t2linux/apple-ib-drv#11`).

## Still to do

- **Suspend is unresolved.** A lid close hangs the machine hard — display and touchpad do
  not come back. Adding the PM callbacks made `/sys/power/pm_test=devices` pass cleanly, but
  a subsequent *real* suspend hung anyway, so the callbacks were necessary and not
  sufficient. The systemd sleep hook was added afterwards and has never been exercised,
  because suspend is disabled on the development machine.

  It is also **not established that this project is the cause**. Every hang observed so far
  had `barkeep-dfr` loaded, but no successful suspend has ever been recorded on this machine
  either, with or without it. The decisive test is a lid close with the package uninstalled;
  it has not been run.
- The stock firmware function row is unavailable while in config 2 (`apple-ibridge` must stay
  unloaded). `dfr-bar.py` replaces it with a working one, so this matters less than it did.
- Adopting `appletbdrm` would make the bar a real DRM device rather than a character device.
