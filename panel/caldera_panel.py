#!/usr/bin/env python3
"""RaspiAudiophile panel: now-playing display on the official 7" DSI touch
display (800x480, /dev/fb0, XRGB).

Data sources:
- Companion timeline on localhost:32500 -> state, volume, ratingKey, server
- Plex server -> track metadata, cover art, loudness envelope (token from
  Caldera's own preferences.json, never stored here)
- peppyalsa FIFO -> live L/R levels for the VU meters

Rendering: Pillow + numpy -> framebuffer. No X server involved.
Configuration: /etc/raspiaudiophile.conf (see the example in pi/etc/).
"""

import fcntl
import io
import select
import json
import math
import os
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

import configparser as _configparser

_CONF = _configparser.ConfigParser(interpolation=None)
_CONF.read(["/etc/raspiaudiophile.conf",
            str(Path.home() / ".config/raspiaudiophile.conf")])


def cfg(section: str, key: str, default, cast=None):
    """Config value from raspiaudiophile.conf, typed like the default."""
    try:
        raw = _CONF[section][key]
    except KeyError:
        return default
    cast = cast or type(default)
    try:
        if cast is bool:
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return cast(raw.strip())
    except (TypeError, ValueError):
        return default


FB = Path("/dev/fb0")   # official 7" DSI display, driven by the firmware
W, H = 800, 480
COVER = 340      # info view, top-left square
COVER_FS = 480   # fullscreen view
TIMELINE_URL = "http://localhost:32500/player/timeline/poll?wait=0&commandID=1"
PREFS = Path.home() / ".config/caldera-music/preferences.json"
POLL_S = 1.0
PAUSED_TO_IDLE_S = cfg("screen", "paused_to_idle_s", 600)
SCREEN_OFF_S = cfg("screen", "screen_off_s", 180)
BL_POWER = Path("/sys/class/backlight/rpi_backlight/bl_power")
SCREEN = {"on": True}
SHUTDOWN = {"on": False}

# Runtime-tunable values, seeded from config, editable in the settings menu
RT = {
    "brightness": cfg("display", "brightness", 100),
    "screen_off": cfg("screen", "screen_off_s", 180),
    "detent_div": cfg("encoder", "detent_divisor", 1),
    "vu_trim": cfg("vu", "ref_trim", 0.0),
    "def_skin": cfg("skins", "default", "amber"),
    "hold_off": cfg("encoder", "shutdown_hold_s", 6),
}
SETTINGS = {"on": False, "idx": 0, "at": 0.0}
BRIGHT_PATH = Path("/sys/class/backlight/rpi_backlight/brightness")


def apply_brightness() -> None:
    try:
        BRIGHT_PATH.write_text(str(max(13, RT["brightness"] * 255 // 100)))
    except OSError:
        pass
SEEN_PLAYING = {"yes": False}


def set_backlight(on: bool) -> None:
    try:
        BL_POWER.write_text("0" if on else "1")
        SCREEN["on"] = on
    except OSError:
        pass

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
F_TITLE = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 36)
F_TEXT = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans.ttf", 28)
F_SMALL = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans.ttf", 20)
F_DB = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 64)
F_FMT = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 32)
F_DBFS = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 104)
F_DBFS_DEC = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 46)

BG = (12, 12, 14)
FG = (235, 235, 235)
DIM = (150, 150, 150)
ACCENT = (240, 180, 60)


def token() -> str:
    return json.loads(PREFS.read_text())["plex"]["token"]


# Caldera's real volume curve, MEASURED 2026-08-19 (interleaved sweep,
# peppyalsa FIFO digital peaks + UCA202 analog loopback, Get Lucky as
# steady source): attenuation follows 55*log10(vol/100) within ~1 dB
# from vol 20 to 100 (i.e. amplitude = (vol/100)^2.75).
VOL_CURVE_DB = cfg("volume", "curve_db_per_decade", 55.0)


def vol_atten_db(vol: int) -> float:
    """Positive attenuation in dB applied by Caldera at this volume."""
    if vol <= 0:
        return 120.0
    return max(0.0, -VOL_CURVE_DB * math.log10(vol / 100.0))


def vu_comp_db(vol) -> float:
    """Meter compensation: add back Caldera's attenuation so the needles
    show source level. At mute there is nothing to compensate — without
    this guard the +120 dB mute figure pegged the needles full scale."""
    return vol_atten_db(vol) if vol > 0 else 0.0


def volume_db(vol: int) -> str:
    if vol <= 0:
        return "MUTE"
    return f"{-vol_atten_db(vol):.1f} dB"


_FB_FD = -1
FBIO_WAITFORVSYNC = 0x40044620
_VSYNC = {"ok": False}


def _vsync_probe() -> None:
    try:
        fd = os.open(str(FB), os.O_RDWR)
        fcntl.ioctl(fd, FBIO_WAITFORVSYNC, 0)
        fcntl.ioctl(fd, FBIO_WAITFORVSYNC, 0)
        os.close(fd)
        _VSYNC["ok"] = True
    except OSError:
        pass


def init_vsync() -> None:
    """Some boots never deliver a vblank IRQ on the firmware fb: the
    WAITFORVSYNC ioctl then blocks FOREVER (panel frozen in D state,
    knob dead). Probe it in a throwaway thread with a timeout; only
    trust it if it answers twice within half a second."""
    t = threading.Thread(target=_vsync_probe, daemon=True)
    t.start()
    t.join(0.5)


def fb_out(buf: bytes) -> None:
    """Write a full frame; vsync'd only when this boot's vblank works."""
    global _FB_FD
    if _FB_FD < 0:
        _FB_FD = os.open(str(FB), os.O_RDWR)
    if _VSYNC["ok"]:
        try:
            fcntl.ioctl(_FB_FD, FBIO_WAITFORVSYNC, 0)
        except OSError:
            pass
    os.pwrite(_FB_FD, buf, 0)


def fb_write(img: Image.Image) -> None:
    # firmware fb is 32bpp XRGB little-endian: byte order B,G,R,X
    fb_out(img.convert("RGB").tobytes("raw", "BGRX"))


def timeline() -> dict | None:
    try:
        r = requests.get(TIMELINE_URL, timeout=3)
        r.raise_for_status()
    except requests.RequestException:
        return None
    for tl in ET.fromstring(r.text):
        if tl.get("type") == "music":
            return tl.attrib
    return None


FALLBACK_SERVER = cfg("plex", "server", "")


def server_base(tl: dict) -> str:
    # prefer the configured LAN server: the timeline sometimes advertises
    # the remote plex.direct route, unreachable from inside the network
    # (hairpin NAT)
    return FALLBACK_SERVER or server_base_alt(tl) or ""


def server_base_alt(tl: dict) -> str | None:
    if tl.get("address") and tl.get("port"):
        return f"{tl.get('protocol', 'http')}://{tl['address']}:{tl['port']}"
    return None


def fetch_meta(tl: dict) -> dict:
    try:
        r = requests.get(f"{server_base(tl)}{tl['key']}",
                         params={"X-Plex-Token": token()}, timeout=4)
        r.raise_for_status()
    except requests.RequestException:
        alt = server_base_alt(tl)
        if not alt:
            raise
        r = requests.get(f"{alt}{tl['key']}",
                         params={"X-Plex-Token": token()}, timeout=6)
        r.raise_for_status()
    track = ET.fromstring(r.text).find("Track")
    media = track.find("Media") if track is not None else None
    part = media.find("Part") if media is not None else None
    st = None
    if part is not None:
        st = part.find("Stream")
    stream_id = st.get("id") if st is not None else None
    fmt = ""
    if media is not None:
        codec = (media.get("audioCodec") or "").upper()
        khz = ""
        bits = ""
        if st is not None:
            sr = st.get("samplingRate")
            bd = st.get("bitDepth")
            khz = f"{int(sr) / 1000:g} kHz" if sr else ""
            bits = f"{bd}-bit" if bd else ""
        quality = " / ".join(x for x in (khz, bits) if x)
        fmt = " ".join(x for x in (codec, quality) if x)
    return {
        "title": track.get("title", "?") if track is not None else "?",
        "artist": track.get("grandparentTitle", "") if track is not None else "",
        "album": track.get("parentTitle", "") if track is not None else "",
        # some tracks carry no own thumb: fall back to album then artist art
        "thumb": (track.get("thumb") or track.get("parentThumb")
                  or track.get("grandparentThumb") or "") if track is not None else "",
        "format": fmt,
        "stream_id": stream_id,
    }


def fetch_cover(tl: dict, thumb: str) -> Image.Image | None:
    if not thumb:
        return None
    url = f"{server_base(tl)}/photo/:/transcode"
    params = {
        "width": COVER_FS,
        "height": COVER_FS,
        "minSize": 1,
        "url": thumb,
        "X-Plex-Token": token(),
    }
    try:
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except (requests.RequestException, OSError):
        return None


WAVE_COLS = W - 32


def fetch_levels(tl: dict, stream_id: str | None):
    """Per-pixel waveform amplitude, precomputed once per track."""
    if not stream_id:
        return None
    url = f"{server_base(tl)}/library/streams/{stream_id}/levels"
    try:
        r = requests.get(url, params={"X-Plex-Token": token()}, timeout=8)
        r.raise_for_status()
        lv = np.array([float(x.get("v", "-60")) for x in ET.fromstring(r.text)])
    except (requests.RequestException, ET.ParseError, ValueError):
        return None
    if lv.size < 2:
        return None
    lin = np.power(10.0, lv / 20.0)
    edges = np.linspace(0, len(lin), WAVE_COLS + 1).astype(int)
    amp = np.array([lin[a:b].mean() if b > a else lin[min(a, len(lin) - 1)]
                    for a, b in zip(edges[:-1], edges[1:])])
    lo, hi = np.percentile(amp, 8), np.percentile(amp, 99)
    amp = np.clip((amp - lo) / max(hi - lo, 1e-9), 0.0, 1.0)
    amp = np.power(amp, 1.6)  # gamma: quiet parts stay visibly small
    amp = np.clip(amp, 0.012, 1.0)
    amp = np.convolve(amp, np.ones(3) / 3, mode="same")
    return prerender_wave(amp)


