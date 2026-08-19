# Bug report draft for Plex Labs forum

Post to: https://forums.plex.tv/c/labs (reply to thread 939731 "Caldera
Headless: Not showing up on Plexamp iOS, but appears to own the sound
devices" if still open, otherwise new topic).

---

**Title:** Caldera headless: Companion HTTP event loop freezes after
"findBestConnection: waiting for in-flight race" — player vanishes while
audio keeps playing

**Body:**

Caldera Music headless 1.1.0-beta.1 (also seen on 1.0.47) on Raspberry Pi
3A+, Raspberry Pi OS Lite 64-bit, I2S DAC (PCM5122), output via an ALSA
plug device, Wi-Fi connection to a LAN Plex server.

Symptom: playback continues normally, but the Companion HTTP server on
port 32500 stops answering entirely — even `curl http://localhost:32500/resources`
from the same host gets no response (connection never completes). The
player disappears from Plexamp and stays gone until the process is
restarted. It happens sporadically, roughly once a day of active use.

Every occurrence correlates with two things in the logs right before the
freeze:

1. Repeated failing seekprint fetches routed to the plex.direct address
   instead of the LAN address (hairpin NAT makes plex.direct unreachable
   from inside my LAN):

```
19:48:54.618 E (Plex): GET /library/streams/9850/levels?subsample=128 failed after 12196ms: HTTP request failed with status 404 (url=https://192-168-1-250.<hash>.plex.direct:32400, token=REDACTED)
19:48:54.627 W (Seekprint): getLevels returned empty for streamId=9850
```

2. A DeviceManager connection race that then repeats forever — after the
   freeze this line is logged continuously and is the only sign of life:

```
19:48:54.627 D (DeviceManager): findBestConnection: waiting for in-flight race (72ff7f7ba73373d266b751c034c5939984fbd56b)
19:49:06.760 D (DeviceManager): findBestConnection: waiting for in-flight race (72ff7f7ba73373d266b751c034c5939984fbd56b)
```

The same race id repeats for minutes; timeline polls, GDM/Companion
requests and everything HTTP-side stay dead while the ALSA thread keeps
rendering audio.

A second issue makes recovery harder: in this frozen state the process
also ignores SIGTERM, so `systemctl stop` hangs until the unit timeout.
I had to add `TimeoutStopSec=5` to make an external watchdog effective.

Happy to provide full verbose logs on the next occurrence (`--verbose`
drop-in is in place). Is there a workaround to force the LAN connection
and avoid the plex.direct route for the seekprint/levels fetches? That
request failing after 12-24 s appears to be what wedges the connection
race.
