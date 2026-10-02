#!/usr/bin/env python3
"""dfr-bar — a working Touch Bar for the Apple T1.

Draws a function row over a colour gradient and makes the buttons actually
work: reads the digitizer over hidraw and injects real keys via uinput. Hold Fn
for F1-F12. The keys and background fade to black when you stop using the machine.

Requires the display session to be up (scripts/dfr-up.sh) so /dev/dfr0 exists.

    sudo python3 dfr-bar.py                   # defaults
    sudo python3 dfr-bar.py --source theme    # no screen capture at all
    sudo python3 dfr-bar.py --no-touch        # render only, no key injection

Colour source:
    --source screen|theme|wallpaper   where the gradient comes from
                                      (default: screen, sampled with grim)
    --wallpaper PATH                  explicit image, implies --source wallpaper
    --poll N          seconds between samples for 'screen'   (default 3)
    --threshold N     ignore palette changes smaller than this (default 18)
    --fade N          seconds to cross-fade a palette change  (default 2)
    --flow N          drift the gradient sideways, px/sec. The bar is
                      2170px wide and the palette repeats once across it, so
                      what you actually see is a colour band passing every
                      (2170 / N / colours) seconds - at N=15 with ~6 colours,
                      about every 24s. 300 is frantic.

Idle auto-hide:
    --idle N          fade the keys and background to black after N seconds of
                      no keyboard, pointer or bar activity. 0 disables (default 30)
    --idle-out N      seconds to fade the bar to black          (default 2)
    --idle-in N       seconds to bring it back                  (default 1)

Theming:
    --tint N          opacity (0-255) of the theme-background tint on each
                      button plate. Lower = more wallpaper/theme gradient
                      shows through; 255 = flat solid plate.  Glyphs and
                      text always use the theme's foreground colour.
                      (default 255)
    --button-wallpaper  keep the bar itself pitch black and clip the wallpaper
                      gradient into the rounded button plates only

Other:
    --fps N           animation frame rate              (default 60)
    --no-touch        do not read the digitizer or inject keys
    -h, --help        this text

Touch protocol (T1, USB config 2): the digitizer is a non-standard HID on
interface 2, EP 0x83. Reports are ~52 bytes; the first 4 are a little-endian
float32 X in [0.5, 1.0] across the bar. hid-generic keeps the interface and
hidraw hands us raw reports, so this coexists with the display stack.
Release is inferred from a gap in reports.
"""
import os, sys, glob, time, math, struct, threading

from PIL import Image, ImageDraw, ImageFont

W, H, BPP = 2170, 60, 3          # bar as the user sees it (landscape)
PW, PH = H, W                    # panel buffer is portrait: 60 wide, 2170 tall
FRAME = W * H * BPP
DEV = "/dev/dfr0"
def _user_home():
    """under sudo, ~ is /root - resolve the DESKTOP user's home instead"""
    import pwd
    for name in (os.environ.get("SUDO_USER"), os.environ.get("DFR_USER")):
        if name:
            try:
                return pwd.getpwnam(name).pw_dir
            except KeyError:
                pass
    try:
        return pwd.getpwuid(1000).pw_dir
    except KeyError:
        return os.path.expanduser("~")


WALL = os.path.join(_user_home(), ".local/state/omarchy/current/background")

RELEASE_S = 0.12                 # no report for this long => finger up

# Idle behaviour: with no keyboard/mouse/bar activity, keys and gradient fade
# together to black. Any input fades both back in.
IDLE_S     = 30.0                # quiet for this long => fade the bar to black
IDLE_OUT_S = 2.0                 # fade out over this
IDLE_IN_S  = 1.0                 # fade back in over this

# The frame loop runs at 60fps, so a wall-clock gap this large between frames
# means the process was frozen - i.e. the machine suspended. The panel enable
# is a HID feature report the firmware can lose over a real suspend, and only
# userspace sends it, so on waking we re-issue it and fade back in from black.
RESUME_GAP_S = 5.0

# Frame rate while idle. The palette can keep tracking while the bar is black;
# it does not need 60fps to do so. The drift advances by elapsed time, so the
# speed on screen is identical at either rate; there are just fewer frames.
IDLE_FPS = 5.0


def ease(t):
    """smoothstep, clamped. THE easing curve - every transition on the bar uses
    it (palette cross-fade, startup rise from black, key auto-hide), so when two
    overlap they move on the same curve and read as one motion."""
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return t * t * (3 - 2 * t)

# A key is a record, not a bare tuple: besides injecting a keycode it may run
# a command in the desktop session, or carry a live indicator whose label is
# recomputed from /sys. Icons are DRAWN, not glyphs - font coverage for emoji
# and media symbols is unreliable (tofu boxes).
def K(kind, code=0, w=1.0, cmd=None, ind=None):
    return {"kind": kind, "code": code, "w": w, "cmd": cmd, "ind": ind}


KEYS_MEDIA = [
    K("esc",  1,   1.6),    # KEY_ESC
    K("bri-", 224),         # KEY_BRIGHTNESSDOWN
    K("bri+", 225),         # KEY_BRIGHTNESSUP
    K("grid", 0, cmd="__mission_control"),   # existing Hyprland SUPER+TAB overview
    K("apps", 0, cmd="omarchy-menu toggle root"),  # Omarchy app/action menu
    K("kb-",  229),         # KEY_KBDILLUMDOWN
    K("kb+",  230),         # KEY_KBDILLUMUP
    K("prev", 165),         # KEY_PREVIOUSSONG
    K("play", 164),         # KEY_PLAYPAUSE
    K("next", 163),         # KEY_NEXTSONG
    K("mute", 113),         # KEY_MUTE
    K("vol-", 114),         # KEY_VOLUMEDOWN
    K("vol+", 115),         # KEY_VOLUMEUP
]

# Hold Fn: F1-F12. Same esc on the left so the layout does not jump.
KEYS_FN = [K("esc", 1, 1.6)] + [
    K(f"F{i}", code) for i, code in enumerate(
        [59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88], start=1)
]

# Ctrl+Fn: live system row. Labels come from UPower and /sys; taps open the
# corresponding Omarchy panel rather than injecting keycodes.
KEYS_SYS = [
    K("esc", 1, 1.6),
    K("kbd",  w=1.6, ind="kbd",  cmd="omarchy-menu toggle root"),
    K("batt", w=1.6, ind="batt", cmd="omarchy-shell shell toggle omarchy.power"),
    K("wifi", w=1.6, ind="wifi", cmd="omarchy-shell shell toggle omarchy.network"),
    K("bt",   w=1.6, ind="bt",   cmd="omarchy-shell shell toggle omarchy.bluetooth"),
    K("bar-", w=1.0, cmd="__bar_dim"),      # handled internally: bar backlight
    K("bar+", w=1.0, cmd="__bar_bright"),
    K("auto", w=1.0, cmd="__bar_auto"),
]

# Alt+Fn: F13-F24. Unbound by default - bind them in your compositor.
KEYS_F13 = [K("esc", 1, 1.6)] + [
    K(f"F{i}", code) for i, code in enumerate(
        [183, 184, 185, 186, 187, 188, 189, 190, 191, 192, 193, 194], start=13)
]

