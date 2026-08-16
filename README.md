# Caldera HiFi

Headless Plex music player on a Raspberry Pi 3, feeding an amplifier through an
I2S DAC HAT. Powered by [Caldera Music headless](https://caldera.homes/music/headless/).

## Current state (2026-08-15)

Deployed and registered with plex.tv as player "RaspiAudiophile".

- Host: `RaspiAudiophile`, user `giorgiofox`, IP `192.168.1.187` (Wi-Fi, DHCP —
  add a router reservation). SSH alias: `ssh caldera`
- Caldera Music 1.1.0-beta.1 (beta channel), user service enabled, linger on,
  Companion watchdog timer active
- Interim DAC: Behringer UCA202 (USB, 16-bit/48 kHz max) on
  `hw:CARD=CODEC,DEV=0`, `audio.sampleRate=48000` (Caldera resamples 96 -> 48)
- Onboard and HDMI audio disabled in `config.txt`
- Pending: Leikurvo PCM5122 HAT ordered — see "Switching to the HAT" below

## Switching to the HAT (when it arrives)

1. Power off, mount the HAT on the 40-pin header, power on
2. In `/boot/firmware/config.txt`: uncomment `dtoverlay=hifiberry-dacplus`
   (line already present), reboot
3. `aplay -l` should show `sndrpihifiberry`
4. Point Caldera at it, bit-perfect (no resampling):

```sh
~/caldera-music/caldera-music --set audio.outputDeviceUid=hw:CARD=sndrpihifiberry,DEV=0 \
                              --set audio.sampleRate=0
systemctl --user restart caldera-music
```

## Hardware

| Part | Detail |
|---|---|
| Board | Raspberry Pi 3B / 3B+ |
| DAC | Leikurvo HiFi DAC HAT, PCM5122, dual oscillator, RCA out |
| Storage | microSD 16 GB (music is streamed from Plex, card holds only OS + cache) |
| Network | Wi-Fi 5 GHz (3B+, -25 dBm, 433 Mbps link — final choice; FLAC 24/96 is ~5 Mbps). Rear-panel RJ45 still in the case design as fallback |
| Output | HAT RCA -> amplifier line input. Ignore the 3.5 mm jack on the HAT and on the Pi |

The HAT sits on the full 40-pin GPIO header. Audio travels over I2S, so the
USB/Ethernet shared bus of the Pi 3 is not involved in playback.

## 1. Flash the SD card (Raspberry Pi Imager)

- Device: Raspberry Pi 3
- OS: Raspberry Pi OS Lite (64-bit) — under "Raspberry Pi OS (other)"
- Storage: the microSD

In the OS customization dialog (gear icon / "Edit settings"):

- Hostname: `caldera`
- Username: `caldera`, password: your choice
- Wi-Fi: only if Ethernet is not available
- Locale: `Europe/Rome`, keyboard `it`
- Services tab: enable SSH (password or public key)

Write the image. Keep the card in the Mac afterwards: the boot partition
(`/Volumes/bootfs`) must be edited before first boot (next step).

## 2. Enable the DAC HAT

Edit `/Volumes/bootfs/config.txt` while the card is still mounted:

- Comment out `dtparam=audio=on` (onboard audio off)
- Add under `[all]`:

```ini
dtoverlay=hifiberry-dacplus
```

The Leikurvo board is a HiFiBerry DAC+ Pro clone (PCM5122 with dual
oscillators); the in-kernel `hifiberry-dacplus` overlay drives it, including
hardware volume. Eject the card, mount the HAT on the Pi, insert the card,
connect Ethernet and power.

## 3. First boot check

```sh
ssh caldera@caldera.local
aplay -l          # expect: card 0: sndrpihifiberry [snd_rpi_hifiberry_dacplus]
```

If `aplay -l` shows the HiFiBerry card, the HAT is working.

Optional quick test (white noise on the amplifier, keep volume low):

```sh
speaker-test -D hw:0 -c 2 -t wav -l 1
```

## 4. Install Caldera Music

```sh
curl -sSL https://releases.caldera.homes/music/headless/install.sh | bash
~/caldera-music/caldera-music --login      # interactive Plex login
loginctl enable-linger $USER               # user services survive logout
systemctl --user enable --now caldera-music
```

The installer auto-detects the architecture (aarch64 on 64-bit OS). Caldera
self-updates in the background afterwards; no package manager involved.

Currently on the beta channel (1.1.0-beta.1, switched 2026-08-15 hoping for a
fix to the event-loop freeze below). To change channel:

```sh
systemctl --user stop caldera-watchdog.timer   # or it aborts the upgrade
curl -sSL https://releases.caldera.homes/music/headless/upgrade.sh | bash -s -- --beta   # or --stable
systemctl --user start caldera-watchdog.timer
```

If the service sticks in "deactivating" (the 1.0.47 daemon can hang on stop),
clear it with `pkill -9 -x caldera-music && systemctl --user restart
caldera-music`. Never `pkill -f` a pattern that appears in your own ssh
command line — it kills the remote shell.

## 5. Use it

The Pi shows up as a Plex player named after the host. Control playback from
Plexamp, Plex iOS/Android, or the Plex web app: pick the `caldera` player in
the cast menu and play anything from the library.

Format support: FLAC, ALAC, MP3, AAC, Ogg, Opus, WAV, AIFF up to 24-bit/384
kHz, streamed bit-perfect to the DAC. A 24/96 library plays at native
resolution.

Features worth exploring:

- SweetFades: adaptive crossfade with volume leveling
- 10-band parametric EQ per instance (per-room tuning)
- Multi-room sync: a second Pi + HAT running Caldera joins automatically and
  plays in lock-step

## Amplifier and power (planned integration)

Amp: Sure Electronics AA-AB32361, class D, TDA7498E, 2x160 W @ 4 ohm.
Inputs: RCA. Outputs: screw terminals (OUT1/OUT2). Supply: 15-36 V DC via
barrel jack or VCC/GND screw terminals. Has STBY/MUTE header and fan header.

Everything goes in a metal case with 3D-printed PETG front/rear panels (PETG
acts as the RF window for the Pi's Wi-Fi antenna). Wi-Fi is the final network
choice: signal is excellent and the box is always on, so the ~10 s of boot
time Ethernet would save is irrelevant. Constraint for the CAD: the Pi's
antenna corner (by the SD slot) must face a PETG panel up close. A rear-panel
RJ45 pass-through stays in the design as a cheap fallback.

Power scheme (external PSU, no mains inside the case):

- Mean Well 24 V DC brick (purchased) -> rear-panel DC jack -> amp VCC
- Internal DC-DC buck 24->5 V -> Pi, fed via GPIO pins 2/4 (5 V) + 6 (GND).
  Tune the buck to 5.1 V measured under load BEFORE connecting; the GPIO feed
  bypasses the Pi's input fuse and reverse-polarity protection
- EMI filter boards (0-50 V 4 A LC): one between buck and Pi, one between
  brick and amp
- Metal chassis tied to DC ground at a single point near the DC inlet (scrape
  paint/anodizing under the lug). One point only — no other chassis-ground
  connections, RCA grounds stay as they are
- At 24 V the TDA7498E delivers ~2x80 W @ 4 ohm

History: the original phone-charger PSU caused live under-voltage
(get_throttled 0xd0005, kernel "Undervoltage detected!") producing
intermittent crackle through the USB DAC; audio.audioBufferMs raised 50->100
as mitigation. Verify get_throttled stays 0x0 after the power rework.

Planned: wire amp STBY/MUTE header to Pi GPIOs for pop-free startup/shutdown
and auto-standby when Caldera is idle.

## Control panel (planned)

- v1: EC11 rotary encoder + Tontec MZ61581-PI-EXT 3.5" TFT (480x320 color,
  SPI up to 128 MHz, in-kernel fbtft overlay `mz61581`). Both on hand; the
  SSD1306 OLED idea was dropped in its favor — the TFT can show cover art.
  Electrically independent from the DAC HAT (SPI vs I2S/I2C); physically both
  want the 40-pin header, so the TFT is wired with jumper leads from a
  stacking header to the front panel instead of plugged on top.
  A single Python service ("caldera-panel": Pillow rendering to the fbtft
  framebuffer + lgpio for the encoder) translates encoder events to Caldera
  Companion HTTP calls on localhost:32500 and shows cover art (320x320 via
  Plex /photo/:/transcode), title/artist, play state and a volume overlay
  with dB readout. Gestures: rotate = volume, click = play/pause, double
  click = next track, long press = previous track.

  v1 pin map (physical pin numbers):

  | Signal | GPIO | Pin | Notes |
  |---|---|---|---|
  | TFT 5V | - | 2 | backlight ~100 mA |
  | TFT GND | - | 6 | |
  | TFT MOSI | 10 | 19 | SPI0 |
  | TFT SCLK | 11 | 23 | SPI0 |
  | TFT CE0 | 8 | 24 | SPI0 |
  | TFT DC / RESET | 25 / 15 | 22 / 10 | check overlay defaults on first test |
  | Encoder A | 5 | 29 | internal pull-up |
  | Encoder B | 6 | 31 | internal pull-up |
  | Encoder SW | 13 | 33 | internal pull-up |
  | Encoder common + SW return | - | 30, 34 | GND |

  Status: TFT running since 2026-08-16 (plugged directly on the header until
  the HAT arrives). Key facts learned:
  - The kernel dropped fb_mz61581; the stock `mz61581` overlay silently binds
    fb_s6d02a1 (wrong init, black screen). Custom `caldera-tft.dtbo` (source
    in pi/boot/caldera-tft.dts) binds fb_ili9481 instead (MZ61581 = R61581
    clone) at 32 MHz — 128 MHz corrupts frames with this init
  - Backlight: GPIO18, active-low, on/off only (no PWM levels). WARNING:
    GPIO18 collides with the DAC HAT's I2S — when rewiring the TFT on jumper
    leads, move the LED line to GPIO12 (hardware PWM, enables dimming)
  - Boot console on the TFT: `fbcon=map:1` (fb0 is the firmware framebuffer,
    fb1 the TFT — stable order with vc4-kms-v3d disabled); vc4 was removed
    (headless, TFT is the only screen). consoleblank=0. A root oneshot
    (caldera-tft-vtunbind.service) releases the console after boot so
    caldera-panel owns the screen; the panel finds the fb by driver name
  - caldera-panel.service (user) runs panel/caldera_panel.py: cover art +
    track info from Plex, state/volume from the local Companion timeline,
    dB readout, RGB565 rendering via Pillow/numpy Spare Pi 2 boards (Ethernet only) are earmarked for future wired
  multi-room zones, one PCM5122 HAT each; the 3B+ stays the main unit.
- v2: detachable display remote — Waveshare ESP32-C6-LCD-1.47 (172x320 LCD,
  onboard LiPo charging) + EC11 encoder + LiPo 500-900 mAh. Magnetic 3-pin
  pogo dock on the front panel (center pin 5 V, outer pins GND so a 180-degree
  flip is polarity-safe). Docked it acts as the system's always-on front
  display; detached it is a handheld remote.
  Talks Wi-Fi directly: control via Caldera Companion HTTP (port 32500),
  now-playing metadata + cover art from the Plex server (/status/sessions,
  /photo/:/transcode?width=172&height=172 for a 172x172 JPEG). No receiver
  dongle or bridge needed. LVGL UI: cover art top, title/artist below,
  volume overlay on rotation showing dB (attenuation = 20*log10(vol/100),
  curve to be verified by measuring the HAT output through the UCA202's ADC
  inputs at several volume settings).
  Screen full brightness on interaction/track change, dim then sleep after;
  charging whenever docked. The 3 spare XIAO ESP32-C6 remain for a future
  minimal knob or a second room.

## Boot tuning (applied 2026-08-15)

Boot trimmed from 37.9 s to 25.4 s. Disabled as nonessential: bluetooth (+
`dtoverlay=disable-bt`), avahi-daemon, udisks2, keyboard/console-setup,
e2scrub, rpi-eeprom-update, NetworkManager-wait-online, cloud-init (via
`/etc/cloud/cloud-init.disabled`), rpi-resize-swap-file (masked), and the
apt-daily / man-db / dpkg-db-backup / e2scrub timers. `config.txt`:
`disable_splash=1`, `boot_delay=0`, camera/display auto-detect off. Wi-Fi
power save off (`/etc/NetworkManager/conf.d/wifi-powersave.conf`).

Remaining bottleneck is NetworkManager (~12 s, Wi-Fi association + DHCP);
Ethernet would cut most of it, but Wi-Fi was kept (always-on appliance,
boot time is rare); initial_turbo=60 added 2026-08-16. Caldera starts
without waiting for network and registers once the link is up
(`Restart=on-failure` covers the edge cases).

## Troubleshooting

| Symptom | Check |
|---|---|
| No `sndrpihifiberry` in `aplay -l` | `config.txt`: overlay line present, onboard audio off; HAT seated on all 40 pins |
| Player not visible in Plexamp | `systemctl --user status caldera-music`; same LAN/subnet as the controller; login step completed |
| Player vanishes / spinner in Plexamp | Known Caldera 1.0.47 bug: after a playback stop, a `findBestConnection: waiting for in-flight race` can freeze the shared event loop, so Companion (port 32500) stops answering even on localhost. Diagnose with `curl -m 5 http://localhost:32500/resources`. Mitigated by `caldera-watchdog.timer` (user unit, probes every 30 s and restarts the service on timeout); worst case the player is gone ~35 s. Service runs with `--verbose` (drop-in `verbose.conf`) to capture evidence for an upstream report |
| Dropouts on Wi-Fi | Check signal (`iw dev wlan0 link`, was -25 dBm); verify powersave still off; RJ45 fallback exists on the rear panel |
| Service dies after SSH logout | `loginctl enable-linger caldera` was skipped |
| Hiss or hum on the amp | Use RCA out (not 3.5 mm); try a different PSU — cheap chargers inject noise |

## Notes

- On-the-fly track analysis runs slower on a Pi 3 than a Pi 4 but happens in
  the background and never blocks playback. Memory footprint is ~34 MB.
- The daemon runs as a user service: logs via
  `journalctl --user -u caldera-music -f`.