WAVE_H_HALF = 32


def prerender_wave(amp: np.ndarray) -> dict:
    """Two ready strips (played/unplayed); per-frame drawing = two pastes."""
    h = WAVE_H_HALF * 2 + 1
    strips = {}
    for key, color in (("on", ACCENT), ("off", (95, 95, 104))):
        im = Image.new("RGB", (WAVE_COLS, h), BG)
        dd = ImageDraw.Draw(im)
        dd.line((0, WAVE_H_HALF, WAVE_COLS - 1, WAVE_H_HALF), fill=(95, 95, 104))
        hs = np.maximum(1, (WAVE_H_HALF * amp).astype(int))
        for i in range(WAVE_COLS):
            dd.line((i, WAVE_H_HALF - hs[i], i, WAVE_H_HALF + hs[i]), fill=color)
        strips[key] = im
    return strips


def ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def wrap2(draw, text, font, max_w):
    """Wrap into at most 2 lines, ellipsize the second."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
            if len(lines) == 2:
                break
    if cur and len(lines) < 2:
        lines.append(cur)
    if len(lines) == 2:
        lines[1] = ellipsize(draw, lines[1], font, max_w)
    return lines


VIEW = {"mode": 0}  # 0 info, 1 fullscreen cover, 2 VU meters


SETTING_ITEMS = [
    ("Brightness", "brightness"),
    ("Screen off", "screen_off"),
    ("Knob sensitivity", "detent_div"),
    ("VU zero trim", "vu_trim"),
    ("Default VU skin", "def_skin"),
    ("Shutdown hold", "hold_off"),
]
_SCREEN_OFF_STEPS = [60, 180, 300, 600, 1800, 0]   # 0 = never


def _fmt_setting(key) -> str:
    v = RT[key]
    if key == "brightness":
        return f"{v} %"
    if key == "screen_off":
        return "never" if v == 0 else f"{v // 60} min"
    if key == "detent_div":
        return "fast" if v <= 1 else "fine"
    if key == "vu_trim":
        return f"{v:+.2f} dB"
    if key == "hold_off":
        return f"{v} s"
    return str(v)


def settings_adjust(d: int) -> None:
    """Knob rotation edits the highlighted item."""
    SETTINGS["at"] = time.monotonic()
    key = SETTING_ITEMS[SETTINGS["idx"]][1]
    if key == "brightness":
        RT["brightness"] = max(10, min(100, RT["brightness"] + 5 * d))
        apply_brightness()
    elif key == "screen_off":
        i = _SCREEN_OFF_STEPS.index(RT["screen_off"]) if RT["screen_off"] in _SCREEN_OFF_STEPS else 1
        RT["screen_off"] = _SCREEN_OFF_STEPS[max(0, min(len(_SCREEN_OFF_STEPS) - 1, i + d))]
    elif key == "detent_div":
        RT["detent_div"] = 2 if d > 0 else 1
    elif key == "vu_trim":
        RT["vu_trim"] = max(-3.0, min(3.0, round(RT["vu_trim"] + 0.25 * d, 2)))
    elif key == "hold_off":
        RT["hold_off"] = max(3, min(10, RT["hold_off"] + d))
    elif key == "def_skin":
        i = SKIN_LIST.index(RT["def_skin"]) if RT["def_skin"] in SKIN_LIST else 0
        i = (i + d) % len(SKIN_LIST)
        RT["def_skin"] = SKIN_LIST[i]
        VU_SKIN["i"] = i               # apply live so the choice is visible


def settings_next() -> None:
    SETTINGS["at"] = time.monotonic()
    SETTINGS["idx"] = (SETTINGS["idx"] + 1) % len(SETTING_ITEMS)


def settings_save_and_exit() -> None:
    """Persist to the user config (read layered after /etc) and leave."""
    import configparser
    path = Path.home() / ".config/raspiaudiophile.conf"
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path)
    def put(sec, key, val):
        if not cp.has_section(sec):
            cp.add_section(sec)
        cp.set(sec, key, str(val))
    put("display", "brightness", RT["brightness"])
    put("screen", "screen_off_s", RT["screen_off"])
    put("encoder", "detent_divisor", RT["detent_div"])
    put("vu", "ref_trim", RT["vu_trim"])
    put("skins", "default", RT["def_skin"])
    put("encoder", "shutdown_hold_s", RT["hold_off"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            cp.write(f)
    except OSError:
        pass
    SETTINGS["on"] = False


SET_ROW_Y0, SET_ROW_H = 92, 50
SET_BTN_Y = 410
SET_BTNS = {   # name -> (x0, x1) at SET_BTN_Y..H
    "back": (16, 260),
    "reboot": (300, 500),
    "poweroff": (540, 784),
}


def render_settings() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((16, 14), "Settings", font=F_TITLE, fill=FG)
    d.text((W - 16, 30), "rotate = adjust   press = next",
           font=F_SMALL, fill=DIM, anchor="rm")
    d.line((16, 74, W - 16, 74), fill=(60, 60, 65), width=2)
    for i, (label, key) in enumerate(SETTING_ITEMS):
        y = SET_ROW_Y0 + i * SET_ROW_H
        sel = i == SETTINGS["idx"]
        if sel:
            d.rounded_rectangle((10, y - 8, W - 10, y + SET_ROW_H - 22),
                                radius=8, fill=(38, 32, 20))
        d.text((28, y + 12), label, font=F_TEXT,
               fill=ACCENT if sel else FG, anchor="lm")
        d.text((W - 28, y + 12), _fmt_setting(key), font=F_TEXT,
               fill=FG if sel else DIM, anchor="rm")
    for name, (x0, x1) in SET_BTNS.items():
        col = {"back": (60, 60, 68), "reboot": (70, 55, 25), "poweroff": (80, 30, 25)}[name]
        d.rounded_rectangle((x0, SET_BTN_Y, x1, H - 14), radius=10, fill=col)
        lbl = {"back": "Back", "reboot": "Reboot", "poweroff": "Power off"}[name]
        d.text(((x0 + x1) // 2, (SET_BTN_Y + H - 14) // 2), lbl,
               font=F_FMT, fill=FG, anchor="mm")
    return img


def touch_listener() -> None:
    try:
        import evdev
    except ImportError:
        return
    dev = None
    for path in evdev.list_devices():
        d = evdev.InputDevice(path)
        if "ADS7846" in d.name or "raspberrypi-ts" in d.name:
            dev = d
            break
    if dev is None:
        return
    last = 0.0
    cur_x = cur_y = None
    for ev in dev.read_loop():
        if ev.type == evdev.ecodes.EV_ABS:
            if ev.code in (evdev.ecodes.ABS_X, evdev.ecodes.ABS_MT_POSITION_X):
                cur_x = ev.value
            elif ev.code in (evdev.ecodes.ABS_Y, evdev.ecodes.ABS_MT_POSITION_Y):
                cur_y = ev.value
        elif (ev.type == evdev.ecodes.EV_KEY
              and ev.code == evdev.ecodes.BTN_TOUCH and ev.value == 0):
            now = time.monotonic()
            if now - last <= 0.4:  # debounce
                continue
            last = now
            if not SCREEN["on"]:
                set_backlight(True)         # wake only, keep the current view
                SCREEN["wake_at"] = now
            elif SETTINGS["on"]:
                if cur_x is None or cur_y is None:
                    continue
                SETTINGS["at"] = now
                if cur_y >= SET_BTN_Y:
                    if SET_BTNS["back"][0] <= cur_x <= SET_BTNS["back"][1]:
                        settings_save_and_exit()
                    elif SET_BTNS["reboot"][0] <= cur_x <= SET_BTNS["reboot"][1]:
                        settings_save_and_exit()
                        os.system("sudo /sbin/reboot")
                    elif SET_BTNS["poweroff"][0] <= cur_x <= SET_BTNS["poweroff"][1]:
                        settings_save_and_exit()
                        SHUTDOWN["on"] = True
                        time.sleep(0.1)
                        img = Image.new("RGB", (W, H), BG)
                        ImageDraw.Draw(img).text((W // 2, H // 2), "Shutting down...",
                                                 font=F_TITLE, fill=FG, anchor="mm")
                        fb_write(img)
                        os.system("sudo /sbin/poweroff")
                elif SET_ROW_Y0 - 10 <= cur_y:
                    i = (cur_y - (SET_ROW_Y0 - 10)) // SET_ROW_H
                    if 0 <= i < len(SETTING_ITEMS):
                        SETTINGS["idx"] = int(i)   # tap a row to select it
            elif cur_x is not None and cur_y is not None \
                    and cur_y < 110 and cur_x > W - 190:
                # top-right corner in any view: open the settings menu
                SETTINGS["on"] = True
                SETTINGS["idx"] = 0
                SETTINGS["at"] = now
            elif (VIEW["mode"] == 2 and len(SKIN_LIST) > 1
                  and cur_x is not None and cur_y is not None and cur_y > H - 160):
                # the whole bottom strip in the VU view belongs to skin
                # switching: left third = previous, right third = next,
                # middle inert — an imprecise tap must never change view
                if cur_x < W // 3:
                    VU_SKIN["i"] = (VU_SKIN["i"] - 1) % len(SKIN_LIST)
                    VU_SKIN["at"] = now
                elif cur_x > W - W // 3:
                    VU_SKIN["i"] = (VU_SKIN["i"] + 1) % len(SKIN_LIST)
                    VU_SKIN["at"] = now
            else:
                VIEW["mode"] = (VIEW["mode"] + 1) % 3


def render(state: str, vol: int, meta: dict, cover: Image.Image | None,
           t_ms: int, dur_ms: int, levels: list[float] | None = None) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    if cover is not None:
        img.paste(cover.resize((COVER, COVER)), (16, 16))
        d.rectangle((15, 15, 16 + COVER, 16 + COVER), outline=(210, 210, 215), width=1)
    else:
        d.rectangle((16, 16, 16 + COVER, 16 + COVER), fill=(30, 30, 34))
        d.text((16 + COVER // 2, 16 + COVER // 2), "♪", font=F_TITLE, fill=DIM, anchor="mm")

    x = COVER + 40
    col_w = W - x - 16

    y = 20
    for line in wrap2(d, meta["title"], F_TITLE, col_w):
        d.text((x, y), line, font=F_TITLE, fill=FG)
        y += 44
    d.text((x, y + 10), ellipsize(d, meta["artist"], F_TEXT, col_w), font=F_TEXT, fill=FG)
    d.text((x, y + 50), ellipsize(d, meta["album"], F_TEXT, col_w), font=F_TEXT, fill=DIM)
    d.text((x, y + 98), ellipsize(d, meta["format"], F_FMT, col_w), font=F_FMT, fill=(120, 200, 255))
    icon = {"playing": "▶", "paused": "⏸"}.get(state, "⏹")
    d.text((x, y + 140), f"{icon} {state}", font=F_SMALL, fill=ACCENT)

    # volume row: [bar][big number][small dB], bar centered on the minus sign
    num = volume_db(vol)
    if num.endswith(" dB"):
        main, unit = num[:-3], "dB"
    else:
        main, unit = num, ""
    tw_unit = d.textlength(" " + unit, font=F_FMT) if unit else 0
    d.text((W - 16 - tw_unit, 352), main, font=F_DB, fill=FG, anchor="rs")
    if unit:
        d.text((W - 16 - tw_unit + 6, 352), unit, font=F_FMT, fill=FG, anchor="ls")
    tw_main = d.textlength(main, font=F_DB)
    bx1 = int(W - 16 - tw_unit - tw_main - 26)
    yc = 352 - 20                       # optical center of the minus sign
    if bx1 > x + 50:
        d.rectangle((x, yc - 8, bx1, yc + 8), fill=(50, 50, 55))
        d.rectangle((x, yc - 8, x + int((bx1 - x) * vol / 100), yc + 8), fill=ACCENT)

    # waveform seekbar: two pastes from the prerendered strips
    if dur_ms > 0:
        yc2, half_max = 440, WAVE_H_HALF
        x0 = 16
        cols = WAVE_COLS
        frac = min(t_ms / dur_ms, 1.0)
        played = max(1, int(cols * frac))
        if levels is not None:
            img.paste(levels["off"], (x0, yc2 - half_max))
            img.paste(levels["on"].crop((0, 0, played, half_max * 2 + 1)),
                      (x0, yc2 - half_max))
        else:
            d.rectangle((x0, yc2 - 3, x0 + cols, yc2 + 3), fill=(50, 50, 55))
            d.rectangle((x0, yc2 - 3, x0 + played, yc2 + 3), fill=ACCENT)
        mins = lambda ms: f"{ms // 60000}:{ms % 60000 // 1000:02d}"
        d.text((x0, yc2 - half_max - 24), mins(t_ms), font=F_SMALL, fill=DIM)
        d.text((x0 + cols, yc2 - half_max - 24), mins(dur_ms), font=F_SMALL, fill=DIM, anchor="ra")
    return img


def render_fullscreen(cover: Image.Image | None, vol: int,
                      show_vol: bool) -> Image.Image:
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    if cover is not None:
        img.paste(cover.resize((COVER_FS, COVER_FS)), (0, 0))
        d.rectangle((0, 0, COVER_FS - 1, COVER_FS - 1), outline=(210, 210, 215), width=1)
    cx = COVER_FS + (W - COVER_FS) // 2
    d.text((cx, 40), "V O L U M E", font=F_SMALL, fill=DIM, anchor="mm")
    num = volume_db(vol)
    if num.endswith(" dB"):
        num = num[:-3]
    if "." in num:
        ip, dec = num.split(".")
        wi = d.textlength(ip, font=F_DBFS)
        wd = d.textlength("." + dec, font=F_DBFS_DEC)
        x0 = cx - (wi + wd) / 2
        d.text((x0, 150), ip, font=F_DBFS, fill=FG, anchor="ls")
        d.text((x0 + wi, 150), "." + dec, font=F_DBFS_DEC, fill=FG, anchor="ls")
    else:
        d.text((cx, 110), num, font=F_DBFS, fill=FG, anchor="mm")
    d.text((cx, 200), "dB", font=F_DBFS_DEC, fill=FG, anchor="mm")
    bx0, bx1, by0, by1 = cx - 22, cx + 22, 250, 450
    d.rectangle((bx0, by0, bx1, by1), fill=(40, 40, 45))
    top = by1 - int((by1 - by0) * vol / 100)
    d.rectangle((bx0, top, bx1, by1), fill=ACCENT)
    return img


VU_MIN, VU_MAX = -20.0, 3.0
VU_REF_DBFS = cfg("vu", "ref_dbfs", 0.0)   # 0 VU vs peppyalsa PEAK levels
VU_LEVELS = {"l": -60.0, "r": -60.0}


VU_FIFO = "/tmp/peppyalsa_fifo"


def vu_capture() -> None:
    # Levels come from the peppyalsa scope plugin inside the ALSA chain
    # (pcm.caldera_tap): one uint32 per update on the FIFO, left channel in
    # the low 16 bits, right in the high 16, linear scale 0..100 (meter_max).
    # The writer closes the FIFO when playback stops -> read() returns b"".
    while True:
        fd = -1
        try:
            fd = os.open(VU_FIFO, os.O_RDONLY)  # blocks until a writer opens
            last = time.monotonic()
            while True:
                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:
                    # writer alive but silent (pause): levels are stale,
                    # let the needles fall to rest instead of freezing
                    VU_LEVELS["l"] = VU_LEVELS["r"] = -90.0
                    continue
                data = os.read(fd, 4096)
                if not data:
                    raise EOFError("fifo writer closed")
                word = int.from_bytes(data[-4:], "little")
                now = time.monotonic()
                alpha = min(1.0, (now - last) / 0.065)  # VU: 99% in 300 ms
                last = now
                for ch, v in (("l", word & 0xFFFF), ("r", (word >> 16) & 0xFFFF)):
                    # v == 0 is true silence: floor deep enough that the
                    # volume compensation can never lift it off the pin
                    db = 20 * math.log10(v / 100.0) if v > 0 else -90.0
                    VU_LEVELS[ch] += (db - VU_LEVELS[ch]) * alpha
        except Exception:
            VU_LEVELS["l"] = VU_LEVELS["r"] = -60.0
            time.sleep(1)
        finally:
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass


VU_ANCHORS = (-20, -10, -7, -5, -3, -2, -1, 0, 1, 2, 3)
_ANCHOR_POS = np.linspace(0.0, 1.0, len(VU_ANCHORS))


def _vu_angle(db_vu: float) -> float:
    db = max(VU_MIN, min(VU_MAX, db_vu))
    f = float(np.interp(db, VU_ANCHORS, _ANCHOR_POS))  # even tick spacing
    return math.radians(-35 + f * 70)


VU_MW, VU_MH = 386, 386          # face size
VU_FACE_Y = 8
VU_PIVOT_Y = 402                 # pivot below the face bottom (hidden)
VU_R_ARC = 262                   # main scale arc radius
VU_R_NEEDLE = 312
INK = (30, 22, 12)
RED = (200, 30, 15)


def _amber_face(w: int, h: int) -> Image.Image:
    """Warm amber top fading to pale cream, with a soft center glow."""
    yy, xx = np.mgrid[0:h, 0:w]
    # tungsten-lamp glow: bright warm pool upper-center, deep amber corners
    g1 = np.exp(-(((xx - w / 2) / (w * 0.46)) ** 2 + ((yy - h * 0.30) / (h * 0.42)) ** 2))
    g2 = np.exp(-(((xx - w / 2) / (w * 0.95)) ** 2 + ((yy - h * 0.85) / (h * 0.8)) ** 2)) * 0.35
    glow = np.clip(g1 + g2, 0, 1)
    r = np.clip(196 + 60 * glow, 0, 255).astype(np.uint8)
    g_ = np.clip(122 + 106 * glow, 0, 255).astype(np.uint8)
    b = np.clip(28 + 132 * glow, 0, 255).astype(np.uint8)
    return Image.fromarray(np.dstack([r, g_, b]))


def _gen_needle_sprite(cx, py, tip_x, tip_y, w0=2.8, w1=0.35,
                       color=(12, 20, 28), sh_dx=5.0, sh_dy=6.0):
    """Pre-composited needle+shadow RGBA sprite.

    Returns (Image, x0, y0) to paste at (x0, y0). The cast shadow (the
    needle alpha shifted by sh_dx/sh_dy, 18% black) is folded into the
    same alpha so a single paste draws both."""
    m = 14
    x0 = int(min(cx, tip_x)) - m
    x1 = int(max(cx, tip_x)) + m + 1
    y0 = int(min(py, tip_y)) - m
    y1 = int(max(py, tip_y)) + m + 2
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    dx, dy = tip_x - cx, tip_y - py
    L2 = float(dx * dx + dy * dy) or 1.0
    t = np.clip(((xx - cx) * dx + (yy - py) * dy) / L2, 0.0, 1.0)
    ex = xx - (cx + t * dx)
    ey = yy - (py + t * dy)
    dist = np.sqrt(ex * ex + ey * ey)
    width = w0 + (w1 - w0) * t
    a = np.clip((width + 1.1 - dist) / 1.1, 0.0, 1.0)
    sh = np.zeros_like(a)
    sy, sx = int(round(sh_dy)), int(round(sh_dx))
    hh, ww = a.shape
    ty0, ty1 = max(0, sy), min(hh, hh + sy)
    tx0, tx1 = max(0, sx), min(ww, ww + sx)
    if ty1 > ty0 and tx1 > tx0:
        sh[ty0:ty1, tx0:tx1] = a[ty0 - sy:ty1 - sy, tx0 - sx:tx1 - sx]
    a_sh = sh * 0.18
    out_a = a + a_sh * (1.0 - a)          # shadow under the needle
    colf = np.array((color[2], color[1], color[0]), np.float32)  # BGR -> RGB
    rgb = np.zeros(a.shape + (3,), np.float32)
    safe = out_a > 0.002
    rgb[safe] = colf[None, :] * (a[safe] / out_a[safe])[:, None]
    rgba = np.empty(a.shape + (4,), np.uint8)
    rgba[..., :3] = rgb.astype(np.uint8)
    rgba[..., 3] = (out_a * 255.0).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA"), x0, y0


_NEEDLE_LRU: dict = {}


def needle_sprite(kind: str, angle: float, cx, py, tip_x, tip_y, **kw):
    """LRU cache of PIL needle sprites, quantized to ~0.11 degrees."""
    key = (kind, int(round(angle * 300)))
    spr = _NEEDLE_LRU.get(key)
    if spr is None:
        spr = _gen_needle_sprite(cx, py, tip_x, tip_y, **kw)
        if len(_NEEDLE_LRU) > 48:    # RAM cap: ~7 MB worst case
            _NEEDLE_LRU.clear()
        _NEEDLE_LRU[key] = spr
    return spr


def paste_sprite(frame: Image.Image, spr, ox: int = 0, oy: int = 0, clip=None):
    """Paste a cached sprite; returns the touched PIL box (x0, y0, x1, y1).
    clip=(x0, y0, x1, y1) confines the paste (e.g. to the meter face, so
    the needle tail cannot spill over the waveform below)."""
    img, sx0, sy0 = spr
    x0, y0 = sx0 + ox, sy0 + oy
    if clip is not None:
        cx0, cy0, cx1, cy1 = clip
        ix0, iy0 = max(x0, cx0), max(y0, cy0)
        ix1 = min(x0 + img.width, cx1)
        iy1 = min(y0 + img.height, cy1)
        if ix1 <= ix0 or iy1 <= iy0:
            return (0, 0, 0, 0)
        img = img.crop((ix0 - x0, iy0 - y0, ix1 - x0, iy1 - y0))
        x0, y0 = ix0, iy0
    frame.paste(img, (x0, y0), img)
    return (max(0, x0), max(0, y0),
            min(W, x0 + img.width), min(H, y0 + img.height))


SS = 3  # supersampling factor for static faces


def make_vu_base() -> Image.Image:
    img = Image.new("RGB", (W, H), (10, 9, 8))
    f_lab = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 17 * SS)
    f_vu = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 52 * SS)
    f_ch = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans.ttf", 17 * SS)
    for mx, name in ((8, "LEFT"), (406, "RIGHT")):
        face = _amber_face(VU_MW * SS, VU_MH * SS)
        d = ImageDraw.Draw(face)
        cx, py = VU_MW * SS // 2, VU_PIVOT_Y * SS

        def pt(db, r):
            a = _vu_angle(db)
            return (cx + r * SS * math.sin(a), py - r * SS * math.cos(a))

        pts = [pt(VU_MIN + (0 - VU_MIN) * i / 60, VU_R_ARC) for i in range(61)]
        d.line(pts, fill=INK, width=6 * SS, joint="curve")
        segs = 24
        for i in range(segs):
            t = i / segs
            col = (int(205 + 20 * t), int(115 - 90 * t), int(25 - 10 * t))
            a1 = _vu_angle(VU_MAX * i / segs)
            a2 = _vu_angle(VU_MAX * (i + 1) / segs)
            d.line((cx + VU_R_ARC * SS * math.sin(a1), py - VU_R_ARC * SS * math.cos(a1),
                    cx + VU_R_ARC * SS * math.sin(a2), py - VU_R_ARC * SS * math.cos(a2)),
                   fill=col, width=9 * SS)
        majors = (-20, -10, -7, -5, -3, -2, -1, 0, 1, 2, 3)
        for db in majors:
            if db >= 2:
                col = (200, 30, 18)
            elif db == 1:
                col = (212, 122, 22)
            else:
                col = INK
            x1, y1 = pt(db, VU_R_ARC)
            x2, y2 = pt(db, VU_R_ARC + 24)
            d.line((x1, y1, x2, y2), fill=col, width=5 * SS)
            xl, yl = pt(db, VU_R_ARC + 42)
            lbl = "0" if db == 0 else (f"+{db}" if db > 0 else f"−{-db}")
            d.text((xl, yl), lbl, font=f_lab, fill=col, anchor="ms")

        def minors_between(a, b, n):
            for i in range(1, n):
                yield a + (b - a) * i / n
        for a0, b0 in zip(majors[:-1], majors[1:]):
            for db in minors_between(a0, b0, 4):
                if db <= 0:
                    col = (55, 44, 32)
                elif db < 1:
                    col = (212, 122, 22)
                else:
                    col = (200, 30, 18)
                x1, y1 = pt(db, VU_R_ARC - 5)
                x2, y2 = pt(db, VU_R_ARC - 30)
                d.line((x1, y1, x2, y2), fill=col, width=1 * SS)
        for db, col in ((VU_MIN, INK), (VU_MAX, (200, 30, 18))):
            x1, y1 = pt(db, VU_R_ARC + 2)
            x2, y2 = pt(db, VU_R_ARC - 34)
            d.line((x1, y1, x2, y2), fill=col, width=5 * SS)
        d.text((cx, 252 * SS), name, font=f_ch, fill=(70, 45, 18), anchor="mm")
        f_corner = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 26 * SS)
        d.text((VU_MW * SS - 26 * SS, 26 * SS), name[0], font=f_corner, fill=(110, 80, 42), anchor="mm")
        face = face.resize((VU_MW, VU_MH), Image.LANCZOS)
        img.paste(face, (mx, VU_FACE_Y))
        dd = ImageDraw.Draw(img)
        dd.rectangle((mx - 1, VU_FACE_Y - 1, mx + VU_MW + 1, VU_FACE_Y + VU_MH + 1),
                     outline=(5, 5, 5), width=6)
    return img


FS_FACE_W, FS_FACE_H = 296, 248
FS_FACE_X, FS_FACE_Y = 492, 14
FS_SCALE = FS_FACE_W / 386
FS_PIVOT_Y = int(402 * FS_SCALE)
FS_R_ARC = int(262 * FS_SCALE)
FS_R_NEEDLE = int(312 * FS_SCALE)


def make_vu_face_small() -> Image.Image:
    f_lab = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 12 * SS)
    f_vu = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 32 * SS)
    face = _amber_face(FS_FACE_W * SS, FS_FACE_H * SS)
    d = ImageDraw.Draw(face)
    cx, py = FS_FACE_W * SS // 2, FS_PIVOT_Y * SS

    def pt(db, r):
        a = _vu_angle(db)
        return (cx + r * SS * math.sin(a), py - r * SS * math.cos(a))

    pts = [pt(VU_MIN + (0 - VU_MIN) * i / 60, FS_R_ARC) for i in range(61)]
    d.line(pts, fill=INK, width=4 * SS, joint="curve")
    segs = 20
    for i in range(segs):
        t = i / segs
        col = (int(205 + 20 * t), int(115 - 90 * t), int(25 - 10 * t))
        a1 = _vu_angle(VU_MAX * i / segs)
        a2 = _vu_angle(VU_MAX * (i + 1) / segs)
        d.line((cx + FS_R_ARC * SS * math.sin(a1), py - FS_R_ARC * SS * math.cos(a1),
                cx + FS_R_ARC * SS * math.sin(a2), py - FS_R_ARC * SS * math.cos(a2)),
               fill=col, width=6 * SS)
    for db in (-20, -10, -7, -5, -3, -2, -1, 0, 1, 2, 3):
        if db >= 2:
            col = (200, 30, 18)
        elif db == 1:
            col = (212, 122, 22)
        else:
            col = INK
        x1, y1 = pt(db, FS_R_ARC)
        x2, y2 = pt(db, FS_R_ARC + 16)
        d.line((x1, y1, x2, y2), fill=col, width=3 * SS)
        xl, yl = pt(db, FS_R_ARC + 24)
        lbl = "0" if db == 0 else (f"+{db}" if db > 0 else f"−{-db}")
        d.text((xl, yl), lbl, font=f_lab, fill=col, anchor="ms")
    d.text((cx, int(FS_FACE_H * 0.78) * SS), "VU", font=f_vu, fill=(45, 36, 26), anchor="mm")
    return face.resize((FS_FACE_W, FS_FACE_H), Image.LANCZOS)


FS_FACE: Image.Image | None = None
FS_CACHE: dict = {"img": None, "key": None}
FS_WORK: dict = {"img": None, "src": None, "rect": None}
FS_DISP = {"m": VU_MIN}


def render_fullscreen_fb(cover: Image.Image | None, vol: int, key, fmt: str = "",
                         t_ms: int = 0, dur_ms: int = 0, levels=None) -> bytes:
    global FS_FACE
    if FS_FACE is None:
        FS_FACE = make_vu_face_small()
    fs_played = 0
    if dur_ms > 0 and levels is not None:
        fs_played = max(1, int(296 * min(t_ms / dur_ms, 1.0)))
    ck = (key, vol, fmt, fs_played // 4)
    if FS_CACHE["img"] is None or FS_CACHE["key"] != ck:
        base = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(base)
        if cover is not None:
            base.paste(cover.resize((COVER_FS, COVER_FS)), (0, 0))
            d.rectangle((0, 0, COVER_FS - 1, COVER_FS - 1), outline=(210, 210, 215), width=1)
        base.paste(FS_FACE, (FS_FACE_X, FS_FACE_Y))
        d.rectangle((FS_FACE_X - 1, FS_FACE_Y - 1, FS_FACE_X + FS_FACE_W,
                     FS_FACE_Y + FS_FACE_H), outline=(5, 5, 5), width=4)
        ccx = FS_FACE_X + FS_FACE_W // 2
        if fmt:
            f_fmt_s = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 22)
            d.text((ccx, 300), fmt, font=f_fmt_s, fill=(120, 200, 255), anchor="mm")
        if levels is not None and dur_ms > 0:
            wh = 27
            off = levels["off"].resize((296, wh * 2 + 1))
            on = levels["on"].resize((296, wh * 2 + 1))
            wy = 330
            base.paste(off, (492, wy))
            if fs_played > 0:
                base.paste(on.crop((0, 0, fs_played, wh * 2 + 1)), (492, wy))
        num = volume_db(vol)
        if num.endswith(" dB"):
            main, unit = num[:-3], " dB"
        else:
            main, unit = num, ""
        f_num = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 68)
        wm = d.textlength(main, font=f_num)
        wu = d.textlength(unit, font=F_FMT) if unit else 0
        x0 = ccx - (wm + wu) / 2
        d.text((x0, 466), main, font=f_num, fill=FG, anchor="ls")
        if unit:
            d.text((x0 + wm, 466), unit, font=F_FMT, fill=FG, anchor="ls")
        FS_CACHE["img"] = base
        FS_CACHE["key"] = ck
    src = FS_CACHE["img"]
    if FS_WORK["src"] is not src:
        FS_WORK.update(img=src.copy(), src=src, rect=None)
    frame = FS_WORK["img"]
    if FS_WORK["rect"]:
        box = FS_WORK["rect"]
        frame.paste(src.crop(box), box)

    now = time.monotonic()
    dt = min(0.3, now - _VU_LAST_T["t"]) if _VU_LAST_T["t"] else 0.03
    atten = vu_comp_db(vol)
    target = max(VU_LEVELS["l"], VU_LEVELS["r"]) - (VU_REF_DBFS + RT["vu_trim"]) + atten
    FS_DISP["m"] = vu_step(FS_DISP["m"], target, dt)
    a = _vu_angle(FS_DISP["m"])
    cx, py = FS_FACE_W // 2, FS_PIVOT_Y
    tip_x = cx + FS_R_NEEDLE * math.sin(a)
    tip_y = py - FS_R_NEEDLE * math.cos(a)
    spr = needle_sprite("fs", a, cx, py, tip_x, tip_y, w0=2.2, w1=0.3,
                        color=(12, 20, 28), sh_dx=4.5 * math.sin(a), sh_dy=4.5)
    FS_WORK["rect"] = paste_sprite(frame, spr, ox=FS_FACE_X, oy=FS_FACE_Y,
                                   clip=(FS_FACE_X, FS_FACE_Y,
                                         FS_FACE_X + FS_FACE_W, FS_FACE_Y + FS_FACE_H))
    return frame.tobytes("raw", "BGRX")


VU_BASE: Image.Image | None = None
VU_CACHE: dict = {"img": None, "played": -1}
VU_WORK: dict = {"static": None, "img": None, "src": None,
                 "vol": None, "fmt": None, "label": None, "rects": []}
VU_VOLTXT = {"vol": None, "arr": None}
VU_FMTTXT = {"fmt": None, "arr": None}
VU_FMT_X = 8 + 10
VU_TXT_W, VU_TXT_H = 170, 46
VU_TXT_X = 404 + VU_MW - VU_TXT_W - 10
VU_TXT_Y = VU_FACE_Y + VU_MH - VU_TXT_H - 8
VU_DISP = {"l": VU_MIN, "r": VU_MIN}
VU_SLEW_DB_S = (VU_MAX - VU_MIN) / 0.45   # mechanical limit: full scale in 450 ms (heavier needle: this LCD ghosts fast sweeps)


def vu_step(disp: float, target: float, dt: float) -> float:
    step = (max(VU_MIN, min(VU_MAX, target)) - disp) * min(1.0, dt / 0.08)
    lim = VU_SLEW_DB_S * dt
    return disp + max(-lim, min(lim, step))
_VU_LAST_T = {"t": 0.0}
VU_REG_Y0, VU_REG_Y1 = VU_FACE_Y, VU_FACE_Y + VU_MH   # needle sweep = whole face


F_VOL_S = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 26)
F_FMT_S = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 22)


def render_vu_fb(levels, t_ms: int, dur_ms: int, vol: int, fmt: str = "",
                 label: "str | None" = None) -> bytes:
    """Full-frame BGRX bytes; only the needle patches are redrawn per call."""
    global VU_BASE
    if VU_BASE is None:
        VU_BASE = make_vu_base()
    # rebuild the composed base only when the waveform progress advances
    played = 0
    if dur_ms > 0 and levels is not None:
        played = max(1, int(WAVE_COLS * min(t_ms / dur_ms, 1.0)))
    def _rebuild(pl):
        base = VU_BASE.copy()
        if levels is not None:
            yc2, hm = 437, 38
            base.paste(levels["off"].resize((WAVE_COLS, hm * 2 + 1)), (16, yc2 - hm))
            on = levels["on"].resize((WAVE_COLS, hm * 2 + 1))
            base.paste(on.crop((0, 0, pl, hm * 2 + 1)), (16, yc2 - hm))
        VU_CACHE["img"] = base
        VU_CACHE["played"] = pl
        VU_CACHE["busy"] = False

    if VU_CACHE["img"] is None:
        _rebuild(played)                      # first time: synchronous
    elif abs(played - VU_CACHE["played"]) >= 6 and not VU_CACHE.get("busy"):
        VU_CACHE["busy"] = True
        threading.Thread(target=_rebuild, args=(played,), daemon=True).start()

    # Static frame (base + waveform + texts + label) rebuilt only on change;
    # per frame the working copy just restores the previous needle patches.
    src = VU_CACHE["img"]
    wk = VU_WORK
    if (wk["static"] is None or wk["src"] is not src
            or wk["vol"] != vol or wk["fmt"] != fmt or wk["label"] != label):
        static = src.copy()
        dd = ImageDraw.Draw(static)
        dd.text((VU_TXT_X + VU_TXT_W - 4, VU_TXT_Y + VU_TXT_H - 8), volume_db(vol),
                font=F_VOL_S, fill=(15, 12, 8), anchor="rs")
        if fmt:
            dd.text((VU_FMT_X + 4, VU_TXT_Y + VU_TXT_H - 8), fmt,
                    font=F_FMT_S, fill=(15, 12, 8), anchor="ls")
        if label:
            skin_label_overlay(static, label)
        wk.update(static=static, img=static.copy(), src=src,
                  vol=vol, fmt=fmt, label=label, rects=[])
    frame = wk["img"]
    static = wk["static"]
    for box in wk["rects"]:
        frame.paste(static.crop(box), box)
    wk["rects"] = []

    now = time.monotonic()
    dt = min(0.3, now - _VU_LAST_T["t"]) if _VU_LAST_T["t"] else 0.03
    _VU_LAST_T["t"] = now
    for mx, ch in ((8, "l"), (404, "r")):
        atten = vu_comp_db(vol)
        target = VU_LEVELS[ch] - (VU_REF_DBFS + RT["vu_trim"]) + atten
        VU_DISP[ch] = vu_step(VU_DISP[ch], target, dt)
        a = _vu_angle(VU_DISP[ch])
        cx = VU_MW // 2
        py = VU_PIVOT_Y
        tip_x = cx + VU_R_NEEDLE * math.sin(a)
        tip_y = py - VU_R_NEEDLE * math.cos(a)
        off = VU_REG_Y0 - VU_FACE_Y
        spr = needle_sprite("vu", a, cx, py - off, tip_x, tip_y - off,
                            color=(8, 16, 25), sh_dx=5.5 * math.sin(a), sh_dy=5.0)
        wk["rects"].append(paste_sprite(frame, spr, ox=mx, oy=VU_REG_Y0,
                                        clip=(mx, VU_REG_Y0, mx + VU_MW, VU_REG_Y1)))
    return frame.tobytes("raw", "BGRX")


# ---------------------------------------------------------------- Peppy skins
# PeppyMeter-format circular skins (bgr + rotating needle + optional fgr).
# Skin 0 is the builtin amber VU; the rest come from SKIN_DIR/meters.txt.
SKIN_DIR = Path("/usr/local/share/caldera/skins")
SKIN_LIST: list[str] = ["amber"]
_SKIN_EXCLUDE_DEFAULT = ("grunge, compass, big-bang, ring, royal, vintage, "
                         "tube, gas, vertical-linear, relax, steam-punk, "
                         "fantasy, chillout, orange, gold")
SKIN_EXCLUDE = {s.strip() for s in
                cfg("skins", "exclude", _SKIN_EXCLUDE_DEFAULT).split(",") if s.strip()}
_SKIN_CFG: dict[str, dict] = {}
_SKIN_OBJ: dict[str, "PeppySkin"] = {}
VU_SKIN = {"i": 0, "at": 0.0}
LAST_SKIN = {"n": None}


def load_skin_configs() -> None:
    import configparser
    mt = SKIN_DIR / "meters.txt"
    if not mt.exists():
        return
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(mt)
    except configparser.Error:
        return
    for name in cp.sections():
        s = cp[name]
        if name in SKIN_EXCLUDE or s.get("meter.type", "").strip() not in ("circular", "linear"):
            continue
        if not (SKIN_DIR / s.get("bgr.filename", "")).exists():
            continue
        _SKIN_CFG[name] = dict(s)
        SKIN_LIST.append(name)


class PeppySkin:
    """One circular PeppyMeter skin. Needle image points up, pivot sits
    `distance` px below the image center, rotation is CCW-positive from
    start.angle (volume 0) to stop.angle (volume 100)."""

    def __init__(self, cfg: dict):
        bgr = Image.open(SKIN_DIR / cfg["bgr.filename"]).convert("RGB")
        if bgr.size != (W, H):
            bgr = bgr.resize((W, H))
        self.needle = Image.open(SKIN_DIR / cfg["indicator.filename"]).convert("RGBA")
        self.fgr = None
        fgr_name = cfg.get("fgr.filename", "").strip()
        if fgr_name and (SKIN_DIR / fgr_name).exists():
            fgr = Image.open(SKIN_DIR / fgr_name).convert("RGBA")
            if fgr.size != (W, H):
                fgr = fgr.resize((W, H))
            # fgr is baked into the base; per frame it only needs re-pasting
            # where the needle was drawn on top of it
            self.fgr = fgr
            bgr = Image.alpha_composite(bgr.convert("RGBA"), fgr).convert("RGB")
        self.base = bgr
        self.dist = float(cfg["distance"])
        # angles may be global (start.angle) or per channel (left.start.angle)
        if int(cfg.get("channels", 2)) == 1:
            self.origins = [(float(cfg["mono.origin.x"]), float(cfg["mono.origin.y"]))]
            self.angles = [(float(cfg["start.angle"]), float(cfg["stop.angle"]))]
        else:
            self.origins = [(float(cfg["left.origin.x"]), float(cfg["left.origin.y"])),
                            (float(cfg["right.origin.x"]), float(cfg["right.origin.y"]))]
            self.angles = [
                (float(cfg.get("left.start.angle", cfg.get("start.angle"))),
                 float(cfg.get("left.stop.angle", cfg.get("stop.angle")))),
                (float(cfg.get("right.start.angle", cfg.get("start.angle"))),
                 float(cfg.get("right.stop.angle", cfg.get("stop.angle")))),
            ]
        self.disp = [0.0] * len(self.origins)   # smoothed linear 0..100
        self._last_t = 0.0
        self.frame = None
        self.rects: list = []
        self._label = object()
        self._spr: dict = {}    # quantized angle -> rotated sprite

    def render(self, vol: int, label: "str | None" = None):
        now = time.monotonic()
        dt = min(0.3, now - self._last_t) if self._last_t else 0.03
        self._last_t = now
        atten = vu_comp_db(vol)
        # persistent frame: only the previous needle patches are restored
        if self.frame is None or label != self._label:
            self.frame = self.base.copy()
            if label:
                skin_label_overlay(self.frame, label)
            self._label = label
            self.rects = []
        frame = self.frame
        for box in self.rects:
            frame.paste(self.base.crop(box), box)
        self.rects = []
        for i, (ox, oy) in enumerate(self.origins):
            if len(self.origins) == 1:
                db = max(VU_LEVELS["l"], VU_LEVELS["r"]) + atten
            else:
                db = VU_LEVELS["l" if i == 0 else "r"] + atten
            v = 100.0 * (10.0 ** (min(0.0, db) / 20.0))
            step = (v - self.disp[i]) * min(1.0, dt / 0.08)
            lim = (100.0 / 0.45) * dt           # heavier needle: this LCD ghosts fast sweeps
            self.disp[i] += max(-lim, min(lim, step))
            start, stop = self.angles[i]
            a = start + (stop - start) * self.disp[i] / 100.0
            ab = round(a * 2) / 2.0            # 0.5 degree sprite buckets
            rot = self._spr.get(ab)
            if rot is None:
                rot = self.needle.rotate(ab, resample=Image.BICUBIC, expand=True)
                if len(self._spr) > 48:   # RAM cap per active skin
                    self._spr.clear()
                self._spr[ab] = rot
            ar = math.radians(ab)
            x = int(ox - self.dist * math.sin(ar) - rot.width / 2)
            y = int(oy - self.dist * math.cos(ar) - rot.height / 2)
            frame.paste(rot, (x, y), rot)
            box = (max(0, x), max(0, y),
                   min(W, x + rot.width), min(H, y + rot.height))
            if box[2] > box[0] and box[3] > box[1]:
                if self.fgr is not None:
                    fc = self.fgr.crop(box)
                    frame.paste(fc, box, fc)
                self.rects.append(box)
        return frame.tobytes("raw", "BGRX")


class PeppyLinearSkin:
    """PeppyMeter linear skin: the indicator image is revealed step by step
    (or slid, for indicator.type=single) along the configured direction."""

    def __init__(self, cfg: dict):
        bgr = Image.open(SKIN_DIR / cfg["bgr.filename"]).convert("RGB")
        if bgr.size != (W, H):
            bgr = bgr.resize((W, H))
        self.base = bgr
        ind = Image.open(SKIN_DIR / cfg["indicator.filename"]).convert("RGBA")
        self.single = cfg.get("indicator.type", "").strip() == "single"
        self.dir = cfg.get("direction", "").strip() or "left-right"
        pr = int(cfg.get("position.regular", 1))
        po = int(cfg.get("position.overload", 0) or 0)
        swr = int(cfg.get("step.width.regular", 1))
        swo = int(cfg.get("step.width.overload", 0) or 0)
        self.masks = ([0] + [n * swr for n in range(1, pr + 1)]
                      + [pr * swr + n * swo for n in range(1, po + 1)])
        self.step = 100.0 / (pr + po)

        def _flag(key: str) -> bool:
            return str(cfg.get(key, "")).strip().lower() in ("1", "true", "yes", "on")

        left_ind = ind.transpose(Image.FLIP_LEFT_RIGHT) if _flag("flip.left.x") else ind
        right_ind = ind.transpose(Image.FLIP_LEFT_RIGHT) if _flag("flip.right.x") else ind
        self.ch = [
            (int(cfg["left.x"]), int(cfg["left.y"]), left_ind, True),
            (int(cfg["right.x"]), int(cfg["right.y"]), right_ind, False),
        ]
        # conservative per-channel dirty regions (cover every travel/crop mode)
        span = self.masks[-1] if self.single else 0
        self.regions = []
        for (x, y, ind, _l) in self.ch:
            iw, ih = ind.size
            box = (max(0, x - iw), max(0, min(y - span, y)),
                   min(W, x + iw + span), min(H, y + ih + span))
            self.regions.append(box)
        self.disp = [0.0, 0.0]
        self._last_t = 0.0
        self.frame = None
        self._label = object()

    def render(self, vol: int, label: "str | None" = None):
        now = time.monotonic()
        dt = min(0.3, now - self._last_t) if self._last_t else 0.03
        self._last_t = now
        atten = vu_comp_db(vol)
        if self.frame is None or label != self._label:
            self.frame = self.base.copy()
            if label:
                skin_label_overlay(self.frame, label)
            self._label = label
        frame = self.frame
        for box in self.regions:
            frame.paste(self.base.crop(box), box)

        def put(im, px, py):
            frame.paste(im, (px, py), im)

        for i, (x, y, ind, left) in enumerate(self.ch):
            db = VU_LEVELS["l" if i == 0 else "r"] + atten
            v = 100.0 * (10.0 ** (min(0.0, db) / 20.0))
            step = (v - self.disp[i]) * min(1.0, dt / 0.08)
            lim = (100.0 / 0.45) * dt
            self.disp[i] += max(-lim, min(lim, step))
            n = min(int(self.disp[i] / self.step), len(self.masks) - 1)
            w = max(1, self.masks[n])
            iw, ih = ind.size
            if not self.single:   # single: w is a travel offset, not a crop size
                w = min(w, ih if self.dir in ("bottom-top", "top-bottom") else iw)
            if self.single:
                if self.dir == "bottom-top":
                    put(ind, x, y - w)
                elif self.dir == "top-bottom":
                    put(ind, x, y + w)
                else:
                    put(ind, x + w, y)
            elif self.dir == "bottom-top":
                put(ind.crop((0, ih - w, iw, ih)), x, y + ih - w)
            elif self.dir == "top-bottom":
                put(ind.crop((0, 0, iw, w)), x, y)
            elif self.dir == "right-left":
                put(ind.crop((iw - w, 0, iw, ih)), x + iw - w, y)
            elif self.dir == "edges-center":
                if left:
                    put(ind.crop((0, 0, w, ih)), x, y)
                else:
                    put(ind.crop((iw - w, 0, iw, ih)), x - w, y)
            elif self.dir == "center-edges":
                if left:
                    put(ind.crop((iw - w, 0, iw, ih)), x - w, y)
                else:
                    put(ind.crop((0, 0, w, ih)), x, y)
            else:   # left-right
                put(ind.crop((0, 0, w, ih)), x, y)
        return frame.tobytes("raw", "BGRX")


_SKIN_LABEL = {"txt": None, "img": None}


def skin_label_overlay(img: Image.Image, text: str) -> None:
    """Paste the skin name (shown briefly after a swipe) onto the frame."""
    if _SKIN_LABEL["txt"] != text:
        f = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 28)
        tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        w = int(tmp.textlength(text, font=f)) + 24
        box = Image.new("RGBA", (w, 46), (0, 0, 0, 150))
        ImageDraw.Draw(box).text((12, 23), text, font=f,
                                 fill=(255, 255, 255, 255), anchor="lm")
        _SKIN_LABEL["img"] = box
        _SKIN_LABEL["txt"] = text
    img.paste(_SKIN_LABEL["img"], (16, 16), _SKIN_LABEL["img"])


def get_skin(name: str) -> "PeppySkin | PeppyLinearSkin | None":
    if name not in _SKIN_OBJ:
        try:
            cfg = _SKIN_CFG[name]
            cls = PeppyLinearSkin if cfg.get("meter.type", "").strip() == "linear" else PeppySkin
            _SKIN_OBJ[name] = cls(cfg)
        except Exception:
            _SKIN_OBJ[name] = None
    return _SKIN_OBJ[name]


_NET = {"ip": None, "at": 0.0}


def local_ip() -> str:
    """Cached local IP for the idle screen (refreshed every 10 s)."""
    import socket
    now = time.monotonic()
    if now - _NET["at"] > 10.0:
        _NET["at"] = now
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.settimeout(1.0)
            sk.connect(("8.8.8.8", 80))
            _NET["ip"] = sk.getsockname()[0]
            sk.close()
        except OSError:
            _NET["ip"] = None
    return _NET["ip"] or "NO NETWORK"


_DIAG = {"txt": "", "at": 0.0}


def idle_diag() -> str:
    """One diagnostic line for the idle screen: Wi-Fi + IP + Caldera.
    Born from a lost-profile hunt done completely blind — never again."""
    now = time.monotonic()
    if now - _DIAG["at"] > 5.0:
        _DIAG["at"] = now
        ssid = sig = None
        try:
            import subprocess
            out = subprocess.run(["iw", "dev", "wlan0", "link"],
                                 capture_output=True, text=True, timeout=2).stdout
            for ln in out.splitlines():
                ln = ln.strip()
                if ln.startswith("SSID:"):
                    ssid = ln[5:].strip()
                elif ln.startswith("signal:"):
                    sig = ln.split()[1] + " dBm"
        except Exception:
            pass
        if ssid:
            wifi = f"WiFi: {ssid}" + (f" ({sig})" if sig else "")
        else:
            wifi = "WiFi: NOT CONNECTED"
        cal = "Caldera: online" if now - TL_SHARED["at"] < 6.0 else "Caldera: offline"
        _DIAG["txt"] = f"{wifi}    IP: {local_ip()}    {cal}"
    return _DIAG["txt"]


LOGIN = {"active": False, "code": None, "at": 0.0}


def plex_token_present() -> bool:
    try:
        return bool(json.loads(PREFS.read_text()).get("plex", {}).get("token"))
    except (OSError, ValueError):
        return False


def login_code() -> "str | None":
    """Self-service Plex linking: when Caldera has no token, drive the
    plex.tv/link login and surface the code on screen (no SSH needed).
    The watchdog and the daemon are stopped first: the daemon's stray-kill
    ExecStartPre would murder the login (same binary name)."""
    import re
    import subprocess
    now = time.monotonic()
    if now - LOGIN["at"] < 5.0:
        return LOGIN["code"]
    LOGIN["at"] = now
    caldera = str(Path.home() / "caldera-music/caldera-music")
    if not LOGIN["active"]:
        subprocess.run(["systemctl", "--user", "stop",
                        "caldera-watchdog.timer", "caldera-music"],
                       capture_output=True, timeout=20)
        subprocess.run(["systemctl", "--user", "reset-failed", "caldera-login"],
                       capture_output=True, timeout=10)
        import socket
        subprocess.run(["systemd-run", "--user", "--unit=caldera-login", "bash", "-c",
                        f"{caldera} --login --player-name {socket.gethostname()} "
                        "> /tmp/login.log 2>&1"],
                       capture_output=True, timeout=20)
        LOGIN["active"] = True
        LOGIN["code"] = None
        return None
    r = subprocess.run(["systemctl", "--user", "is-active", "caldera-login"],
                       capture_output=True, text=True, timeout=10)
    if r.stdout.strip() != "active":
        LOGIN["active"] = False     # expired or crashed: relaunch next tick
        return LOGIN["code"]
    try:
        m = re.search(r"Enter code: ([A-Z0-9]+)",
                      Path("/tmp/login.log").read_text(errors="ignore"))
        if m:
            LOGIN["code"] = m.group(1)
    except OSError:
        pass
    return LOGIN["code"]


def login_finished() -> None:
    """Token appeared: put the normal services back."""
    import subprocess
    subprocess.run(["systemctl", "--user", "start",
                    "caldera-music", "caldera-watchdog.timer"],
                   capture_output=True, timeout=20)
    LOGIN["active"] = False


def render_login(code: "str | None") -> Image.Image:
    _load_idle_logo()
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    if _IDLE_LOGO is not None:
        img.paste(_IDLE_LOGO, (W // 2 - 100, 15), _IDLE_LOGO)
    d.text((W // 2, 250), "Link this player to Plex", font=F_TITLE, fill=FG, anchor="mm")
    d.text((W // 2, 300), "plex.tv/link", font=F_FMT, fill=ACCENT, anchor="mm")
    d.text((W // 2, 385), code or "requesting code...",
           font=F_DB if code else F_TEXT, fill=FG, anchor="mm")
    d.text((W // 2, 462), idle_diag(), font=F_SMALL, fill=(110, 110, 115), anchor="mm")
    return img


IDLE_LOGO_PATH = Path("/usr/local/share/caldera/hires_logo.png")
_IDLE_LOGO: Image.Image | None = None


def _load_idle_logo() -> None:
    global _IDLE_LOGO
    if _IDLE_LOGO is None and IDLE_LOGO_PATH.exists():
        try:
            _IDLE_LOGO = Image.open(IDLE_LOGO_PATH).convert("RGBA").resize((200, 200))
        except OSError:
            _IDLE_LOGO = None


def render_idle(vol: int) -> Image.Image:
    _load_idle_logo()
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    if _IDLE_LOGO is not None:
        img.paste(_IDLE_LOGO, (W // 2 - 100, 15), _IDLE_LOGO)
    d.text((W // 2, 255), "RaspiAudiophile", font=F_TITLE, fill=DIM, anchor="mm")
    d.text((W // 2, 298), "D A C", font=F_FMT, fill=DIM, anchor="mm")
    d.text((W // 2, 372), volume_db(vol), font=F_DB, fill=FG, anchor="mm")
    d.text((W // 2, 428), "Waiting for Plexamp Server", font=F_SMALL, fill=DIM, anchor="mm")
    d.text((W // 2, 462), idle_diag(), font=F_SMALL, fill=(110, 110, 115), anchor="mm")
    return img


TL_SHARED = {"tl": None, "at": 0.0}
COMPANION = "http://localhost:32500/player/playback"
_CMD_ID = {"n": 100}
VOL_LOCAL = {"v": None, "at": 0.0}


def companion_cmd(path: str, **params) -> None:
    _CMD_ID["n"] += 1
    params.setdefault("commandID", _CMD_ID["n"])
    try:
        requests.get(f"{COMPANION}/{path}", params=params, timeout=2)
    except requests.RequestException:
        pass


def _wake_screen() -> None:
    if not SCREEN["on"]:
        set_backlight(True)
        SCREEN["wake_at"] = time.monotonic()


def encoder_worker() -> None:
    """KY-040 rotary encoder: volume + transport. Pins from config.

    GPIO 5/6 are NOT free with the clone DAC+ Pro HAT: they gate the
    onboard oscillators - driving them kills the audio clock."""
    try:
        from gpiozero import RotaryEncoder, Button
    except ImportError:
        return
    try:
        enc = RotaryEncoder(cfg("encoder", "gpio_clk", 16),
                            cfg("encoder", "gpio_dt", 26),
                            max_steps=0, wrap=False)
        btn = Button(cfg("encoder", "gpio_sw", 13),
                     pull_up=True, bounce_time=0.03, hold_time=0.8,
                     hold_repeat=True)
    except Exception:
        return

    pending = {"delta": 0, "last_rot": 0.0}
    lock = threading.Lock()

    def cw():
        with lock:
            pending["delta"] += 1
            pending["last_rot"] = time.monotonic()
        _wake_screen()

    def ccw():
        with lock:
            pending["delta"] -= 1
            pending["last_rot"] = time.monotonic()
        _wake_screen()

    enc.when_rotated_clockwise = cw
    enc.when_rotated_counter_clockwise = ccw

    held = {"fired": False}
    click = {"timer": None, "last_release": 0.0, "pressed_at": 0.0}

    def on_press():
        click["pressed_at"] = time.monotonic()

    def do_shutdown():
        # freeze the render loop FIRST or it repaints over the message
        SHUTDOWN["on"] = True
        time.sleep(0.1)
        try:
            img = Image.new("RGB", (W, H), BG)
            ImageDraw.Draw(img).text((W // 2, H // 2), "Shutting down...",
                                     font=F_TITLE, fill=FG, anchor="mm")
            fb_write(img)
        except Exception:
            pass
        # the message stays on for the whole shutdown; the system-shutdown
        # hook (backlight-off.shutdown) blanks the screen at the exact
        # moment the filesystem is read-only: BLACK SCREEN = safe to unplug
        os.system("sudo /sbin/poweroff")

    def on_held():
        # fires every 0.8 s while pressed (hold_repeat)
        held["fired"] = True          # swallow the release either way
        if not SHUTDOWN["on"] and time.monotonic() - click["pressed_at"] >= RT["hold_off"]:
            do_shutdown()             # immediately at 6 s, no release needed

    def single_click():
        if SETTINGS["on"]:
            settings_next()
        else:
            companion_cmd("playPause")

    def on_release():
        if held["fired"]:
            held["fired"] = False
            now = time.monotonic()
            # long-press < 6 s = previous track (decided at RELEASE so a
            # shutdown hold no longer restarts the song on its way)
            if (not SHUTDOWN["on"] and now - pending["last_rot"] >= 0.5
                    and 0.8 <= now - click["pressed_at"] < RT["hold_off"]):
                _wake_screen()
                companion_cmd("skipPrevious")
            return
        now = time.monotonic()
        # ghost-click guards: shaft wobble while rotating, sub-40ms glitches
        if now - pending["last_rot"] < 0.3:
            return
        if now - click["pressed_at"] < 0.04:
            return
        _wake_screen()
        if click["timer"] is not None and now - click["last_release"] < 0.35:
            click["timer"].cancel()
            click["timer"] = None
            companion_cmd("skipNext")
        else:
            click["last_release"] = now
            t = threading.Timer(0.36, single_click)
            click["timer"] = t
            t.start()

    btn.when_pressed = on_press
    btn.when_held = on_held
    btn.when_released = on_release

    # some KY-040 clones emit 2 quadrature cycles per physical detent:
    # divide raw counts, carrying the remainder so slow turns are not lost
    carry = 0
    while True:
        time.sleep(0.2)
        divisor = max(1, int(RT["detent_div"]))
        with lock:
            raw = pending["delta"]
            pending["delta"] = 0
        raw += carry
        d = int(raw / divisor)
        carry = raw - d * divisor
        if d == 0:
            continue
        if SETTINGS["on"]:
            settings_adjust(d)
            continue
        now = time.monotonic()
        tl = TL_SHARED["tl"] or {}
        base = VOL_LOCAL["v"] if (VOL_LOCAL["v"] is not None
                                  and now - VOL_LOCAL["at"] < 3.0) else float(int(tl.get("volume", 50)))
        # one detent = one Caldera volume unit: simple and predictable
        # (the display shows the real dB of wherever you land)
        v = max(0, min(100, int(round(base)) + d))
        VOL_LOCAL["v"] = v
        VOL_LOCAL["at"] = now
        companion_cmd("setParameters", volume=v, type="music")


def timeline_poller() -> None:
    while True:
        fresh = timeline()
        if fresh is not None:
            TL_SHARED["tl"] = fresh
            TL_SHARED["at"] = time.monotonic()
        time.sleep(1.0)


def _warm_caches() -> None:
    """Build the expensive static assets at startup, not on first view
    entry: make_vu_base (3x supersampled faces) alone takes seconds on a
    Pi 3 and used to stall the first switch into the VU view.

    Deliberately does NOT preload the Peppy skins: on a 512 MB Pi 3A+
    holding every skin (base + frame + sprite caches) in RAM caused an
    OOM/swap storm that froze the whole system, audio included."""
    global VU_BASE, FS_FACE
    try:
        if VU_BASE is None:
            VU_BASE = make_vu_base()
        if FS_FACE is None:
            FS_FACE = make_vu_face_small()
    except Exception:
        pass


def trim_skins(active: str) -> None:
    """Drop every skin object except the one on screen: on a 512 MB
    board even the decoded base/fgr/needle images of a dozen skins are
    real money. Revisiting a skin reloads it from disk (~0.3 s)."""
    for n in [k for k in _SKIN_OBJ if k != active]:
        del _SKIN_OBJ[n]


def main() -> None:
    set_backlight(True)   # sync real state: service may restart with screen off
    apply_brightness()
    init_vsync()
    load_skin_configs()
    if RT["def_skin"] in SKIN_LIST:
        VU_SKIN["i"] = SKIN_LIST.index(RT["def_skin"])
    threading.Thread(target=_warm_caches, daemon=True).start()
    threading.Thread(target=touch_listener, daemon=True).start()
    threading.Thread(target=vu_capture, daemon=True).start()
    threading.Thread(target=timeline_poller, daemon=True).start()
    threading.Thread(target=encoder_worker, daemon=True).start()
    last_key = None
    meta: dict = {}
    cover: Image.Image | None = None
    levels = None
    last_frame = b""
    last_vol = -1
    vol_changed_at = 0.0
    paused_since = 0.0
    idle_since = 0.0
    while True:
        if SHUTDOWN["on"]:
            time.sleep(1.0)       # renders frozen: the goodbye screen stays
            continue
        if SETTINGS["on"]:
            if time.monotonic() - SETTINGS["at"] > 60.0:
                settings_save_and_exit()   # forgotten menu: save and leave
            else:
                fb_write(render_settings())
                last_frame = b""
                time.sleep(0.1)
                continue
        now = time.monotonic()
        tl = TL_SHARED["tl"]
        tl_at = TL_SHARED["at"]
        if tl is not None and now - tl_at > 15.0:
            # Companion stopped answering (e.g. the known Caldera freeze):
            # drop the stale timeline instead of interpolating it forever
            tl = None

        state = tl.get("state") if tl else None
        if state == "playing":
            SEEN_PLAYING["yes"] = True
        if state == "paused":
            if paused_since == 0.0:
                paused_since = now
        else:
            paused_since = 0.0
        # a queue resumed as "paused" at boot is stale history, not a session:
        # keep the idle screen (with IP/diagnostics) until real playback
        stale_pause = paused_since and (now - paused_since > PAUSED_TO_IDLE_S
                                        or not SEEN_PLAYING["yes"])

        def live_vol(fallback: int) -> int:
            if VOL_LOCAL["v"] is not None and now - VOL_LOCAL["at"] < 2.0:
                return VOL_LOCAL["v"]      # optimistic: knob just moved
            return fallback

        if tl is None or state in (None, "stopped") or "key" not in tl or stale_pause:
            vol = live_vol(int(tl.get("volume", 0)) if tl else 0)
            if not plex_token_present():
                img = render_login(login_code())
            else:
                if LOGIN["active"]:
                    login_finished()   # token just arrived: resume services
                img = render_idle(vol)
            last_key = None
            if idle_since == 0.0:
                idle_since = now
            elif (SCREEN["on"] and RT["screen_off"] > 0
                  and now - idle_since > RT["screen_off"]
                  and now - SCREEN.get("wake_at", 0.0) > RT["screen_off"]):
                set_backlight(False)
        else:
            idle_since = 0.0
            if not SCREEN["on"]:
                set_backlight(True)       # music came back: wake the screen
            vol = live_vol(int(tl.get("volume", 0)))
            if vol != last_vol:
                if last_vol >= 0:
                    vol_changed_at = now
                last_vol = vol
            key = tl.get("ratingKey")
            if key != last_key:
                try:
                    meta = fetch_meta(tl)
                    cover = fetch_cover(tl, meta["thumb"])
                    levels = fetch_levels(tl, meta.get("stream_id"))
                    last_key = key
                except (requests.RequestException, ET.ParseError):
                    meta, cover, levels = {"title": "?", "artist": "", "album": "",
                                           "thumb": "", "format": ""}, None, None
            t_ms = int(tl.get("time", 0))
            if state == "playing":
                t_ms += int((now - tl_at) * 1000)  # interpolate between polls
            if VIEW["mode"] == 1:
                fb_out(render_fullscreen_fb(cover, vol, last_key, meta.get("format", ""),
                                                    t_ms, int(tl.get("duration", 0)), levels))
                last_frame = b""
                time.sleep(0.04)
                continue
            elif VIEW["mode"] == 2:
                name = SKIN_LIST[VU_SKIN["i"] % len(SKIN_LIST)]
                label = name if now - VU_SKIN["at"] < 1.5 else None
                if name != LAST_SKIN["n"]:
                    trim_skins(name)          # free RAM of inactive skins
                    LAST_SKIN["n"] = name
                skin = get_skin(name) if name != "amber" else None
                if skin is not None:
                    fb_out(skin.render(vol, label))
                else:
                    fb_out(render_vu_fb(levels, t_ms, int(tl.get("duration", 0)), vol,
                                                meta.get("format", ""), label))
                last_frame = b""
                time.sleep(0.028)   # ~20 fps: this LCD needs ~45 ms between needle positions or it ghosts doubles
                continue
            else:
                img = render(state or "?", vol, meta, cover,
                             t_ms, int(tl.get("duration", 0)), levels)

        frame = img.tobytes()
        if frame != last_frame:
            fb_write(img)
            last_frame = frame
        # sleep in slices so a touch that changes the view reacts instantly
        m0 = VIEW["mode"]
        for _ in range(10):
            time.sleep(0.02)
            if VIEW["mode"] != m0:
                break


if __name__ == "__main__":
    main()
