# Caldera HiFi

Headless Plex music player on a Raspberry Pi 3 with a color touch display,
feeding an amplifier through an I2S DAC HAT. Powered by
[Caldera Music headless](https://caldera.homes/music/headless/).
Endgame: everything (Pi, DAC, amp, PSU filtering, display, detachable
remote) in one metal case — a self-built DAC/streamer.

## Current state (2026-08-16)

Registered with plex.tv as player "RaspiAudiophile". Playing, with a live
now-playing touch display.

- Host: `RaspiAudiophile`, user `giorgiofox`, IP `192.168.1.187` (Wi-Fi,
  DHCP — router reservation still pending). SSH alias: `ssh caldera`
- Caldera Music 1.1.0-beta.1 (beta channel), user service + linger,
  Companion watchdog timer active
- Display: Tontec MZ61581 3.5" TFT (480x320) live with two touch-switchable
  views — see "Control panel" below
- Power: external 24 V brick planned into the case; on the bench the Pi runs
  from a known-good 5 V supply after the phone charger caused under-voltage
- Interim DAC: Behringer UCA202 (USB, 16-bit/48 kHz cap) on
  `hw:CARD=CODEC,DEV=0`, `audio.sampleRate=48000`
- Arriving: Leikurvo PCM5122 HAT — swap procedure below

## Repository layout

```
README.md                  this file
panel/caldera_panel.py     now-playing display service (runs on the Pi)
pi/boot/config.txt         live copy of the Pi's boot config
pi/boot/cmdline.txt        live copy of the kernel command line
pi/boot/caldera-tft.dts    source of the custom TFT overlay
pi/etc/                    system config (NetworkManager, vtunbind unit)
pi/systemd-user/           caldera-music/panel/watchdog user units
remote/                    (future) ESP32-C6 remote firmware
cad/                       (future) Fusion 360 / STL for the case
```

Rule: every change made on the Pi gets synced back here and committed.

## Switching to the HAT (when it arrives)

1. Power off. Move the TFT from the header to jumper leads (stacking header
   on top of the HAT). Pin map below. **Move the TFT LED wire from GPIO18
   to GPIO12** — GPIO18 is I2S bit clock, the HAT needs it. Edit
   `pi/boot/caldera-tft.dts`: `led-gpios` `0x12` (18) -> `0x0c` (12),
   recompile with `dtc -I dts -O dtb`, copy to `/boot/firmware/overlays/`.
   GPIO12 is hardware PWM: backlight dimming becomes possible.
2. Mount the HAT on the 40-pin header, power on
3. In `/boot/firmware/config.txt`: uncomment `dtoverlay=hifiberry-dacplus`,
   reboot
4. `aplay -l` should show `sndrpihifiberry`
5. Point Caldera at it, bit-perfect (no resampling):

```sh
~/caldera-music/caldera-music --set audio.outputDeviceUid=hw:CARD=sndrpihifiberry,DEV=0 \
                              --set audio.sampleRate=0
systemctl --user restart caldera-music
```

6. Afterwards: try `audio.audioBufferMs` back down (100 -> 50 or 25) for
   snappier volume response; the 100 ms value was an under-voltage-era
   mitigation.

The freed UCA202 becomes a measurement tool: loop the HAT's RCA out into the
UCA202's ADC inputs, record a test tone at several volume settings, measure
the RMS deltas — that calibrates the real Caldera volume curve for the dB
readouts (currently assumed 0.5 dB/step).

## Hardware

| Part | Detail |
|---|---|
| Board | Raspberry Pi 3B+ (5 GHz Wi-Fi) |
| DAC | Leikurvo HiFi DAC HAT, PCM5122, dual oscillator, RCA out (arriving) |
| Display | Tontec MZ61581-PI-EXT 3.5" TFT, 480x320, SPI, ADS7846 resistive touch |
| Amp | Sure Electronics AA-AB32361 (TDA7498E, 2x160 W @ 4 ohm, 15-36 V DC), board 121.92 x 91.44 mm, no public STEP model — measure and mock up in Fusion |
| Storage | microSD 16 GB (music streams from Plex; card holds OS + cache) |
| Network | Wi-Fi 5 GHz (-25 dBm, 433 Mbps link — final choice; FLAC 24/96 is ~5 Mbps). Rear-panel RJ45 in the case design as fallback |
| Output | HAT RCA -> amp RCA in. Amp screw terminals -> speakers |

Spare Pi 2 boards (Ethernet only) are earmarked for future wired multi-room
zones, one PCM5122 HAT each; Caldera syncs rooms natively.

## Setup from scratch

### 1. Flash the SD card (Raspberry Pi Imager)

- Device: Raspberry Pi 3; OS: Raspberry Pi OS Lite (64-bit); storage: microSD
- Customization: hostname, user, Wi-Fi, locale, SSH with public key
- Keep the card mounted afterwards and apply `pi/boot/config.txt` and
  `pi/boot/cmdline.txt` from this repo (DAC overlay, TFT overlay, console
  mapping, boot tuning)

### 2. First boot

```sh
aplay -l          # DAC present?
ls /dev/fb*       # TFT framebuffer present?
```

### 3. Install Caldera Music

```sh
curl -sSL https://releases.caldera.homes/music/headless/install.sh | bash
~/caldera-music/caldera-music --login --player-name <NAME>   # plex.tv/link PIN
loginctl enable-linger $USER
systemctl --user enable --now caldera-music
```

Non-interactive device setup: `--login --device <UID>` with the saved token
skips the menu. `--list-devices` shows ALSA UIDs.

### 4. Install the panel

```sh
sudo apt install python3-numpy python3-pil python3-requests python3-evdev fonts-dejavu-core
mkdir ~/caldera-panel && cp panel/caldera_panel.py ~/caldera-panel/
cp pi/systemd-user/caldera-panel.service ~/.config/systemd/user/
sudo cp pi/etc/caldera-tft-vtunbind.service /etc/systemd/system/
sudo systemctl enable caldera-tft-vtunbind
systemctl --user daemon-reload && systemctl --user enable --now caldera-panel
sudo usermod -aG video,input $USER
```

## Caldera channel and upgrades

Currently on beta (1.1.0-beta.1, switched 2026-08-15 hoping for a fix to the
event-loop freeze in Troubleshooting). To change channel:

```sh
systemctl --user stop caldera-watchdog.timer   # or it aborts the upgrade
curl -sSL https://releases.caldera.homes/music/headless/upgrade.sh | bash -s -- --beta   # or --stable
systemctl --user start caldera-watchdog.timer
```

If the service sticks in "deactivating" (1.0.47 could hang on stop):
`pkill -9 -x caldera-music && systemctl --user restart caldera-music`.
Never `pkill -f` a pattern that appears in your own ssh command line — it
kills the remote shell.

## Control panel

### v1 — TFT + encoder on the Pi (display DONE, encoder pending)

`caldera-panel.service` (user unit) runs `panel/caldera_panel.py`:

- Data: state/volume/track from the local Companion timeline
  (`localhost:32500/player/timeline/poll`), metadata + cover art from the
  Plex server (token read at runtime from Caldera's `preferences.json`),
  whole-track loudness envelope from `/library/streams/{id}/levels`
  (the Plex sonic analysis — same data Plexamp uses for its waveform bar)
- Rendering: Pillow -> RGB565 -> fbtft framebuffer (found by driver name,
  the index moves between boots). Frames written only when content changes
- Views (tap the ADS7846 touchscreen to cycle):
  1. Info: cover 176px + 2-line title + artist/album + bold light-blue
     format line ("FLAC 192 kHz / 24-bit", rate first like Plexamp) +
     waveform seekbar (dB levels converted to linear amplitude for true
     dynamics, played portion in accent) + volume bar with big dB readout
  2. Fullscreen: cover 320x320 left with thin light border, right column
     "V O L U M E" label, 68px dB number with small decimal
     (preamp-display typography), white "dB" unit, vertical volume bar
- dB scale: hi-fi attenuator style, 0.5 dB/step (vol 100 = 0 dB, vol 45 =
  -27.5 dB). To be calibrated against Caldera's real curve (UCA202 loopback)
- Planned views: VU meter — analog style (cream face, arc scale with red
  0/+3 zone, true 300 ms VU ballistics), first version a SINGLE fullscreen
  needle on the L+R mono sum, stereo pair later. Data via ALSA
  multi/loopback tap (or Caldera viz API — the daemon logs mention
  `vizBoost`, worth probing). Then: signal-path + loudness screen
  (LUFS/LRA/peak from the Plex analysis), clock when idle

TFT bring-up facts (hard-won):

- The kernel dropped `fb_mz61581`; the stock `mz61581` overlay silently
  binds fb_s6d02a1 -> wrong init, black screen. Custom `caldera-tft.dtbo`
  (source `pi/boot/caldera-tft.dts`) binds **fb_ili9481** (MZ61581 is an
  R61581 clone) at **32 MHz** — 128 MHz corrupts frames with this init
- Backlight GPIO18, **active-low** (fixed in the overlay), on/off only.
  GPIO18 collides with I2S -> move to GPIO12 on HAT day (PWM dimming)
- Boot console on the TFT: `fbcon=map:1 consoleblank=0` (fb0 = firmware fb,
  fb1 = TFT; stable with vc4-kms-v3d removed — headless, TFT is the only
  screen). `caldera-tft-vtunbind.service` (root oneshot) releases the
  console after boot so the panel owns the screen

Encoder (EC11) wiring when the HAT + stacking header go in:

| Signal | GPIO | Pin | Notes |
|---|---|---|---|
| TFT 5V / GND | - | 2 / 6 | backlight ~100 mA |
| TFT MOSI / SCLK / CE0 | 10 / 11 / 8 | 19 / 23 / 24 | SPI0 |
| TFT DC / RESET | 25 / 15 | 22 / 10 | |
| TFT LED | 12 | 32 | moved off GPIO18, hardware PWM |
| Touch CE1 / IRQ | 7 / 24* | 26 / 18* | *verify against overlay before wiring |
| Encoder A / B / SW | 5 / 6 / 13 | 29 / 31 / 33 | internal pull-ups |
| Encoder commons | - | 30, 34 | GND |

Gestures: rotate = volume, click = play/pause, double click = next track,
long press = previous track (Companion endpoints `/player/playback/...`).

### v2 — detachable display remote (parts ordered)

Waveshare ESP32-C6-LCD-1.47 (172x320, onboard LiPo charging) + EC11 +
LiPo 500-900 mAh. Magnetic 3-pin pogo dock on the front panel (center pin
5 V, outer pins GND — a 180-degree flip is polarity-safe). Docked = always-on
front display + charging; detached = handheld remote. Talks Wi-Fi directly
to Caldera (HTTP 32500) and the Plex server (cover via
`/photo/:/transcode?width=172&height=172`); no receiver dongle needed.
Screen bright on interaction, dim then sleep. The 3 spare XIAO ESP32-C6
boards remain for a future minimal knob / second room.

## Amplifier and power (case integration, planned)

Metal case, 3D-printed PETG front/rear panels (Prusa MK4S / Bambu P1S,
Fusion 360). PETG doubles as the RF window: the Pi's antenna corner (by the
SD slot) must face a PETG panel up close. Case internal minimum
230 x 160 x 55 mm; comfortable 260 x 180 x 70.

- Mean Well 24 V DC brick (purchased) -> rear-panel DC jack -> amp VCC
- DC-DC buck 24->5 V -> Pi via GPIO pins 2/4 (5 V) + 6 (GND). Tune to
  **5.10-5.15 V measured at the Pi's GPIO under full load** before
  connecting — the GPIO feed bypasses the Pi's fuse and reverse-polarity
  protection. Short thick leads (AWG 18-20)
- EMI filter boards (0-50 V 4 A LC, purchased): one between buck and Pi,
  one between brick and amp
- Chassis tied to DC ground at ONE point near the DC inlet (scrape
  paint/anodizing under the lug); no other chassis-ground connections
- At 24 V the TDA7498E delivers ~2x80 W @ 4 ohm
- Planned: amp STBY/MUTE header on Pi GPIOs for pop-free startup/shutdown
  and auto-standby when idle

Power history: a phone-charger PSU caused live under-voltage
(`get_throttled 0xd0005`, kernel "Undervoltage detected!") = intermittent
crackle through the USB DAC; `audio.audioBufferMs` was raised 50 -> 100 as
mitigation. The TFT adds ~150 mA — under-voltage returned on a weak buck
and went away on a good 5 V supply. Rule: after any power change check
`vcgencmd get_throttled` stays `0x0`.

## Boot tuning

37.9 s -> 25.4 s. Disabled: bluetooth (+ `dtoverlay=disable-bt`), avahi,
udisks2, keyboard/console-setup, e2scrub, rpi-eeprom-update,
NetworkManager-wait-online, cloud-init (`/etc/cloud/cloud-init.disabled`),
rpi-resize-swap-file (masked), apt-daily/man-db/dpkg-db-backup/e2scrub
timers. `config.txt`: `disable_splash=1`, `boot_delay=0`,
`initial_turbo=60`, camera/display auto-detect off, vc4-kms-v3d removed.
Wi-Fi powersave off (`/etc/NetworkManager/conf.d/wifi-powersave.conf`).
Remaining bottleneck: NetworkManager ~12 s (Wi-Fi association + DHCP) —
accepted, always-on appliance.

## Troubleshooting

| Symptom | Check |
|---|---|
| No `sndrpihifiberry` in `aplay -l` | overlay line in config.txt, onboard audio off, HAT seated |
| Player not visible in Plexamp | `systemctl --user status caldera-music`; same LAN; login done |
| Player vanishes / spinner | Caldera 1.0.47 bug: event loop freeze after stop (`findBestConnection: waiting for in-flight race`), Companion dead even on localhost. Diagnose: `curl -m 5 http://localhost:32500/resources`. Mitigation: `caldera-watchdog.timer` restarts on 30 s probe timeout. Beta 1.1.0 under observation |
| Cannot open hw:CARD=... No such device | DAC unplugged; Caldera retries every second, just reconnect it |
| TFT black | backlight is active-low (bl_power semantics inverted); check `caldera-tft` overlay loaded, not stock `mz61581` |
| TFT garbled/torn frames | SPI speed crept up? Must be 32 MHz with the ili9481 init |
| Panel service crash loop | `sudo journalctl -u user@1000 | grep caldera-panel`; deps: numpy, PIL, requests, evdev, fonts-dejavu-core |
| Dropouts on Wi-Fi | `iw dev wlan0 link` (was -25 dBm); powersave off; RJ45 fallback |
| Crackle ("prr" like vinyl dust) | `vcgencmd get_throttled` — any non-zero = fix power first |
| Hiss/hum on the amp | buck noise: EMI filter between buck and Pi; single-point chassis ground; last resort RCA ground isolator |

## Notes

- Track analysis runs slower on Pi 3 than Pi 4 but never blocks playback.
  Daemon memory ~34 MB
- Logs: `sudo journalctl -u user@1000 | grep caldera-music` (user journal
  files are not persisted separately)
- Never `pkill -f` over ssh with the pattern in your own command line
