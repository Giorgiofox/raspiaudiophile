#!/usr/bin/env python3
"""Generate the boot splash shown by caldera-splash.service (7\" DSI, 800x480 XRGB).

Run on the Pi, then: sudo cp /tmp/splash.raw /usr/local/share/caldera/splash.raw
Output is raw RGB565 480x320, written straight to the TFT framebuffer at boot.
"""

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 800, 480
FONT_DIR = "/usr/share/fonts/truetype/dejavu"

img = Image.new("RGB", (W, H), (12, 12, 14))
d = ImageDraw.Draw(img)
f_title = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 36)
f_sub = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 22)
f_small = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans.ttf", 14)

d.text((W // 2, H // 2 - 34), "RaspiAudiophile", font=f_title, fill=(235, 235, 235), anchor="mm")
d.text((W // 2, H // 2 + 8), "D A C", font=f_sub, fill=(150, 150, 150), anchor="mm")
d.rectangle((W // 2 - 60, H // 2 + 34, W // 2 + 60, H // 2 + 36), fill=(240, 180, 60))
d.text((W // 2, H - 30), "loading…", font=f_small, fill=(110, 110, 115), anchor="mm")

arr = np.asarray(img, dtype=np.uint8)
out = np.empty((H, W, 4), dtype=np.uint8)
out[..., 0] = arr[..., 2]
out[..., 1] = arr[..., 1]
out[..., 2] = arr[..., 0]
out[..., 3] = 255
raw = out.tobytes()
with open("/tmp/splash.raw", "wb") as f:
    f.write(raw)
print(f"wrote /tmp/splash.raw ({len(raw)} bytes)")