# Meta+Fn: transport only, big targets.
KEYS_XPORT = [
    K("esc",  1, 1.6),
    K("prev", 165, 2.0), K("play", 164, 2.4), K("next", 163, 2.0),
    K("mute", 113, 1.6), K("vol-", 114, 1.6), K("vol+", 115, 1.6),
]

# Index order must match layer_for() below.
LAYERS = [KEYS_MEDIA, KEYS_FN, KEYS_SYS, KEYS_F13, KEYS_XPORT]
LAYER_NAMES = ["media", "fn", "system", "f13-f24", "transport"]
KEYS = KEYS_MEDIA          # default; the render/touch paths take a layer arg

# every keycode any layer can emit, so uinput advertises them all
ALL_CODES = sorted({k["code"] for layer in LAYERS for k in layer if k["code"]}
                   | {15, 125})  # KEY_TAB + KEY_LEFTMETA for Mission Control


# ---------------------------------------------------------------- palette

def find_font(size):
    for pat in ("/usr/share/fonts/**/JetBrainsMono*.ttf",
                "/usr/share/fonts/**/DejaVuSans.ttf",
                "/usr/share/fonts/**/*Nerd*.ttf",
                "/usr/share/fonts/**/*.ttf"):
        for p in sorted(glob.glob(pat, recursive=True)):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def wallpaper_palette(path, n=6, merge_dist=60):
    """Dominant colours actually PRESENT in the wallpaper.

    Quantizing alone is not enough: anti-aliased edges (e.g. a logo outline)
    produce blend colours that exist nowhere as a real region. So we quantize,
    then merge entries that are perceptually close, keeping the most common
    member of each cluster and its total share.
    """
    im = Image.open(path).convert("RGB")
    return _palette_from_image(im, n, merge_dist)


def _palette_from_image(im, n=6, merge_dist=60):
    import colorsys
    im = im.copy()
    im.thumbnail((200, 200))
    q = im.quantize(colors=n * 2, method=Image.MEDIANCUT).convert("RGB")
    counts = {}
    for px in q.getdata():
        counts[px] = counts.get(px, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])

    clusters = []                       # [representative, total_weight]
    for col, wgt in ordered:
        for cl in clusters:
            r = cl[0]
            if math.dist(col, r) < merge_dist:
                cl[1] += wgt
                break
        else:
            clusters.append([col, wgt])

    total = sum(c[1] for c in clusters) or 1
    keep = [c for c in clusters if c[1] / total >= 0.04][:n] or clusters[:n]
    if len(keep) < 2:
        keep = clusters[:2] if len(clusters) >= 2 else keep
    keep.sort(key=lambda c: colorsys.rgb_to_hsv(*[v / 255 for v in c[0]])[0])
    return [tuple(c[0]) for c in keep]


def gradient_strip(cols, length, hold=0.55):
    """Cyclic ramp that HOLDS each sampled colour, then blends briefly.

    `hold` is the fraction of each segment showing the real colour, so most of
    the bar is a colour that genuinely appears in the wallpaper rather than an
    invented in-between shade.
    """
    out = []
    k = len(cols)
    for i in range(length):
        t = (i / length) * k
        idx = int(t)
        f = t - idx
        a = cols[idx % k]
        b = cols[(idx + 1) % k]
        if f <= hold:
            out.append(tuple(a))
        else:
            g = (f - hold) / (1.0 - hold)
            g = ease(g)                             # smoothstep the short blend
            out.append(tuple(int(a[j] + (b[j] - a[j]) * g) for j in range(3)))
    return out


THEME = None   # resolved in main()


def theme_colors_path():
    return os.path.join(_user_home(), ".local/state/omarchy/current/theme/colors.toml")


def theme_palette(path=None, n=6):
    """Palette straight from the desktop theme - no capture, no cost.

    Uses the accent/ANSI hues the UI actually draws with, ordered by hue.
    """
    import colorsys, re
    path = path or theme_colors_path()
    vals = {}
    for line in open(path):
        m = re.match(r'\s*([a-z_]+)\s*=\s*"(#[0-9a-fA-F]{6})"', line)
        if m:
            vals[m.group(1)] = m.group(2)
    want = ["accent", "blue", "cyan", "green", "yellow", "orange",
            "red", "magenta", "brown"]
    cols = []
    for k in want:
        v = vals.get(k)
        if not v:
            continue
        c = tuple(int(v[i:i + 2], 16) for i in (1, 3, 5))
        if all(math.dist(c, o) > 40 for o in cols):
            cols.append(c)
    if len(cols) < 2:
        # Foreground is for glyphs, not the bar gradient. Including it here
        # makes a dark theme cycle through an unrelated bright white band.
        for k in ("background", "accent"):
            if vals.get(k):
                v = vals[k]
                c = tuple(int(v[i:i + 2], 16) for i in (1, 3, 5))
                if all(math.dist(c, o) > 40 for o in cols):
                    cols.append(c)
    cols.sort(key=lambda c: colorsys.rgb_to_hsv(*[v / 255 for v in c])[0])
    return cols[:n]


def theme_stamp(path):
    try:
        return (os.path.realpath(path), os.stat(path).st_mtime)
    except OSError:
        return None


def load_theme_button_colors(path=None):
    """Apply Omarchy background/foreground/accent to button plates and glyphs."""
    import re
    global FG, BUTTON_BG, BUTTON_ACCENT
    vals = {}
    try:
        with open(path or theme_colors_path()) as f:
            for line in f:
                m = re.match(r'\s*([a-z_]+)\s*=\s*"(#[0-9a-fA-F]{6})"', line)
                if m:
                    vals[m.group(1)] = m.group(2)
    except OSError as e:
        print(f"button theme unavailable ({e}); keeping defaults")
        return

    def rgb(name, fallback):
        value = vals.get(name, fallback)
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))

    FG = rgb("foreground", "#ffffff") + (255,)
    BUTTON_BG = rgb("background", "#161616")
    BUTTON_ACCENT = rgb("accent", "#8ab4f8")
    print("button theme: bg #%02x%02x%02x fg #%02x%02x%02x accent #%02x%02x%02x tint %d/255" %
          (*BUTTON_BG, *FG[:3], *BUTTON_ACCENT, BUTTON_TINT_ALPHA))


def watch_theme_buttons(state, period=2.0):
    path = theme_colors_path()
    stamp = theme_stamp(path)
    while not state["stop"]:
        time.sleep(period)
        cur = theme_stamp(path)
        if cur and cur != stamp:
            stamp = cur
            old = (FG, BUTTON_BG, BUTTON_ACCENT)
            load_theme_button_colors(path)
            if (FG, BUTTON_BG, BUTTON_ACCENT) != old:
                state["dirty"] = True


def watch_theme(state, period=2.0):
    path = theme_colors_path()
    stamp = theme_stamp(path)
    while not state["stop"]:
        time.sleep(period)
        cur = theme_stamp(path)
        if cur and cur != stamp:
            stamp = cur
            try:
                set_palette(state, theme_palette(path), "theme")
            except Exception as e:
                print("theme reload failed:", e)


