#!/usr/bin/env python3
"""Generate the boot splash (7" DSI, 800x480 XRGB): Hi-Res badge + name."""

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 800, 480
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
LOGO = "/usr/local/share/caldera/hires_logo.png"

img = Image.new("RGB", (W, H), (12, 12, 14))
d = ImageDraw.Draw(img)
f_title = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 40)
f_sub = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 24)
f_small = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans.ttf", 15)

try:
    logo = Image.open(LOGO).convert("RGBA").resize((170, 170))
    img.paste(logo, (W // 2 - 85, 40), logo)
except OSError:
    pass

d.text((W // 2, 250), "RaspiAudiophile", font=f_title, fill=(235, 235, 235), anchor="mm")
d.text((W // 2, 300), "D A C", font=f_sub, fill=(150, 150, 150), anchor="mm")
d.rectangle((W // 2 - 70, 330, W // 2 + 70, 333), fill=(240, 180, 60))
d.text((W // 2, 440), "loading…", font=f_small, fill=(110, 110, 115), anchor="mm")

arr = np.asarray(img, dtype=np.uint8)
out = np.empty((H, W, 4), dtype=np.uint8)
out[..., 0] = arr[..., 2]
out[..., 1] = arr[..., 1]
out[..., 2] = arr[..., 0]
out[..., 3] = 255
with open("/tmp/splash.raw", "wb") as f:
    f.write(out.tobytes())
print(f"wrote /tmp/splash.raw")
