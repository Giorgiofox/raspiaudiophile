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
COVER = 320  # left square
TIMELINE_URL = "http://localhost:32500/player/timeline/poll?wait=0&commandID=1"
PREFS = Path.home() / ".config/caldera-music/preferences.json"
POLL_S = 1.0

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
F_TITLE = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 22)
F_TEXT = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans.ttf", 18)
F_SMALL = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans.ttf", 14)
F_DB = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 54)

BG = (12, 12, 14)
FG = (235, 235, 235)
DIM = (150, 150, 150)
ACCENT = (240, 180, 60)


def token() -> str:
    return json.loads(PREFS.read_text())["plex"]["token"]


def volume_db(vol: int) -> str:
    if vol <= 0:
        return "MUTE"
    return f"{20 * math.log10(vol / 100):.1f} dB"


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
        "width": COVER,
        "height": COVER,
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


def render(state: str, vol: int, meta: dict, cover: Image.Image | None,
           t_ms: int, dur_ms: int) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    if cover is not None:
        img.paste(cover, (0, 0))
    else:
        d.rectangle((0, 0, COVER - 1, H - 1), fill=(30, 30, 34))
        d.text((COVER // 2, H // 2), "♪", font=F_DB, fill=DIM, anchor="mm")

    x = COVER + 12
    col_w = W - x - 10

    d.text((x, 16), ellipsize(d, meta["title"], F_TITLE, col_w), font=F_TITLE, fill=FG)
    d.text((x, 52), ellipsize(d, meta["artist"], F_TEXT, col_w), font=F_TEXT, fill=FG)
    d.text((x, 78), ellipsize(d, meta["album"], F_TEXT, col_w), font=F_TEXT, fill=DIM)
    d.text((x, 110), meta["format"], font=F_SMALL, fill=DIM)

    icon = {"playing": "▶", "paused": "⏸"}.get(state, "⏹")
    d.text((x, 140), f"{icon} {state}", font=F_TEXT, fill=ACCENT)

    # progress
    if dur_ms > 0:
        y = 180
        d.rectangle((x, y, x + col_w, y + 4), fill=(50, 50, 55))
        px = x + int(col_w * min(t_ms / dur_ms, 1.0))
        d.rectangle((x, y, px, y + 4), fill=ACCENT)
        mins = lambda ms: f"{ms // 60000}:{ms % 60000 // 1000:02d}"
        d.text((x, y + 10), mins(t_ms), font=F_SMALL, fill=DIM)
        d.text((x + col_w, y + 10), mins(dur_ms), font=F_SMALL, fill=DIM, anchor="ra")

    # volume, dB primary
    d.text((x, 222), volume_db(vol), font=F_DB, fill=FG)
    y = 292
    d.rectangle((x, y, x + col_w, y + 8), fill=(50, 50, 55))
    d.rectangle((x, y, x + int(col_w * vol / 100), y + 8), fill=ACCENT)
    return img


def render_idle(vol: int) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((W // 2, H // 2 - 30), "RaspiAudiophile", font=F_TITLE, fill=DIM, anchor="mm")
    d.text((W // 2, H // 2 + 10), volume_db(vol), font=F_DB, fill=FG, anchor="mm")
    d.text((W // 2, H // 2 + 60), "waiting for Plexamp…", font=F_SMALL, fill=DIM, anchor="mm")
    return img


def main() -> None:
    last_key = None
    meta: dict = {}
    cover: Image.Image | None = None
    last_frame = b""

    while True:
        tl = timeline()
        if tl is None or tl.get("state") in (None, "stopped") or "key" not in tl:
            vol = int(tl.get("volume", 0)) if tl else 0
            img = render_idle(vol)
            last_key = None
        else:
            key = tl.get("ratingKey")
            if key != last_key:
                try:
                    meta = fetch_meta(tl)
                    cover = fetch_cover(tl, meta["thumb"])
                    last_key = key
                except (requests.RequestException, ET.ParseError):
                    meta, cover = {"title": "?", "artist": "", "album": "",
                                   "thumb": "", "format": ""}, None
            img = render(tl.get("state", "?"), int(tl.get("volume", 0)),
                         meta, cover, int(tl.get("time", 0)),
                         int(tl.get("duration", 0)))

        frame = img.tobytes()
        if frame != last_frame:  # skip identical frames, spare the SPI bus
            fb_write(img)
            last_frame = frame
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