def _desktop_env():
    """uid / runtime dir / wayland socket of the logged-in desktop user"""
    import pwd
    name = os.environ.get("SUDO_USER") or os.environ.get("DFR_USER")
    try:
        pw = pwd.getpwnam(name) if name else pwd.getpwuid(1000)
    except KeyError:
        pw = pwd.getpwuid(1000)
    rt = f"/run/user/{pw.pw_uid}"
    wd = os.environ.get("WAYLAND_DISPLAY")
    if not wd and os.path.isdir(rt):
        socks = sorted(g for g in os.listdir(rt) if g.startswith("wayland-")
                       and not g.endswith(".lock"))
        wd = socks[0] if socks else "wayland-1"
    return pw.pw_name, pw.pw_uid, rt, wd or "wayland-1"


def screen_palette(n=6, scale=0.05):
    """Dominant colours of what is CURRENTLY ON SCREEN (grim, as the user)."""
    import subprocess, io
    user, uid, rt, wd = _desktop_env()
    cmd = ["grim", "-s", str(scale), "-t", "ppm", "-"]
    if os.geteuid() == 0:
        # setpriv drops to the desktop user WITHOUT opening a PAM session -
        # sudo/runuser would log an auth record on every single sample.
        import shutil
        env = ["env", f"XDG_RUNTIME_DIR={rt}", f"WAYLAND_DISPLAY={wd}",
               f"HOME={os.path.expanduser('~' + user)}"]
        if shutil.which("setpriv"):
            cmd = ["setpriv", "--reuid", str(uid), "--regid", str(uid),
                   "--clear-groups"] + env + cmd
        else:
            cmd = ["sudo", "-n", "-u", user] + env + cmd
    r = subprocess.run(cmd, capture_output=True, timeout=8)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError((r.stderr or b"grim failed").decode()[:120])
    im = Image.open(io.BytesIO(r.stdout)).convert("RGB")
    return _palette_from_image(im, n)


def wallpaper_stamp(path):
    """identity of the CURRENT wallpaper: theme changes swap the symlink target"""
    try:
        real = os.path.realpath(path)
        return (real, os.stat(real).st_mtime)
    except OSError:
        return None


def strip_row(cols):
    """gradient as a 1-row RGB image, W wide - the unit we blend and scale"""
    return Image.frombytes("RGB", (W, 1),
                           b"".join(bytes(c) for c in gradient_strip(cols, W)))


def displayed_row(state):
    """the gradient exactly as it is on the panel at this instant.

    Pure: no state mutation, so the palette watcher threads can ask for it too.
    """
    a, b = state.get("row_from"), state.get("row_to")
    if a is None or b is None:
        return b
    t = (time.time() - state.get("fade_t0", 0)) / max(state.get("fade_s", 2.0), 1e-6)
    if t >= 1.0:
        return b
    return Image.blend(a, b, ease(t))


def set_palette(state, cols, why):
    """cross-fade the WHOLE bar from the current gradient to the new one.

    Starts from what is ON THE PANEL right now, not from the previous fade's
    destination. Screen sampling polls every 3s while a fade takes 2s, so a
    second colour change lands mid-fade often; using the old target as the
    start snapped the background before fading it, which read as a jolt.
    """
    new = strip_row(cols)
    if state.get("row_to") is not None and new.tobytes() == state["row_to"].tobytes():
        return
    state["row_from"] = displayed_row(state) or new
    state["row_to"] = new
    state["fade_t0"] = time.time()
    state["dirty"] = True
    print(f"{why}: " + "  ".join("#%02x%02x%02x" % c for c in cols))


def current_row(state, fade_s):
    """the 1-row gradient for this instant, mid-fade if a change is in flight"""
    a, b = state.get("row_from"), state.get("row_to")
    if a is None:
        return b
    if (time.time() - state.get("fade_t0", 0)) / max(fade_s, 1e-6) >= 1.0:
        state["row_from"] = None
        return b
    state["fading"] = True
    return displayed_row(state)


def watch_wallpaper(path, state, period=2.0):
    stamp = wallpaper_stamp(path)
    while not state["stop"]:
        time.sleep(period)
        cur = wallpaper_stamp(path)
        if cur and cur != stamp:
            stamp = cur
            try:
                set_palette(state, wallpaper_palette(path),
                            "wallpaper -> " + os.path.basename(cur[0]))
            except Exception as e:
                print("wallpaper reload failed:", e)


def palette_distance(a, b):
    """rough perceptual gap between two palettes (0 = identical)"""
    if not a or not b:
        return 1e9
    def near(c, pal):
        return min(math.dist(c, o) for o in pal)
    return (sum(near(c, b) for c in a) / len(a) +
            sum(near(c, a) for c in b) / len(b)) / 2


def initial_palette(src, wall):
    if src == "screen":
        return screen_palette(), "screen"
    if src == "wallpaper":
        return wallpaper_palette(wall), "wallpaper " + os.path.basename(
            os.path.realpath(wall))
    return theme_palette(), "theme"


def watch_screen(state, period=3.0, threshold=18.0):
    """Track the colours on screen and cross-fade when they actually shift.

    The threshold stops the bar re-fading on every tiny variation (a blinking
    cursor, a scrolling line) - only a real change in what is on screen.
    """
    last = None
    while not state["stop"]:
        try:
            cols = screen_palette()
            if last is None or palette_distance(cols, last) > threshold:
                last = cols
                set_palette(state, cols, "screen")
        except Exception as e:
            print("screen sample failed:", e)
            time.sleep(10)
        time.sleep(period)


# ---------------------------------------------------------------- drawing

def key_extents(keys=None):
    keys = keys if keys is not None else KEYS
    total = sum(k["w"] for k in keys)
    acc, out = 0.0, []
    for wgt in (k["w"] for k in keys):
        x0 = int(acc / total * W + 0.5)
        acc += wgt
        x1 = int(acc / total * W + 0.5)
        out.append((x0, x1))
    return out


FG = (255, 255, 255, 255)
BUTTON_BG = (22, 22, 22)
BUTTON_ACCENT = (138, 180, 248)
# Opacity of the theme-background tint laid over each button plate. 255 is
# solid; lower values let the wallpaper/theme gradient show through, which is
# what makes the theme read as a tint rather than a flat block. Tunable with
# --tint. The idle fade needs no special case: render() already blends the
# whole overlay by `buttons`, so a faint plate washes out with everything else.
BUTTON_TINT_ALPHA = 255


