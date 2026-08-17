#!/usr/bin/env python3
"""Caldera panel: now-playing display on the MZ61581 TFT (480x320, /dev/fb0).

Data sources:
- Companion timeline on localhost:32500 -> state, volume, ratingKey, server
- Plex server -> track metadata and cover art (token from Caldera's own
  preferences.json, never stored here)

Rendering: Pillow -> RGB565 -> framebuffer. No X server involved.
"""

import io
import json
import math
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import math
import threading

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

FB = Path("/dev/fb0")   # official 7" DSI display, driven by the firmware
W, H = 800, 480
COVER = 340      # info view, top-left square
COVER_FS = 480   # fullscreen view
TIMELINE_URL = "http://localhost:32500/player/timeline/poll?wait=0&commandID=1"
PREFS = Path.home() / ".config/caldera-music/preferences.json"
POLL_S = 1.0
PAUSED_TO_IDLE_S = 600  # after 10 min paused, show the idle screen
SCREEN_OFF_S = 180      # idle this long -> backlight off; touch/play wakes
BL_POWER = Path("/sys/class/backlight/rpi_backlight/bl_power")
SCREEN = {"on": True}


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


DB_PER_STEP = 0.5  # hi-fi attenuator scale: vol 100 = 0 dB, vol 0 = -50 dB
# TODO: calibrate against Caldera's real curve (UCA202 ADC loopback measure)


def volume_db(vol: int) -> str:
    if vol <= 0:
        return "MUTE"
    return f"{-DB_PER_STEP * (100 - vol):.1f} dB"


def fb_write(img: Image.Image) -> None:
    # firmware fb is 32bpp XRGB little-endian: byte order B,G,R,X
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    out = np.empty((H, W, 4), dtype=np.uint8)
    out[..., 0] = arr[..., 2]
    out[..., 1] = arr[..., 1]
    out[..., 2] = arr[..., 0]
    out[..., 3] = 255
    FB.write_bytes(out.tobytes())


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


FALLBACK_SERVER = "http://192.168.1.250:32400"


def server_base(tl: dict) -> str:
    # prefer the LAN server: timeline sometimes advertises the remote
    # plex.direct route, unreachable from inside the network (hairpin NAT)
    return FALLBACK_SERVER


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
        "thumb": track.get("thumb", "") if track is not None else "",
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
    for ev in dev.read_loop():
        if ev.type == evdev.ecodes.EV_KEY and ev.code == evdev.ecodes.BTN_TOUCH and ev.value == 1:
            now = time.monotonic()
            if now - last > 0.4:  # debounce
                if not SCREEN["on"]:
                    set_backlight(True)   # wake only, keep the current view
                    SCREEN["wake_at"] = now
                else:
                    VIEW["mode"] = (VIEW["mode"] + 1) % 3
                last = now


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
VU_REF_DBFS = -8.0   # 0 VU reference (calibrated by ear on quiet tracks)
VU_LEVELS = {"l": -60.0, "r": -60.0}


def vu_capture() -> None:
    try:
        import alsaaudio
    except ImportError:
        return
    while True:
        try:
            pcm = alsaaudio.PCM(alsaaudio.PCM_CAPTURE, alsaaudio.PCM_NORMAL,
                                device="plughw:CARD=Loopback,DEV=1",
                                channels=2, rate=48000,
                                format=alsaaudio.PCM_FORMAT_S16_LE,
                                periodsize=1200)
            while True:
                n, data = pcm.read()
                if n <= 0:
                    time.sleep(0.02)
                    continue
                a = np.frombuffer(data, dtype=np.int16).reshape(-1, 2).astype(np.float64)
                blk = a.shape[0] / 48000.0
                alpha = min(1.0, blk / 0.065)  # VU: 99% in 300 ms -> tau 65 ms
                for i, ch in enumerate(("l", "r")):
                    rms = np.sqrt(np.mean(a[:, i] ** 2))
                    db = 20 * math.log10(max(rms, 1.0) / 32768.0)
                    VU_LEVELS[ch] += (db - VU_LEVELS[ch]) * alpha
        except Exception:
            VU_LEVELS["l"] = VU_LEVELS["r"] = -60.0
            time.sleep(2)


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


