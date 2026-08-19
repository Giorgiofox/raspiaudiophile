# RaspiAudiophile

A complete hi-fi streamer built on a Raspberry Pi 3A+ and
[Caldera Music headless](https://caldera.homes/music/headless/): bit-perfect
Plex playback through an I2S DAC, a 7" touch display with three views
(now-playing with waveform seekbar, fullscreen cover, analog VU meters), a
physical rotary encoder for volume and transport, and appliance behaviors
(boot splash, screen sleep, self-healing services).

Endgame: everything in one metal case with a class D amp — a self-built
DAC/streamer/amplifier. Later: open-source release with an installer.

## Current state (2026-08-17)

Fully working. Registered with plex.tv as player "RaspiAudiophile".

- Host `RaspiAudiophile`, user `giorgiofox`, Wi-Fi (DHCP — router
  reservation still pending). SSH alias: `ssh caldera` (update the IP in
  `~/.ssh/config` when it changes)
- Caldera Music beta channel, user service + linger, Companion watchdog
- Output: Leikurvo PCM5122 HAT in **slave clock mode**, bit-perfect
  (`audio.sampleRate=0`) through the ALSA tap (see Audio path)
- Display: official Raspberry Pi 7" Touch Display (DSI, 800x480, firmware
  fb0, FT5406 capacitive touch, sysfs backlight)
- Encoder: KY-040 — volume, play/pause, next/previous
- Repo: github.com/Giorgiofox/raspiaudiophile (private)

## Hardware

| Part | Detail |
|---|---|
| Board | Raspberry Pi 3 Model A+ (same footprint as the HAT) |
| DAC | Leikurvo HiFi DAC HAT (PCM5122). Clone quirks below |
| Display | Official RPi 7" Touch Display v1.1, DSI ribbon + 5V/GND jumpers |
| Encoder | KY-040 rotary encoder module |
| Amp (planned) | Sure Electronics AA-AB32361 (TDA7498E, 15-36 V DC), board 121.92x91.44 mm |
| PSU (planned) | Mean Well 24 V brick -> amp direct + buck 24->5.1 V for the Pi |
| Storage | microSD 16 GB (music streams from Plex) |

### Clone HAT quirks (hard-won)

- **Master ("Pro") clock mode plays at exactly 2x speed**: the board mounts
  double-frequency crystals (45.1584/49.152 MHz instead of 22.5792/24.576).
  Run it with `dtoverlay=hifiberry-dacplus,slave` — the PCM5122's internal
  PLL cleans the Pi clock and it sounds excellent.
- **GPIO 5 and 6 gate the onboard oscillators.** Anything driving those pins
  kills the audio clock (discovered the hard way with the encoder: silences
  and skips at every detent). Also reserved: 18/19/21 (I2S), 2/3 (I2C).

### Encoder wiring (final)

| Signal | GPIO (BCM) | Physical pin |
|---|---|---|
| CLK | 16 | 36 |
| DT | 26 | 37 |
| SW | 13 | 33 |
| + | 3.3V | 17 (never 5V: the module pulls the signal lines to +) |
| GND | — | 39 |

## Audio path

```
Plex server --(FLAC up to 24/192)--> Caldera daemon --> ALSA "caldera_tap"
                                       (plug -> meter scope -> hw DAC, source rate)
                                                     `-> peppyalsa -> /tmp/peppyalsa_fifo -> panel VU