def draw_icon(d, kind, cx, cy, font, val=None):
    """vector icons - no font coverage worries"""
    def sun(scale, dx=0):
        r = 7 * scale
        x = cx + dx
        d.ellipse([x - r, cy - r, x + r, cy + r], fill=FG)
        for a in range(0, 360, 45):
            t = math.radians(a)
            d.line([x + math.cos(t) * (r + 3), cy + math.sin(t) * (r + 3),
                    x + math.cos(t) * (r + 7), cy + math.sin(t) * (r + 7)],
                   fill=FG, width=2)

    def tri(ox, flip=False):
        s = 9
        pts = ([(cx + ox + s, cy - s), (cx + ox + s, cy + s), (cx + ox - s, cy)]
               if flip else
               [(cx + ox - s, cy - s), (cx + ox - s, cy + s), (cx + ox + s, cy)])
        d.polygon(pts, fill=FG)

    def speaker(waves, dx=0):
        x = cx + dx
        d.polygon([(x - 12, cy - 5), (x - 6, cy - 5), (x + 1, cy - 12),
                   (x + 1, cy + 12), (x - 6, cy + 5), (x - 12, cy + 5)], fill=FG)
        for i in range(waves):
            r = 6 + i * 5
            d.arc([x + 1 - r, cy - r, x + 1 + r, cy + r], -55, 55, fill=FG, width=2)

    def text(label, size=22, dx=0):
        f = font
        try:
            bb = d.textbbox((0, 0), label, font=f)
            d.text((cx + dx - (bb[2] - bb[0]) / 2 - bb[0],
                    cy - (bb[3] - bb[1]) / 2 - bb[1]),
                   label, font=f, fill=FG, stroke_width=1,
                   stroke_fill=(0, 0, 0, 210))
        except Exception:
            pass

    def plusminus(sign, ox):
        d.line([cx + ox - 6, cy, cx + ox + 6, cy], fill=FG, width=2)
        if sign > 0:
            d.line([cx + ox, cy - 6, cx + ox, cy + 6], fill=FG, width=2)

    if kind == "esc":
        text("esc")
    elif kind in ("bri-", "bri+"):
        # sun spans dx-12..dx+12 (rays); +/- spans ox-6..ox+6 -> ~16px clear
        sun(0.8, dx=-18)
        plusminus(1 if kind.endswith("+") else -1, 20)
    elif kind == "grid":
        for r in range(2):
            for c in range(3):
                x = cx - 15 + c * 11; y = cy - 9 + r * 11
                d.rectangle([x, y, x + 8, y + 8], fill=FG)
    elif kind == "apps":
        for r in range(3):
            for c in range(3):
                x = cx - 13 + c * 10; y = cy - 13 + r * 10
                d.ellipse([x, y, x + 6, y + 6], fill=FG)
    elif kind in ("kb-", "kb+"):
        d.rounded_rectangle([cx - 30, cy - 8, cx - 10, cy + 8], radius=3,
                            outline=FG, width=2)
        for i in range(3):
            d.line([cx - 26 + i * 6, cy + 4, cx - 26 + i * 6, cy + 4], fill=FG, width=2)
        plusminus(1 if kind.endswith("+") else -1, 16)
    elif kind == "prev":
        d.rectangle([cx - 12, cy - 9, cx - 9, cy + 9], fill=FG); tri(4, flip=True)
    elif kind == "next":
        tri(-4); d.rectangle([cx + 9, cy - 9, cx + 12, cy + 9], fill=FG)
    elif kind == "play":
        d.rectangle([cx - 8, cy - 9, cx - 4, cy + 9], fill=FG)
        d.rectangle([cx + 4, cy - 9, cx + 8, cy + 9], fill=FG)
    elif kind == "mute":
        speaker(0)
        d.line([cx + 6, cy - 8, cx + 16, cy + 8], fill=FG, width=2)
        d.line([cx + 16, cy - 8, cx + 6, cy + 8], fill=FG, width=2)
    elif kind == "vol-":
        speaker(1, dx=-10); plusminus(-1, 22)
    elif kind == "vol+":
        speaker(2, dx=-12); plusminus(1, 26)
    elif kind == "batt":
        # body + terminal, filled proportionally to charge
        x0, x1, y0, y1 = cx - 46, cx - 20, cy - 8, cy + 8
        d.rounded_rectangle([x0, y0, x1, y1], radius=3, outline=FG, width=2)
        d.rectangle([x1 + 1, cy - 3, x1 + 4, cy + 3], fill=FG)
        pct = (val or 0) if isinstance(val, int) else 0
        if pct > 0:
            w = int((x1 - x0 - 6) * pct / 100.0)
            d.rectangle([x0 + 3, y0 + 3, x0 + 3 + w, y1 - 3], fill=FG)
        text(f"{pct}%", dx=18)
    elif kind == "wifi":
        # three arcs; hollow when down
        for i, r in enumerate((6, 13, 20)):
            box = [cx - 18 - r + 18, cy + 4 - r, cx - 18 + r + 18, cy + 4 + r]
            d.arc(box, start=215, end=325, fill=FG,
                  width=3 if val else 1)
        d.ellipse([cx + 16, cy + 1, cx + 21, cy + 6], fill=FG if val else None,
                  outline=FG)
    elif kind == "bt":
        # the runic bluetooth mark
        # One continuous stroke, the way the real mark is built:
        # upper-left -> lower-right -> bottom -> up the stem -> top ->
        # upper-right -> lower-left. The two wings sit on the RIGHT of the
        # stem and the long diagonals cross it.
        w = 3 if val else 2
        d.line([(cx - 9, cy - 7), (cx + 9, cy + 7), (cx, cy + 15),
                (cx, cy - 15), (cx + 9, cy - 7), (cx - 9, cy + 7)],
               fill=FG, width=w, joint="curve")
        if not val:
            d.line([cx - 14, cy + 13, cx + 14, cy - 13], fill=FG, width=2)
    elif kind == "kbd":
        d.rounded_rectangle([cx - 46, cy - 9, cx - 20, cy + 7], radius=3,
                            outline=FG, width=2)
        for r in range(2):
            for c in range(4):
                x = cx - 42 + c * 6
                y = cy - 5 + r * 6
                d.rectangle([x, y, x + 2, y + 2], fill=FG)
        text(f"{val}%" if isinstance(val, int) else "kbd", dx=18)
    elif kind in ("bar-", "bar+"):
        # a little touch bar, filled more for "+", with the sign beside it
        d.rounded_rectangle([cx - 26, cy - 7, cx + 2, cy + 7], radius=3,
                            outline=FG, width=2)
        d.rectangle([cx - 23, cy - 4, cx + (-1 if kind == "bar+" else -15), cy + 4],
                    fill=FG)
        plusminus(1 if kind == "bar+" else -1, 16)
    elif kind == "auto":
        text("AUTO")
    else:
        text(kind)


def panel_on():
    """Turn the panel backlight on: HID feature report id 2, [rid, aux=1, disp=1].

    Called AFTER the first real frame is on the panel, so the user never sees
    the module's placeholder fill - the bar lights up already showing the UI.
    """
    import fcntl
    def rid_of(sd):
        try:
            d = open(os.path.join(sd, "report_descriptor"), "rb").read()
        except Exception:
            return None
        i = 0; up = None; r = None
        while i < len(d):
            b = d[i]; sz = b & 3; sz = 4 if sz == 3 else sz
            t = (b >> 2) & 3; g = (b >> 4) & 0xF
            v = int.from_bytes(d[i + 1:i + 1 + sz], "little") if sz else 0
            if t == 1 and g == 0: up = v
            if t == 1 and g == 8: r = v
            if t == 2 and g == 0 and up == 0xff12 and v == 0x21:
                return r
            i += 1 + sz
        return None

    def ioc(l):
        return (3 << 30) | (l << 16) | (ord("H") << 8) | 0x06

    ok = 0
    for hd in sorted(glob.glob("/sys/bus/hid/devices/*")):
        r = rid_of(hd)
        raws = glob.glob(os.path.join(hd, "hidraw", "hidraw*"))
        if r is None or not raws:
            continue
        node = "/dev/" + os.path.basename(raws[0])
        buf = bytearray(11); buf[0] = r; buf[1] = 1; buf[2] = 1
        try:
            fd = os.open(node, os.O_RDWR)
            try:
                fcntl.ioctl(fd, ioc(len(buf)), bytes(buf)); ok += 1
            finally:
                os.close(fd)
        except Exception:
            pass
    return ok