def draw_needle_aa(arr_bgr, cx, py, tip_x, tip_y, w0=2.8, w1=0.35,
                   color=(12, 20, 28), shadow=True, sh_dx=5.0, sh_dy=6.0):
    """Anti-aliased tapered needle with a soft offset lamp shadow (BGR array)."""
    h, w = arr_bgr.shape[:2]
    x0 = max(0, int(min(cx, tip_x)) - 12); x1 = min(w, int(max(cx, tip_x)) + 13)
    y0 = max(0, int(min(py, tip_y)) - 12); y1 = min(h, int(max(py, tip_y)) + 14)
    if x1 <= x0 or y1 <= y0:
        return
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    dx, dy = tip_x - cx, tip_y - py
    L2 = float(dx * dx + dy * dy) or 1.0
    t = np.clip(((xx - cx) * dx + (yy - py) * dy) / L2, 0.0, 1.0)
    px = cx + t * dx
    pyl = py + t * dy
    width = w0 + (w1 - w0) * t
    sub = arr_bgr[y0:y1, x0:x1, :3].astype(np.float32)
    if shadow:
        # distance from the pixel to the SHIFTED needle line (true cast shadow)
        xs, ys = xx - sh_dx, yy - sh_dy
        t2 = np.clip(((xs - cx) * dx + (ys - py) * dy) / L2, 0.0, 1.0)
        dist_sh = np.hypot(xs - (cx + t2 * dx), ys - (py + t2 * dy))
        w_sh = w0 + (w1 - w0) * t2
        a_sh = np.clip((w_sh + 3.0 - dist_sh) / 3.0, 0.0, 1.0)[..., None] * 0.45
        sub = sub * (1.0 - a_sh * 0.40)
    dist = np.hypot(xx - px, yy - pyl)
    a = np.clip((width + 1.1 - dist) / 1.1, 0.0, 1.0)[..., None]
    sub = sub * (1 - a) + np.array(color, np.float32) * a
    arr_bgr[y0:y1, x0:x1, :3] = sub.astype(np.uint8)

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
        d.text((VU_MW * SS - 22 * SS, 22 * SS), name[0], font=f_ch, fill=(120, 90, 50), anchor="mm")
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
FS_CACHE: dict = {"arr": None, "key": None}
FS_DISP = {"m": VU_MIN}


def render_fullscreen_fb(cover: Image.Image | None, vol: int, key) -> bytes:
    global FS_FACE
    if FS_FACE is None:
        FS_FACE = make_vu_face_small()
    ck = (key, vol)
    if FS_CACHE["arr"] is None or FS_CACHE["key"] != ck:
        base = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(base)
        if cover is not None:
            base.paste(cover.resize((COVER_FS, COVER_FS)), (0, 0))
            d.rectangle((0, 0, COVER_FS - 1, COVER_FS - 1), outline=(210, 210, 215), width=1)
        base.paste(FS_FACE, (FS_FACE_X, FS_FACE_Y))
        d.rectangle((FS_FACE_X - 1, FS_FACE_Y - 1, FS_FACE_X + FS_FACE_W,
                     FS_FACE_Y + FS_FACE_H), outline=(5, 5, 5), width=4)
        ccx = FS_FACE_X + FS_FACE_W // 2
        num = volume_db(vol)
        if num.endswith(" dB"):
            main, unit = num[:-3], " dB"
        else:
            main, unit = num, ""
        f_num = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 68)
        wm = d.textlength(main, font=f_num)
        wu = d.textlength(unit, font=F_FMT) if unit else 0
        x0 = ccx - (wm + wu) / 2
        d.text((x0, 428), main, font=f_num, fill=FG, anchor="ls")
        if unit:
            d.text((x0 + wm, 428), unit, font=F_FMT, fill=FG, anchor="ls")
        FS_CACHE["arr"] = _to_xrgb(base)
        FS_CACHE["key"] = ck
    arr = FS_CACHE["arr"].copy()

    now = time.monotonic()
    dt = min(0.3, now - _VU_LAST_T["t"]) if _VU_LAST_T["t"] else 0.03
    atten = DB_PER_STEP * (100 - vol) if vol > 0 else 60.0
    target = max(VU_LEVELS["l"], VU_LEVELS["r"]) - VU_REF_DBFS + atten
    FS_DISP["m"] = vu_step(FS_DISP["m"], target, dt)
    a = _vu_angle(FS_DISP["m"])
    cx, py = FS_FACE_W // 2, FS_PIVOT_Y
    tip_x = cx + FS_R_NEEDLE * math.sin(a)
    tip_y = py - FS_R_NEEDLE * math.cos(a)
    view = arr[FS_FACE_Y:FS_FACE_Y + FS_FACE_H, FS_FACE_X:FS_FACE_X + FS_FACE_W]
    draw_needle_aa(view, cx, py, tip_x, tip_y, w0=2.2, w1=0.3,
                   color=(12, 20, 28), sh_dx=4.5 * math.sin(a), sh_dy=4.5)
    return arr.tobytes()


