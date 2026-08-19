#!/usr/bin/env bash
# RaspiAudiophile installer.
# Target: Raspberry Pi OS Lite (64-bit), fresh install, run as the login
# user (not root) from the repo root. Re-runnable: every step is
# idempotent.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SHARE=/usr/local/share/caldera

say() { printf '\n== %s\n' "$*"; }

[ "$(id -u)" = 0 ] && { echo "Run as your user, not root."; exit 1; }

say "APT packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3-numpy python3-pil python3-requests python3-evdev \
    python3-gpiozero python3-lgpio fonts-dejavu-core \
    git build-essential autoconf automake libtool libasound2-dev \
    libfftw3-dev alsa-utils curl

say "peppyalsa (ALSA scope plugin for the VU meters)"
if [ ! -e /usr/lib/libpeppyalsa.so ]; then
    rm -rf /tmp/peppyalsa
    git clone -q https://github.com/project-owner/peppyalsa.git /tmp/peppyalsa
    (cd /tmp/peppyalsa &&
        aclocal && libtoolize --force && autoconf &&
        automake --add-missing --force-missing &&
        ./configure --prefix=/usr >/dev/null &&
        make -s -j"$(nproc)" &&
        sudo make -s install)
fi

say "Boot config (I2S DAC overlay, display)"
BOOTCFG=/boot/firmware/config.txt
[ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt
grep -q "hifiberry-dacplus" "$BOOTCFG" || {
    echo "NOTE: add 'dtoverlay=hifiberry-dacplus,slave' to $BOOTCFG"
    echo "      (,slave is REQUIRED on clone HATs with doubled crystals)"
}

say "ALSA tap + VU FIFO"
sudo install -m 644 "$REPO/pi/etc/asound.conf" /etc/asound.conf
echo "p /tmp/peppyalsa_fifo 0666 $USER $USER" | sudo tee /etc/tmpfiles.d/peppyalsa.conf >/dev/null
sudo systemd-tmpfiles --create /etc/tmpfiles.d/peppyalsa.conf

say "Wi-Fi power save off (audio dropouts otherwise)"
sudo install -m 644 "$REPO/pi/etc/NetworkManager-conf.d/wifi-powersave.conf" \
    /etc/NetworkManager/conf.d/wifi-powersave.conf 2>/dev/null || true

say "Panel configuration"
if [ ! -f /etc/raspiaudiophile.conf ]; then
    sudo install -m 644 "$REPO/pi/etc/raspiaudiophile.conf.example" /etc/raspiaudiophile.conf
    read -rp "LAN address of your Plex server (e.g. http://192.168.1.10:32400): " PLEX
    [ -n "$PLEX" ] && sudo sed -i "s|^server = .*|server = $PLEX|" /etc/raspiaudiophile.conf
fi

say "Panel, splash, assets"
mkdir -p "$HOME/caldera-panel"
install -m 755 "$REPO/panel/caldera_panel.py" "$HOME/caldera-panel/caldera_panel.py"
sudo mkdir -p "$SHARE/skins"
sudo cp "$REPO/panel/assets/hires_logo.png" "$SHARE/hires_logo.png"
sudo cp -r "$REPO/panel/skins/." "$SHARE/skins/"
python3 "$REPO/panel/make_splash.py"
sudo install -m 644 /tmp/splash.raw "$SHARE/splash.raw"
sudo install -m 755 "$REPO/pi/etc/caldera-splash.sh" /usr/local/bin/caldera-splash.sh

say "System units and rules"
sudo install -m 644 "$REPO/pi/etc/caldera-splash.service" /etc/systemd/system/
sudo install -m 644 "$REPO/pi/etc/caldera-vtunbind.service" /etc/systemd/system/
sudo install -m 644 "$REPO/pi/etc/52-backlight.rules" /etc/udev/rules.d/52-backlight.rules
sudo systemctl daemon-reload
sudo systemctl enable caldera-splash.service caldera-vtunbind.service

say "User units"
mkdir -p "$HOME/.config/systemd/user"
cp -r "$REPO/pi/systemd-user/." "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
loginctl enable-linger "$USER"
sudo usermod -aG video,input,gpio,audio "$USER"

say "Caldera Music"
if [ ! -d "$HOME/caldera-music" ] && ! command -v caldera-music >/dev/null; then
    echo "Installing Caldera Music headless..."
    curl -sSL https://releases.caldera.homes/music/headless/install.sh | bash
    echo
    echo "Now link the player (plex.tv PIN):"
    echo "  caldera-music --login --player-name RaspiAudiophile"
    echo "Then set:"
    echo "  audio.outputDeviceUid=caldera_tap  audio.sampleRate=0  audio.audioBufferMs=100"
fi

say "Enable services"
systemctl --user enable caldera-music caldera-panel caldera-watchdog.timer || true

echo
echo "Done. Reboot (group membership and boot config need it), link the"
echo "player if you haven't, and play something."
