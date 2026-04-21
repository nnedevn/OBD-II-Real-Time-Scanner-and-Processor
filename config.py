"""
OBD II + LLM Scanner — Active Configuration
=============================================
This file selects which hardware profile to load.
All other files import from here — nothing else needs to change.

To switch hardware, change PROFILE below and restart the scanner.

Available profiles
------------------
  "pi3b"  →  config_pi3b.py  — Raspberry Pi 3B+ (1 GB RAM, granite4:350m)
  "fast"  →  config_fast.py  — i7 / 16 GB RAM desktop or laptop (granite4:3b)

Quick-start checklist (both profiles)
--------------------------------------
  1. Set OBD_PORT in the chosen profile file (or leave None for auto-detect):
       Linux (Pi OS / Ubuntu):  "/dev/rfcomm0"
       (after: sudo rfcomm bind /dev/rfcomm0 <MAC>)

  2. Pull the model for your profile:
       Pi 3B+:   ollama pull granite4:350m
       Fast PC:  ollama pull granite4:3b

  3. Pi 3B+ only — disable BT power management before first run:
       sudo hciconfig hci0 nsniff
     (See README Step 3 to make this permanent.)
"""

# ── SELECT YOUR HARDWARE PROFILE ──────────────────────────────────────────────
# Change this one line to switch between machines.
PROFILE = "pi3b"   # "pi3b" | "fast"
# ─────────────────────────────────────────────────────────────────────────────

if PROFILE == "pi3b":
    from config_pi3b import *          # noqa: F401, F403
elif PROFILE == "fast":
    from config_fast import *          # noqa: F401, F403
else:
    raise ValueError(
        f"Unknown PROFILE '{PROFILE}' in config.py. "
        f"Valid options: 'pi3b', 'fast'"
    )