VU_BASE: Image.Image | None = None
VU_CACHE: dict = {"arr": None, "played": -1}
VU_VOLTXT = {"vol": None, "arr": None}
VU_FMTTXT = {"fmt": None, "arr": None}
VU_FMT_X = 8 + 10
VU_TXT_W, VU_TXT_H = 170, 46
VU_TXT_X = 404 + VU_MW - VU_TXT_W - 10
VU_TXT_Y = VU_FACE_Y + VU_MH - VU_TXT_H - 8
VU_DISP = {"l": VU_MIN, "r": VU_MIN}
VU_SLEW_DB_S = (VU_MAX - VU_MIN) / 0.30   # mechanical limit: full scale in 300 ms


def vu_step(disp: float, target: float, dt: float) -> float:
    step = (max(VU_MIN, min(VU_MAX, target)) - disp) * min(1.0, dt / 0.05)
    lim = VU_SLEW_DB_S * dt
    return disp + max(-lim, min(lim, step))
_VU_LAST_T = {"t": 0.0}
VU_REG_Y0, VU_REG_Y1 = VU_FACE_Y, VU_FACE_Y + VU_MH   # needle sweep = whole face


def _to_xrgb(img: Image.Image) -> np.ndarray:
    a = np.asarray(img.convert("RGB"), dtype=np.uint8)
    out = np.empty((a.shape[0], a.shape[1], 4), dtype=np.uint8)
    out[..., 0] = a[..., 2]
    out[..., 1] = a[..., 1]
    out[..., 2] = a[..., 0]
    out[..., 3] = 255
    return out


def render_vu_fb(levels, t_ms: int, dur_ms: int, vol: int, fmt: str = "") -> bytes:
    """Full-frame XRGB bytes; only the needle regions are redrawn per call."""
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
        VU_CACHE["arr"] = _to_xrgb(base)
        VU_CACHE["played"] = pl
        VU_CACHE["busy"] = False

    if VU_CACHE["arr"] is None:
        _rebuild(played)                      # first time: synchronous
    elif abs(played - VU_CACHE["played"]) >= 6 and not VU_CACHE.get("busy"):
        VU_CACHE["busy"] = True
        threading.Thread(target=_rebuild, args=(played,), daemon=True).start()
    arr = VU_CACHE["arr"].copy()

    if VU_VOLTXT["vol"] != vol:
        crop = VU_BASE.crop((VU_TXT_X, VU_TXT_Y,
                             VU_TXT_X + VU_TXT_W, VU_TXT_Y + VU_TXT_H)).copy()
        dd = ImageDraw.Draw(crop)
        dd.text((VU_TXT_W - 4, VU_TXT_H - 6), volume_db(vol),
                font=F_FMT, fill=(15, 12, 8), anchor="rs")
        VU_VOLTXT["arr"] = _to_xrgb(crop)
        VU_VOLTXT["vol"] = vol
    arr[VU_TXT_Y:VU_TXT_Y + VU_TXT_H, VU_TXT_X:VU_TXT_X + VU_TXT_W] = VU_VOLTXT["arr"]

    if fmt and VU_FMTTXT["fmt"] != fmt:
        crop = VU_BASE.crop((VU_FMT_X, VU_TXT_Y,
                             VU_FMT_X + VU_TXT_W + 40, VU_TXT_Y + VU_TXT_H)).copy()
        dd = ImageDraw.Draw(crop)
        short = fmt.replace(" / 24-bit", "").replace(" / 16-bit", "")
        dd.text((4, VU_TXT_H - 6), short, font=F_FMT, fill=(15, 12, 8), anchor="ls")
        VU_FMTTXT["arr"] = _to_xrgb(crop)
        VU_FMTTXT["fmt"] = fmt
    if VU_FMTTXT["arr"] is not None:
        arr[VU_TXT_Y:VU_TXT_Y + VU_TXT_H, VU_FMT_X:VU_FMT_X + VU_TXT_W + 40] = VU_FMTTXT["arr"]

    now = time.monotonic()
    dt = min(0.3, now - _VU_LAST_T["t"]) if _VU_LAST_T["t"] else 0.03
    _VU_LAST_T["t"] = now
    for mx, ch in ((8, "l"), (404, "r")):
        atten = DB_PER_STEP * (100 - vol) if vol > 0 else 60.0
        target = VU_LEVELS[ch] - VU_REF_DBFS + atten
        VU_DISP[ch] = vu_step(VU_DISP[ch], target, dt)
        a = _vu_angle(VU_DISP[ch])
        cx = VU_MW // 2
        py = VU_PIVOT_Y
        tip_x = cx + VU_R_NEEDLE * math.sin(a)
        tip_y = py - VU_R_NEEDLE * math.cos(a)
        view = arr[VU_REG_Y0:VU_REG_Y1, mx:mx + VU_MW]
        off = VU_REG_Y0 - VU_FACE_Y
        draw_needle_aa(view, cx, py - off, tip_x, tip_y - off, color=(8, 16, 25),
                       sh_dx=5.5 * math.sin(a), sh_dy=5.0)
    return arr.tobytes()


