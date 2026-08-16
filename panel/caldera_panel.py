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

import threading

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

def find_fb() -> Path:
    """The TFT's fb index moves between boots; find it by driver name."""
    for p in Path("/sys/class/graphics").glob("fb*"):
        try:
            if (p / "name").read_text().strip() == "fb_ili9481":
                return Path("/dev") / p.name
        except OSError:
            continue
    raise SystemExit("TFT framebuffer (fb_ili9481) not found")


FB = find_fb()
W, H = 480, 320
COVER = 176      # info view, top-left square
COVER_FS = 320   # fullscreen view
TIMELINE_URL = "http://localhost:32500/player/timeline/poll?wait=0&commandID=1"
PREFS = Path.home() / ".config/caldera-music/preferences.json"
POLL_S = 1.0

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
F_TITLE = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 22)
F_TEXT = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans.ttf", 18)
F_SMALL = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans.ttf", 14)
F_DB = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 54)
F_FMT = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 21)

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
    arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    rgb565 = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    FB.write_bytes(rgb565.astype("<u2").tobytes())


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


def server_base(tl: dict) -> str:
    return f"{tl.get('protocol', 'http')}://{tl['address']}:{tl['port']}"


def fetch_meta(tl: dict) -> dict:
    url = f"{server_base(tl)}{tl['key']}"
    r = requests.get(url, params={"X-Plex-Token": token()}, timeout=5)
    r.raise_for_status()
    track = ET.fromstring(r.text).find("Track")
    media = track.find("Media") if track is not None else None
    part = media.find("Part") if media is not None else None
    st = None
    if part is not None:
        st = part.find("Stream")
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
        fmt = " ".join(x for x in (codec, bits, khz) if x)
    return {
        "title": track.get("title", "?") if track is not None else "?",
        "artist": track.get("grandparentTitle", "") if track is not None else "",
        "album": track.get("parentTitle", "") if track is not None else "",
        "thumb": track.get("thumb", "") if track is not None else "",
        "format": fmt,
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


VIEW = {"fullscreen": False}


def touch_listener() -> None:
    try:
        import evdev
    except ImportError:
        return
    dev = None
    for path in evdev.list_devices():
        d = evdev.InputDevice(path)
        if "ADS7846" in d.name:
            dev = d
            break
    if dev is None:
        return
    last = 0.0
    for ev in dev.read_loop():
        if ev.type == evdev.ecodes.EV_KEY and ev.code == evdev.ecodes.BTN_TOUCH and ev.value == 1:
            now = time.monotonic()
            if now - last > 0.4:  # debounce
                VIEW["fullscreen"] = not VIEW["fullscreen"]
                last = now


def render(state: str, vol: int, meta: dict, cover: Image.Image | None,
           t_ms: int, dur_ms: int) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    if cover is not None:
        img.paste(cover.resize((COVER, COVER)), (8, 8))
    else:
        d.rectangle((8, 8, 8 + COVER, 8 + COVER), fill=(30, 30, 34))
        d.text((8 + COVER // 2, 8 + COVER // 2), "♪", font=F_TITLE, fill=DIM, anchor="mm")

    x = COVER + 24
    col_w = W - x - 8

    y = 10
    for line in wrap2(d, meta["title"], F_TITLE, col_w):
        d.text((x, y), line, font=F_TITLE, fill=FG)
        y += 28
    d.text((x, y + 6), ellipsize(d, meta["artist"], F_TEXT, col_w), font=F_TEXT, fill=FG)
    d.text((x, y + 32), ellipsize(d, meta["album"], F_TEXT, col_w), font=F_TEXT, fill=DIM)
    d.text((x, y + 64), meta["format"], font=F_FMT, fill=(120, 200, 255))
    icon = {"playing": "▶", "paused": "⏸"}.get(state, "⏹")
    d.text((x, y + 96), f"{icon} {state}", font=F_TEXT, fill=ACCENT)

    # bottom strip, full width
    if dur_ms > 0:
        yb = 208
        d.rectangle((8, yb, W - 8, yb + 5), fill=(50, 50, 55))
        px = 8 + int((W - 16) * min(t_ms / dur_ms, 1.0))
        d.rectangle((8, yb, px, yb + 5), fill=ACCENT)
        mins = lambda ms: f"{ms // 60000}:{ms % 60000 // 1000:02d}"
        d.text((8, yb + 12), mins(t_ms), font=F_SMALL, fill=DIM)
        d.text((W - 8, yb + 12), mins(dur_ms), font=F_SMALL, fill=DIM, anchor="ra")

    # volume: bar left, big dB right
    yv = 288
    d.rectangle((8, yv, 220, yv + 10), fill=(50, 50, 55))
    d.rectangle((8, yv, 8 + int(212 * vol / 100), yv + 10), fill=ACCENT)
    d.text((W - 8, H - 8), volume_db(vol), font=F_DB, fill=FG, anchor="rs")
    return img


def render_fullscreen(cover: Image.Image | None, vol: int,
                      show_vol: bool) -> Image.Image:
    img = Image.new("RGB", (W, H), (0, 0, 0))
    if cover is not None:
        img.paste(cover.resize((COVER_FS, COVER_FS)), (0, 0))
    d = ImageDraw.Draw(img)
    cx = COVER_FS + (W - COVER_FS) // 2  # center of right column
    num = volume_db(vol)
    if num.endswith(" dB"):
        num = num[:-3]
    d.text((cx, 62), num, font=F_DB, fill=FG, anchor="mm")
    d.text((cx, 114), "dB", font=F_FMT, fill=DIM, anchor="mm")
    # vertical volume bar, fills bottom-up
    bx0, bx1, by0, by1 = cx - 14, cx + 14, 150, 305
    d.rectangle((bx0, by0, bx1, by1), fill=(40, 40, 45))
    top = by1 - int((by1 - by0) * vol / 100)
    d.rectangle((bx0, top, bx1, by1), fill=ACCENT)
    return img


def render_idle(vol: int) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((W // 2, H // 2 - 30), "RaspiAudiophile", font=F_TITLE, fill=DIM, anchor="mm")
    d.text((W // 2, H // 2 + 10), volume_db(vol), font=F_DB, fill=FG, anchor="mm")
    d.text((W // 2, H // 2 + 60), "waiting for Plexamp…", font=F_SMALL, fill=DIM, anchor="mm")
    return img


def main() -> None:
    threading.Thread(target=touch_listener, daemon=True).start()
    last_key = None
    meta: dict = {}
    cover: Image.Image | None = None
    last_frame = b""
    last_vol = -1
    vol_changed_at = 0.0

    while True:
        tl = timeline()
        if tl is None or tl.get("state") in (None, "stopped") or "key" not in tl:
            vol = int(tl.get("volume", 0)) if tl else 0
            img = render_idle(vol)
            last_key = None
        else:
            vol = int(tl.get("volume", 0))
            if vol != last_vol:
                if last_vol >= 0:
                    vol_changed_at = time.monotonic()
                last_vol = vol
            key = tl.get("ratingKey")
            if key != last_key:
                try:
                    meta = fetch_meta(tl)
                    cover = fetch_cover(tl, meta["thumb"])
                    last_key = key
                except (requests.RequestException, ET.ParseError):
                    meta, cover = {"title": "?", "artist": "", "album": "",
                                   "thumb": "", "format": ""}, None
            if VIEW["fullscreen"]:
                show_vol = time.monotonic() - vol_changed_at < 2.5
                img = render_fullscreen(cover, vol, show_vol)
            else:
                img = render(tl.get("state", "?"), vol, meta, cover,
                             int(tl.get("time", 0)), int(tl.get("duration", 0)))

        frame = img.tobytes()
        if frame != last_frame:  # skip identical frames, spare the SPI bus
            fb_write(img)
            last_frame = frame
        time.sleep(0.15 if VIEW["fullscreen"] else POLL_S)


if __name__ == "__main__":
    main()