_render_cache = {"sig": None, "img": None}
_plate_masks = {}


def render(row, offset, pressed, font, keys=None, buttons=1.0, ind=None,
           background=1.0, black=None, button_wallpaper=False):
    """Render the bar, optionally clipping the wallpaper into button plates.

    In button-wallpaper mode the unoccupied Touch Bar is true black. The
    wallpaper gradient is pasted only through each rounded button mask; the
    normal theme tint is then drawn over that clipped wallpaper.
    """
    rowb = row.tobytes()
    o = (int(offset) % W) * 3
    rowb = rowb[o:] + rowb[:o]
    wallpaper = Image.frombytes("RGB", (W, 1), rowb).resize((W, H), Image.NEAREST)
    black = black if black is not None else Image.new("RGB", (W, H))
    if button_wallpaper:
        bg = black.copy()
    else:
        bg = wallpaper
        if background < 1.0:
            bg = Image.blend(black, bg, max(0.0, min(1.0, background)))
    if buttons <= 0.002:
        return bg
    keys = keys if keys is not None else KEYS
    ind = ind or {}
    sig = (rowb, pressed, id(keys), id(font), repr(ind), FG, BUTTON_BG,
           BUTTON_ACCENT, BUTTON_TINT_ALPHA, button_wallpaper,
           None if button_wallpaper else background)
    if _render_cache["sig"] == sig:
        img = _render_cache["img"]
    else:
        img = bg if buttons >= 0.998 else bg.copy()
        d = ImageDraw.Draw(img, "RGBA")
        for i, (x0, x1) in enumerate(key_extents(keys)):
            box = [x0 + 4, 5, x1 - 4, H - 6]
            if button_wallpaper and i != pressed:
                mask = _plate_masks.get((x0, x1))
                if mask is None:
                    mw, mh = box[2] - box[0] + 1, box[3] - box[1] + 1
                    mask = Image.new("L", (mw, mh), 0)
                    ImageDraw.Draw(mask).rounded_rectangle(
                        [0, 0, mw - 1, mh - 1], radius=10, fill=255)
                    _plate_masks[(x0, x1)] = mask
                img.paste(wallpaper.crop((box[0], box[1], box[2] + 1, box[3] + 1)),
                          (box[0], box[1]), mask)
            d.rounded_rectangle(box, radius=10,
                                fill=((*BUTTON_BG, BUTTON_TINT_ALPHA) if i != pressed
                                      else (*BUTTON_ACCENT, 255)),
                                outline=(*BUTTON_ACCENT, 190), width=1)
            k = keys[i]
            val = ind.get(k["ind"]) if k.get("ind") else None
            if k.get("ind") == "batt" and isinstance(val, tuple):
                val = val[0]
            draw_icon(d, k["kind"], (x0 + x1) / 2, H / 2, font, val)
        _render_cache["sig"] = sig
        _render_cache["img"] = img
    return img if buttons >= 0.998 else Image.blend(bg, img, buttons)


def to_panel(img):
    """landscape 2170x60 -> panel's portrait 60x2170 buffer"""
    return img.transpose(Image.ROTATE_270).tobytes()


# ---------------------------------------------------------------- touch

def find_digitizer():
    """hidraw node on USB interface 2 of 05ac:8600 while in config 2"""
    for hr in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        dev = os.path.realpath(os.path.join(hr, "device"))
        p = dev
        for _ in range(6):
            inum = os.path.join(p, "bInterfaceNumber")
            if os.path.exists(inum):
                try:
                    if int(open(inum).read().strip(), 16) != 2:
                        break
                except Exception:
                    break
                usbdev = os.path.dirname(p)
                try:
                    vid = open(os.path.join(usbdev, "idVendor")).read().strip()
                    pid = open(os.path.join(usbdev, "idProduct")).read().strip()
                    cfg = open(os.path.join(usbdev, "bConfigurationValue")).read().strip()
                except Exception:
                    break
                if vid == "05ac" and pid == "8600" and cfg == "2":
                    return "/dev/" + os.path.basename(hr)
                break
            p = os.path.dirname(p)
    return None


def find_keyboard():
    """Prefer the MacBook keyboard for Fn; fall back to another KEY_FN device."""
    import evdev
    fallback = None
    for p in sorted(glob.glob("/dev/input/event*")):
        try:
            d = evdev.InputDevice(p)
            has_fn = evdev.ecodes.KEY_FN in d.capabilities().get(evdev.ecodes.EV_KEY, [])
        except Exception:
            continue
        if not has_fn:
            continue
        if d.name == "Apple SPI Keyboard":
            return p
        if fallback is None:
            fallback = p
    return fallback


def layer_for(fn, ctrl, alt, meta):
    """Which layer the current modifier combination selects."""
    if not fn:
        return 1                        # F1-F12 by default
    if ctrl:
        return 2                        # live system row
    if alt:
        return 3                        # F13-F24
    if meta:
        return 4                        # transport
    return 0                            # media while Fn is held


def watch_fn(state, node):
    """Fn (plus a modifier) chooses the layer; releasing Fn returns to media.

    The Apple SPI keyboard autorepeats Fn (value 2) while held, so treat 1 and 2
    as down and 0 as up. If a key is being touched when the layer flips, release
    it first so the old layer's keycode cannot stick down.
    """
    import evdev, select
    from evdev import ecodes
    d = evdev.InputDevice(node)
    print(f"fn: watching {node} ({d.name}) - Fn / Ctrl+Fn / Alt+Fn / Meta+Fn")
    CTRL = {ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL}
    ALT  = {ecodes.KEY_LEFTALT,  ecodes.KEY_RIGHTALT}
    META = {ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA}
    held = {"fn": False, "ctrl": False, "alt": False, "meta": False}
    while not state["stop"]:
        r, _, _ = select.select([d], [], [], 0.5)
        if not r:
            continue
        try:
            events = list(d.read())
        except OSError:
            continue
        changed = False
        for e in events:
            if e.type != ecodes.EV_KEY:
                continue
            down = e.value in (1, 2)        # 2 = autorepeat, still held
            if e.code == ecodes.KEY_FN:
                held["fn"] = down; changed = True
            elif e.code in CTRL:
                held["ctrl"] = down; changed = True
            elif e.code in ALT:
                held["alt"] = down; changed = True
            elif e.code in META:
                held["meta"] = down; changed = True
        if not changed:
            continue
        want = layer_for(held["fn"], held["ctrl"], held["alt"], held["meta"])
        if want != state["layer"]:
            state["layer"] = want
            state["layer_changed"] = True
            state["dirty"] = True


def _first(pattern, name):
    for d in sorted(glob.glob(pattern)):
        try:
            return open(os.path.join(d, name)).read().strip()
        except Exception:
            continue
    return None