def render_idle(vol: int) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((W // 2, 150), "RaspiAudiophile", font=F_TITLE, fill=DIM, anchor="mm")
    d.text((W // 2, 255), volume_db(vol), font=F_DB, fill=FG, anchor="mm")
    d.text((W // 2, 355), "waiting for Plexamp…", font=F_SMALL, fill=DIM, anchor="mm")
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
    """KY-040 on GPIO 16 (CLK) / 26 (DT) / 13 (SW): volume + transport.\n\n    GPIO 5/6 are NOT free with the clone DAC+ Pro HAT: they gate the\n    onboard oscillators - driving them kills the audio clock."""
    try:
        from gpiozero import RotaryEncoder, Button
    except ImportError:
        return
    try:
        enc = RotaryEncoder(16, 26, max_steps=0, wrap=False)
        btn = Button(13, pull_up=True, bounce_time=0.03, hold_time=0.8)
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

    def on_held():
        held["fired"] = True          # swallow the release either way
        if time.monotonic() - pending["last_rot"] < 0.5:
            return                    # ghost hold while rotating
        _wake_screen()
        companion_cmd("skipPrevious")

    def single_click():
        companion_cmd("playPause")

    def on_release():
        if held["fired"]:
            held["fired"] = False
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

    while True:
        time.sleep(0.2)
        with lock:
            d = pending["delta"]
            pending["delta"] = 0
        if d == 0:
            continue
        now = time.monotonic()
        tl = TL_SHARED["tl"] or {}
        base = VOL_LOCAL["v"] if (VOL_LOCAL["v"] is not None
                                  and now - VOL_LOCAL["at"] < 3.0) else int(tl.get("volume", 50))
        v = max(0, min(100, base + d))
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


def main() -> None:
    set_backlight(True)   # sync real state: service may restart with screen off
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
        now = time.monotonic()
        tl = TL_SHARED["tl"]
        tl_at = TL_SHARED["at"]

        state = tl.get("state") if tl else None
        if state == "paused":
            if paused_since == 0.0:
                paused_since = now
        else:
            paused_since = 0.0
        stale_pause = paused_since and now - paused_since > PAUSED_TO_IDLE_S

        def live_vol(fallback: int) -> int:
            if VOL_LOCAL["v"] is not None and now - VOL_LOCAL["at"] < 2.0:
                return VOL_LOCAL["v"]      # optimistic: knob just moved
            return fallback

        if tl is None or state in (None, "stopped") or "key" not in tl or stale_pause:
            vol = live_vol(int(tl.get("volume", 0)) if tl else 0)
            img = render_idle(vol)
            last_key = None
            if idle_since == 0.0:
                idle_since = now
            elif (SCREEN["on"] and now - idle_since > SCREEN_OFF_S
                  and now - SCREEN.get("wake_at", 0.0) > SCREEN_OFF_S):
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
                FB.write_bytes(render_fullscreen_fb(cover, vol, last_key))
                last_frame = b""
                time.sleep(0.04)
                continue
            elif VIEW["mode"] == 2:
                FB.write_bytes(render_vu_fb(levels, t_ms, int(tl.get("duration", 0)), vol, meta.get("format", "")))
                last_frame = b""
                time.sleep(0.025)
                continue
            else:
                img = render(state or "?", vol, meta, cover,
                             t_ms, int(tl.get("duration", 0)), levels)

        frame = img.tobytes()
        if frame != last_frame:
            fb_write(img)
            last_frame = frame
        time.sleep(0.2)


if __name__ == "__main__":
    main()
