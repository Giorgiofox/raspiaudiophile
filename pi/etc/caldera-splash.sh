#!/bin/sh
for i in $(seq 1 50); do [ -e /dev/fb0 ] && cat /usr/local/share/caldera/splash.raw > /dev/fb0 && exit 0; sleep 0.2; done