def _upower_display_battery():
    """Read UPower's aggregate percentage, matching Omarchy's own battery UI."""
    import subprocess
    try:
        result = subprocess.run(
            ["upower", "-i", "/org/freedesktop/UPower/devices/DisplayDevice"],
            check=True, capture_output=True, text=True, timeout=1.0)
        fields = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                name, value = line.strip().split(":", 1)
                fields[name.strip()] = value.strip()
        percent = fields.get("percentage", "").rstrip("% ")
        return int(round(float(percent))), fields.get("state", "").lower()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def read_indicators():
    """Live system values from UPower and /sys; absent devices simply omit keys."""
    out = {}
    battery = _upower_display_battery()
    if battery is not None:
        out["batt"] = battery
    else:
        # Fallback for systems without UPower: prefer an energy/charge ratio
        # over the gauge's sometimes-inaccurate rounded `capacity` attribute.
        cap = _first("/sys/class/power_supply/BAT*", "capacity")
        st = _first("/sys/class/power_supply/BAT*", "status")
        for now_name, full_name in (("energy_now", "energy_full"),
                                    ("charge_now", "charge_full")):
            now = _first("/sys/class/power_supply/BAT*", now_name)
            full = _first("/sys/class/power_supply/BAT*", full_name)
            if now is not None and full is not None and int(full) > 0:
                cap = str(round(100 * int(now) / int(full)))
                break
        if cap is not None:
            out["batt"] = (int(cap), (st or "").lower())

    # wifi: an operstate of "up" on a wireless interface
    for d in sorted(glob.glob("/sys/class/net/*")):
        if not os.path.isdir(os.path.join(d, "wireless")):
            continue
        try:
            out["wifi"] = open(os.path.join(d, "operstate")).read().strip() == "up"
        except Exception:
            pass
        break

    # bluetooth: rfkill says whether the radio is blocked
    for d in sorted(glob.glob("/sys/class/rfkill/rfkill*")):
        try:
            if open(os.path.join(d, "type")).read().strip() != "bluetooth":
                continue
            soft = open(os.path.join(d, "soft")).read().strip()
            hard = open(os.path.join(d, "hard")).read().strip()
            out["bt"] = (soft == "0" and hard == "0")
        except Exception:
            pass
        break

    # keyboard backlight as a percentage of its own maximum
    for d in sorted(glob.glob("/sys/class/leds/*kbd_backlight*")):
        try:
            cur = int(open(os.path.join(d, "brightness")).read().strip())
            mx  = int(open(os.path.join(d, "max_brightness")).read().strip()) or 1
            out["kbd"] = int(cur * 100 / mx)
        except Exception:
            pass
        break
    return out


def watch_indicators(state, period=2.5):
    while not state["stop"]:
        try:
            new = read_indicators()
            if new != state.get("ind"):
                state["ind"] = new
                # only forces a redraw when the system layer is on screen
                if LAYERS[state.get("layer", 0)] is KEYS_SYS:
                    state["dirty"] = True
        except Exception as e:
            print("indicators failed:", e)
        time.sleep(period)


def run_as_user(cmd):
    """Run a command in the desktop user's session (never as root)."""
    import subprocess, pwd
    try:
        pw = pwd.getpwuid(1000)
        for name in (os.environ.get("SUDO_USER"), os.environ.get("DFR_USER")):
            if name:
                pw = pwd.getpwnam(name)
                break
    except KeyError:
        return
    env = dict(os.environ)
    omarchy_path = env.get("OMARCHY_PATH") or "/usr/share/omarchy"
    omarchy_bin = os.path.join(omarchy_path, "bin")
    path = env.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    if omarchy_bin not in path.split(os.pathsep):
        path = omarchy_bin + os.pathsep + path
    env.update({"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name,
                "XDG_RUNTIME_DIR": f"/run/user/{pw.pw_uid}",
                "OMARCHY_PATH": omarchy_path, "PATH": path})
    env.pop("SUDO_USER", None)
    try:
        subprocess.Popen(
            ["setpriv", "--reuid", str(pw.pw_uid), "--regid", str(pw.pw_gid),
             "--init-groups", "--inh-caps=-all", "/bin/sh", "-c", cmd],
            env=env, stdout=subprocess.DEVNULL, stderr=sys.stderr,
            start_new_session=True)
    except Exception as e:
        print("command failed:", e)


def do_action(state, key, ui=None, ecodes=None):
    """A key with `cmd` runs something instead of injecting a keycode."""
    cmd = key.get("cmd")
    if not cmd:
        return False
    if cmd == "__mission_control":
        # Reuse the user's existing Hyprland SUPER+TAB -> Hymission binding.
        ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTMETA, 1)
        ui.write(ecodes.EV_KEY, ecodes.KEY_TAB, 1)
        ui.syn()
        ui.write(ecodes.EV_KEY, ecodes.KEY_TAB, 0)
        ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTMETA, 0)
        ui.syn()
        return True
    if cmd.startswith("__bar_"):                 # bar backlight, handled here
        try:
            import t1hid
            if cmd == "__bar_auto":
                t1hid.set_auto(True)
                print("bar backlight: ALS auto")
            else:
                step = 15 if cmd == "__bar_bright" else -15
                pct = max(0, min(100, state.get("bar_pct", 60) + step))
                state["bar_pct"] = pct
                t1hid.set_percent(pct)
                print(f"bar backlight: {pct}%")
        except Exception as e:
            print("bar backlight failed:", e)
        return True
    run_as_user(cmd)
    return True


def input_devices():
    """Real keyboards and pointers - what counts as 'the user is here'.

    Deliberately narrow. Power/Sleep/Lid/Video Bus all carry EV_KEY but are not
    typing, and our own uinput node would let a bar tap wake itself through a
    second path. Keyboard = has a letter key; pointer = EV_REL, or EV_ABS with
    a touch/click button (that last one is the bar's own digitizer surface, so
    touching the bar counts as activity too).
    """
    import evdev
    from evdev import ecodes
    out = []
    for p in sorted(glob.glob("/dev/input/event*"), key=lambda x: int(x.split("event")[-1])):
        try:
            d = evdev.InputDevice(p)
        except Exception:
            continue
        caps = d.capabilities()
        keys = set(caps.get(ecodes.EV_KEY, []))
        if d.name == "Apple T1 Touch Bar":            # our own injected keys
            d.close(); continue
        pointer = ecodes.EV_REL in caps or (
            ecodes.EV_ABS in caps and
            keys & {ecodes.BTN_TOUCH, ecodes.BTN_LEFT, ecodes.BTN_TOOL_FINGER})
        if ecodes.KEY_A in keys or pointer:
            # EVIOCGRAB probe. An evdev grab is exclusive, so a device grabbed
            # by another process (unpalm grabs the physical A1707 pad and
            # republishes it as "Filtered Touchpad") never delivers events to
            # our fd, even though the fd opens fine. Probe the grab instead of
            # assuming an open fd means a readable device.
            try:
                d.grab(); d.ungrab()
            except OSError:
                d.close(); continue                # grabbed elsewhere: dead fd
            out.append(d)
        else:
            d.close()
    return out


