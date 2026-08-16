#!/bin/sh
for i in $(seq 1 100); do
  for f in /sys/class/graphics/fb*; do
    if grep -q ili9481 "$f/name" 2>/dev/null; then
      cat /usr/local/share/caldera/splash.raw > "/dev/$(basename "$f")"
      exit 0
    fi
  done
  sleep 0.2
done