```

`/etc/asound.conf` defines the tap: a `plug` over an alsa-lib `meter`
device wrapping the DAC, with the [peppyalsa](https://github.com/project-owner/peppyalsa)
scope plugin (the one Volumio uses) writing L/R peak levels to a FIFO the
panel reads. One slave device only — any rate the DAC accepts opens.

History: the previous tap was a `multi` device duplicating the stream to
the DAC and a fixed-48k snd-aloop branch. `multi` couples the period/rate
constraints of its slaves, so every 44.1k-family track failed to open
(EINVAL) — the root cause of all "this album won't play" incidents. Gone
for good. peppyalsa is built from source (needs `libfftw3-dev`, autotools);
the FIFO is created at boot by `pi/etc/peppyalsa-tmpfiles.conf`.

## The panel (`panel/caldera_panel.py`)

One Python service (`caldera-panel`, user unit) renders to `/dev/fb0`
(800x480 XRGB) with Pillow+numpy, no X server.

Data: state/volume/track from the local Companion timeline
(`localhost:32500/player/timeline/poll`, polled in a thread), metadata and
cover art from the Plex server (LAN address preferred — the timeline
sometimes advertises the unreachable remote plex.direct route), whole-track
loudness envelope from `/library/streams/{id}/levels` (the Plexamp waveform
data), live L/R levels from the loopback capture.

Views (tap the touchscreen to cycle):

1. **Info**: cover 340px, 2-line title, artist/album, format line (rate
   first, light blue), waveform seekbar (dB levels -> linear amplitude,
   percentile-normalized, gamma 1.6, prerendered strips), volume bar +
   dB readout aligned to the number
2. **Fullscreen**: cover 480x480 left; right column: mono VU meter (max of
   L/R), format line, big dB
3. **VU meters**: the builtin "amber" skin — two amber tungsten-lit faces
   (supersampled 3x, even tick spacing, orange 0..+1 then red, colored
   labels), anti-aliased tapered needles with a parallax-cast soft shadow,
   true VU ballistics (99% in 300 ms) plus a mechanical slew limit,
   ~25 fps via numpy-precomposed frames; format printed on the left face,
   volume dB on the right; waveform strip below.
   Plus **PeppyMeter skins** (circular and linear, vendored in
   `panel/skins/`, GPLv3, credit project-owner/PeppyMeter): tap the
   bottom-right/bottom-left strip of the VU view for next/previous skin;
   the skin name shows for 1.5 s. Hide skins via the `[skins] exclude`
   config key. Live levels come from the peppyalsa FIFO for all skins.

Encoder: rotate = volume (0.5 dB/detent, optimistic UI echo so the display
tracks instantly, coalesced Companion sends), click = play/pause, double
click = next, long press = previous. Ghost-click guards (rotation window,
minimum press time, stiff debounce) tame the KY-040's wobbly shaft switch.

Appliance behaviors: boot splash (`caldera-splash.service` writes a
pre-rendered frame as soon as fb0 exists), console released from fb0 after
boot (`caldera-vtunbind.service`), screen off after 3 min idle
(10 min paused -> idle -> 3 min -> backlight off), any touch or encoder
activity or resumed playback wakes it; panel start syncs the backlight on.

dB honesty: volume dB assumes 0.5 dB/step (vol 100 = 0 dB); VU 0 VU sits at
-8 dBFS, set by ear. Both await calibration by measuring the HAT output
through the retired UCA202's ADC inputs.

## Repository layout

```
README.md                  this file
LICENSE                    MIT (skins: GPLv3, see panel/skins/)
install.sh                 one-shot installer for a fresh Pi
panel/caldera_panel.py     the panel service
panel/make_splash.py       boot splash generator
panel/skins/               PeppyMeter VU skins (GPLv3, vendored)
panel/assets/              Hi-Res Audio logo
pi/boot/config.txt         live copy of the Pi's boot config
pi/boot/cmdline.txt        live copy of the kernel command line
pi/etc/                    asound.conf, units, udev rules, conf example
pi/systemd-user/           caldera-music/panel/watchdog user units
docs/                      upstream bug reports, notes
remote/                    (future) ESP32-C6 display-remote firmware
cad/                       (future) Fusion 360 / STL for the case
```

Rule: every change made on the Pi gets synced back here, committed, pushed.

## Setup from scratch

1. Flash Raspberry Pi OS Lite (64-bit); customize hostname/user/Wi-Fi/SSH.
   Apply `pi/boot/config.txt` to the boot partition before first boot
   (or at least `dtoverlay=hifiberry-dacplus,slave`).
   Verify the filesystem got expanded (`df -h /` — a 100% full 2.2G root
   means firstboot never ran; `sudo raspi-config nonint do_expand_rootfs`)
2. Clone this repo and run `./install.sh`. It installs packages, builds
   peppyalsa, deploys the ALSA tap, panel, skins, splash and units, and
   walks through the Caldera Music install.
3. Link the player: `caldera-music --login --player-name <name>`
   (plex.tv/link PIN), then set `audio.outputDeviceUid=caldera_tap`,
   `audio.sampleRate=0`, `audio.audioBufferMs=100`
4. Reboot.

## Configuration

Panel settings live in `/etc/raspiaudiophile.conf` (INI; every key
optional, defaults in `pi/etc/raspiaudiophile.conf.example`): Plex LAN
address, encoder GPIO pins, dB-per-step, VU reference, screen timeouts
and the list of VU skins to hide.

## Caldera channel and upgrades

```sh
systemctl --user stop caldera-watchdog.timer   # or it aborts the upgrade
curl -sSL https://releases.caldera.homes/music/headless/upgrade.sh | bash -s -- --beta   # or --stable
systemctl --user start caldera-watchdog.timer
```

## Boot tuning

37.9 s -> ~22 s: bluetooth/avahi/udisks2/keyboard-console-setup/e2scrub/
eeprom-update/wait-online/cloud-init disabled, apt & friends timers off,
swap-resize masked, no initramfs, `initial_turbo=60`, splash instead of
console. Remaining bottleneck: Wi-Fi association (~11 s) — accepted,
always-on appliance.

## Troubleshooting

| Symptom | Check |
|---|---|
| Player vanishes / Plexamp spinner | Caldera event-loop freeze (1.0.47 bug, beta under observation): `curl -m 5 http://localhost:32500/resources`; watchdog restarts it within 30 s |
| Songs stall at 0:00-0:01 or some albums refuse to play | Historical multi/aloop tap symptom — should be extinct with the peppyalsa tap. If it returns: `aplay -D caldera_tap -f S16_LE -r 44100 /dev/zero` (silent) to probe |
| VU needles dead, audio fine | Panel can't read `/tmp/peppyalsa_fifo`: check the FIFO exists (tmpfiles) and `libpeppyalsa.so` is installed |
| "Failed to initialize audio backend" repeats, silent playback, frozen VU | Poisoned state after a device race at startup: `systemctl --user restart caldera-music` (the panel's strict-params capture prevents the race itself) |
| Double-speed playback | Master clock mode on the clone HAT — keep `,slave` |
| Audio dies when touching GPIO wires | You're on GPIO 5/6 (oscillator gates) or 18/19/21 (I2S) — move |
| Crackle ("prr") | `vcgencmd get_throttled` — any non-zero: fix power first. Under-voltage was the root cause of every mystery in this project's history |
| Cover/waveform missing | Track served via remote plex.direct route (hairpin NAT): set `[plex] server` in `/etc/raspiaudiophile.conf` to the LAN address |
| Panel crash loop | `sudo journalctl -u user@1000 | grep caldera-panel`; check python deps and fonts-dejavu-core |
| Encoder ghost play/pause | Shaft wobble: guards exist; check + is on 3.3V, not 5V |
| Screen won't wake | `echo 0 | sudo tee /sys/class/backlight/rpi_backlight/bl_power`; panel start now resyncs it |

## Roadmap

- Calibrate volume curve and 0 VU with the UCA202 ADC loopback
- Case: Mean Well 24 V + buck 5.1 V, EMI filters, star ground, single-point
  chassis bond; Fusion CAD (amp board 121.92x91.44 mm, no public STEP —
  measure and mock up); PETG front/rear panels as Wi-Fi window
- Touch buttons instead of blind view cycling
- Detachable display remote (Waveshare ESP32-C6-LCD-1.47 + pogo dock)
- Multi-room: spare Pi 2 fleet, one DAC HAT each, Caldera syncs natively
- Open-source: wiring diagrams, sanitization pass, then public
