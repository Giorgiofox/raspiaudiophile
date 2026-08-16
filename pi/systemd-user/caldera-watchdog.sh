#!/bin/bash
if ! curl -s -m 5 -o /dev/null http://localhost:32500/resources; then
  echo "Companion unresponsive, restarting caldera-music"
  systemctl --user restart caldera-music
fi