def watch_input(state):
    """Any keypress or pointer movement refreshes state['last_input']."""
    import evdev, select
    from evdev import ecodes
    devs = input_devices()
    if not devs:
        print("idle: no keyboard/pointer found - keys will stay visible")
        return
    print("idle: activity from " + ", ".join(d.name for d in devs))
    fds = {d.fd: d for d in devs}
    while not state["stop"]:
        r, _, _ = select.select(list(fds), [], [], 0.5)
        for fd in r:
            try:
                for e in fds[fd].read():
                    if e.type in (ecodes.EV_KEY, ecodes.EV_REL, ecodes.EV_ABS):
                        state["last_input"] = time.time()
                        break
            except OSError:
                pass


def make_uinput():
    import evdev
    from evdev import UInput, ecodes
    caps = {ecodes.EV_KEY: ALL_CODES}
    return UInput(caps, name="Apple T1 Touch Bar")


def touch_loop(state, node):
    import evdev
    from evdev import ecodes
    ui = make_uinput()
    fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
    last = 0.0
    cur = None
    print(f"touch: reading {node}")
    try:
        while not state["stop"]:
            try:
                data = os.read(fd, 64)
            except BlockingIOError:
                data = b""
            now = time.time()
            keys = LAYERS[state["layer"]]
            extents = key_extents(keys)
            if state.pop("layer_changed", False) and cur is not None:
                # layer flipped while a finger was down - release the old key
                old = LAYERS[cur[0]][cur[1]]
                if old["code"]:
                    ui.write(ecodes.EV_KEY, old["code"], 0)
                    ui.syn()
                cur = None
                state["pressed"] = -1
            if data and len(data) >= 4:
                x = struct.unpack("<f", data[:4])[0]
                if x >= 0.45:
                    keys = LAYERS[state.get("layer", 0)]
                    extents = key_extents(keys)
                    nx = max(0.0, min(0.99999, (x - 0.5) * 2.0))
                    idx = min(int(nx * W) , W - 1)
                    zone = 0
                    for i, (a, b) in enumerate(extents):
                        if a <= idx < b:
                            zone = i
                            break
                    last = now
                    state["last_input"] = now      # touching the bar wakes it
                    if cur != (state.get("layer", 0), zone):
                        if cur is not None:
                            prev = LAYERS[cur[0]][cur[1]]
                            if prev["code"]:
                                ui.write(ecodes.EV_KEY, prev["code"], 0)
                        k = keys[zone]
                        # A command key acts once on touch-down; a normal key
                        # injects press now and release when the finger lifts.
                        if not do_action(state, k, ui, ecodes) and k["code"]:
                            ui.write(ecodes.EV_KEY, k["code"], 1)
                        ui.syn()
                        cur = (state.get("layer", 0), zone)
                        state["pressed"] = zone
                        state["dirty"] = True
            elif cur is not None and now - last > RELEASE_S:
                done = LAYERS[cur[0]][cur[1]]
                if done["code"]:
                    ui.write(ecodes.EV_KEY, done["code"], 0)
                    ui.syn()
                cur = None
                state["pressed"] = -1
                state["dirty"] = True
            time.sleep(0.004)
    finally:
        os.close(fd)
        ui.close()


# ---------------------------------------------------------------- main

def main():
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__.strip())
        return
    wall, no_touch, flow = WALL, False, 0.0
    source, fade_s = "screen", 2.0
    fps = 60.0
    button_wallpaper = False
    if "--wallpaper" in args:
        i = args.index("--wallpaper"); wall = args[i + 1]; source = "wallpaper"; del args[i:i + 2]
    if "--source" in args:
        i = args.index("--source"); source = args[i + 1]; del args[i:i + 2]
    if "--fade" in args:
        i = args.index("--fade"); fade_s = float(args[i + 1]); del args[i:i + 2]
    if "--no-touch" in args:
        no_touch = True; args.remove("--no-touch")
    if "--button-wallpaper" in args:
        button_wallpaper = True; args.remove("--button-wallpaper")
    if "--flow" in args:
        i = args.index("--flow"); flow = float(args[i + 1]); del args[i:i + 2]
    if "--fps" in args:
        i = args.index("--fps"); fps = max(1.0, float(args[i + 1])); del args[i:i + 2]
    thresh = 18.0
    if "--threshold" in args:
        i = args.index("--threshold"); thresh = float(args[i + 1]); del args[i:i + 2]
    poll = 3.0
    if "--poll" in args:
        i = args.index("--poll"); poll = float(args[i + 1]); del args[i:i + 2]
    preview, preview_layer = None, 0
    if "--preview" in args:
        i = args.index("--preview"); preview = args[i + 1]; del args[i:i + 2]
    if "--preview-layer" in args:
        i = args.index("--preview-layer")
        name = args[i + 1]; del args[i:i + 2]
        preview_layer = (LAYER_NAMES.index(name) if name in LAYER_NAMES
                         else int(name))
    bar_pct = None
    if "--bar-brightness" in args:
        i = args.index("--bar-brightness"); bar_pct = args[i + 1]; del args[i:i + 2]
    if "--tint" in args:
        i = args.index("--tint")
        globals()["BUTTON_TINT_ALPHA"] = max(0, min(255, int(float(args[i + 1]))))
        del args[i:i + 2]
    idle_s, idle_out_s, idle_in_s = IDLE_S, IDLE_OUT_S, IDLE_IN_S
    if "--idle" in args:              # 0 disables the auto-hide entirely
        i = args.index("--idle"); idle_s = float(args[i + 1]); del args[i:i + 2]
    if "--idle-out" in args:
        i = args.index("--idle-out"); idle_out_s = float(args[i + 1]); del args[i:i + 2]
    if "--idle-in" in args:
        i = args.index("--idle-in"); idle_in_s = float(args[i + 1]); del args[i:i + 2]

    load_theme_button_colors()
    print(f"frame rate: {fps:g} fps")

    if preview:
        # Offline render: no /dev/dfr0, no root, no panel. Lets the layout and
        # icons be iterated on without touching the hardware at all.
        try:
            cols, why = initial_palette(source, wall)
        except Exception as e:
            print(f"palette unavailable ({e}); using the builtin")
            cols, why = [(137, 180, 250), (203, 166, 247), (166, 227, 161)], "builtin"
        img = render(strip_row(cols), 0, -1, find_font(26),
                     LAYERS[preview_layer], 1.0, read_indicators(),
                     button_wallpaper=button_wallpaper)
        img.save(preview)
        print(f"{preview}: {LAYER_NAMES[preview_layer]} layer, palette from {why}")
        return

    if not os.path.exists(DEV):
        sys.exit(f"{DEV} missing - run scripts/dfr-up.sh first")

    # Single-instance lock: two writers on /dev/dfr0 fight and the bar flickers
    # wildly. Held for the life of the process, released automatically on exit.
    import fcntl as _f
    lockf = open("/run/barkeep.lock", "w")
    try:
        _f.flock(lockf, _f.LOCK_EX | _f.LOCK_NB)
    except BlockingIOError:
        sys.exit("another dfr-bar is already running "
                 "(sudo systemctl stop barkeep-bar, or kill it first)")
    lockf.write(str(os.getpid())); lockf.flush()

    state = {"pressed": -1, "dirty": True, "stop": False, "layer": 1,
             "row_from": None, "row_to": None, "fade_t0": 0.0, "fade_s": fade_s,
             "last_input": time.time(), "btn_p": 1.0,
             "ind": {}, "bar_pct": 60}

    def initial(src):
        return initial_palette(src, wall)

    # Seed the palette from whatever works right now, but KEEP the requested
    # source: at boot there may be no Wayland session yet for 'screen', and the
    # watcher must keep trying so it takes over once the session appears.
    wanted = source
    for src in (source, "theme", "wallpaper"):
        try:
            cols, why = initial(src)
            break
        except Exception as e:
            print(f"{src} unavailable right now: {e}")
    else:
        cols, why = [(137, 180, 250), (203, 166, 247), (166, 227, 161)], "builtin"
    source = wanted
    set_palette(state, cols, why)
    state["row_from"] = None                     # palette fade starts settled

    font = find_font(26)

    if source == "theme":
        threading.Thread(target=watch_theme, args=(state,), daemon=True).start()
    elif source == "wallpaper":
        threading.Thread(target=watch_wallpaper, args=(wall, state), daemon=True).start()
    elif source == "screen":
        print(f"screen tracking: sampling every {poll}s, fade {fade_s}s, "
              f"threshold {thresh} (in-memory only, nothing written to disk)")
        threading.Thread(target=watch_screen, args=(state, poll, thresh), daemon=True).start()

    threading.Thread(target=watch_theme_buttons, args=(state,), daemon=True).start()

    state["ind"] = read_indicators()
    threading.Thread(target=watch_indicators, args=(state,), daemon=True).start()
    print("indicators: " + (", ".join(sorted(state["ind"])) or "none found"))

    if bar_pct is not None:
        try:
            import t1hid
            if bar_pct == "auto":
                t1hid.set_auto(True); print("bar backlight: ALS auto")
            else:
                state["bar_pct"] = float(bar_pct)
                t1hid.set_percent(state["bar_pct"])
                print(f"bar backlight: {bar_pct}%")
        except Exception as e:
            print("bar backlight failed:", e)

    if idle_s > 0:
        print(f"idle: keys and background fade to black after {idle_s}s quiet "
              f"(out {idle_out_s}s / in {idle_in_s}s)")
        threading.Thread(target=watch_input, args=(state,), daemon=True).start()

    kbd = find_keyboard()
    if kbd:
        threading.Thread(target=watch_fn, args=(state, kbd), daemon=True).start()
    else:
        print("fn: no keyboard reporting KEY_FN - layer switching disabled")

    node = None if no_touch else find_digitizer()
    if node:
        threading.Thread(target=touch_loop, args=(state, node), daemon=True).start()
    elif not no_touch:
        print("touch: digitizer hidraw not found (need config 2) - display only")

    dev = open(DEV, "wb", buffering=0)
    offset = 0
    black = Image.new("RGB", (W, H))

    # Put BLACK on the panel first, then light it: the backlight comes up with
    # nothing visible on it, so there is no pop. The loop then fades the real
    # frame up out of that black using the same eased curve as palette changes.
    dev.write(to_panel(black)); dev.flush()
    n = panel_on()
    print(f"panel on showing black ({n} report(s)); fading in over {fade_s}s")
    # Barkeep is deliberately stopped before suspend and started as a fresh
    # process after wake. A new process cannot observe a frame-time "resume
    # gap", while the T1 can ignore one early enable report during USB settle.
    # Retry this harmless HID feature report for a bounded startup window.
    panel_retry_until = time.time() + 10.0
    panel_retry_at = 0.0
    lit = True
    intro_t0 = time.time()
    period = 1.0 / fps
    last_frame = time.time()
    last_btn = None
    try:
        while True:
            state["fading"] = False
            row = current_row(state, fade_s)
            now = time.time()
            dt, last_frame = now - last_frame, now

            if dt > RESUME_GAP_S:
                print(f"resume: {dt:.0f}s gap - re-enabling the panel")
                dev.write(to_panel(black)); dev.flush()
                panel_on()
                intro_t0 = now              # rise out of black again
                state["last_input"] = now   # and show the keys on wake

            # Idle auto-hide. Ramp a phase 0..1 LINEARLY by elapsed time, then
            # ease the phase for the actual opacity. Doing it this way means an
            # interrupted fade reverses from exactly where it is - press a key
            # halfway through the fade-out and the keys come back from half,
            # with no jump - which a fixed start-time curve cannot do.
            btn, btn_fading = 1.0, False
            btn_changed = False
            if idle_s > 0:
                want = 1.0 if (now - state["last_input"]) < idle_s else 0.0
                p = state["btn_p"]
                step = dt / max(idle_in_s if want > p else idle_out_s, 1e-6)
                p = min(want, p + step) if want > p else max(want, p - step)
                state["btn_p"] = p
                btn = ease(p)
                btn_fading = 0.0 < p < 1.0
                btn_changed = btn != last_btn
            # Fade the WHOLE frame up from black on startup, using the same
            # eased curve as palette changes. The panel is lit while still
            # showing black, so there is no pop - it rises out of black.
            intro = 1.0
            if intro_t0 is not None:
                e = (time.time() - intro_t0) / max(fade_s, 1e-6)
                if e < 1.0:
                    intro = ease(e)
                else:
                    intro_t0 = None

            # Draw when something is changing. `flow` used to sit in this
            # condition directly, so with the default --flow 30 it was always
            # true and we rendered and pushed a full 390,600-byte frame every
            # tick forever - which is where this process's CPU went.
            animating = (state["dirty"] or state["fading"] or intro < 1.0
                         or btn_fading or btn_changed or now < panel_retry_until)
            if animating or flow:
                img = render(row, offset, state["pressed"], font,
                             LAYERS[state.get("layer", 0)], btn,
                             state.get("ind"), background=btn, black=black,
                             button_wallpaper=button_wallpaper)
                if intro < 1.0:
                    img = Image.blend(black, img, intro)
                dev.write(to_panel(img)); dev.flush()
                if now < panel_retry_until and now >= panel_retry_at:
                    threading.Thread(target=panel_on, daemon=True).start()
                    panel_retry_at = now + 0.5
                state["dirty"] = False
                last_btn = btn
            # Advance by ELAPSED TIME, not a fixed step per frame, so --flow is
            # honestly px/sec and the drift runs at the same visible speed
            # whichever rate we are rendering at.
            if flow:
                offset = (offset + flow * dt) % W
            # Idle - keys hidden and nothing in transition - is the cheap case:
            # keep drifting, just at fewer frames per second.
            idle_now = btn <= 0.002 and not animating
            # Static state is event-driven: avoid waking/rendering at 60 FPS.
            # Keep the fast cadence only while a fade/press/startup animation
            # is active; the short static poll still notices idle timeout and
            # watcher/input dirty flags promptly.
            if not animating and not flow:
                time.sleep(0.20)
            else:
                target = (1.0 / IDLE_FPS) if idle_now else period
                slack = target - (time.time() - now)
                if slack > 0:
                    time.sleep(slack)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        state["stop"] = True
        dev.close()


if __name__ == "__main__":
    main()
