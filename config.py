"""
OBD II + LLM Scanner — Configuration
=====================================
Edit this file to match your hardware setup before running.

Current hardware profile:
  Adapter:  OBDLink MX+ (Bluetooth, STN chipset)
  Computer: Raspberry Pi 3B+ (prototype)
  Vehicle:  2012 Volvo C30 T5 (B5254T2)

Vehicle profile is loaded from vehicle_profile.py.
To switch vehicles, replace that file with a different profile.

Pi 3B+ notes
-------------
The Pi 3B+ has 1 GB of RAM and ARM Cortex-A53 cores at 1.4 GHz.
Key constraints vs Pi 4:
  - RAM is tight: OS (~250 MB) + granite4:350m (~250 MB) + Python (~150 MB)
    ≈ 650 MB, leaving ~350 MB headroom. Do not run other services.
  - CPU is ~40% slower for single-threaded work — keep poll intervals relaxed.
  - Bluetooth stack behaves identically to Pi 4 but is more sensitive to
    power-management settings (see README troubleshooting section).

macOS (development / testing)
------------------------------
If running on macOS, set OBD_PORT to the serial device created by macOS
when you pair the OBDLink MX+ — typically /dev/cu.OBDLinkMXp-SPPDev or
similar. Find it with:  ls /dev/cu.* | grep -i obd
No rfcomm binding is needed on macOS; the port appears automatically.
"""

from vehicle_profile import STANDARD_PIDS, THRESHOLDS_C30_T5, VOLVO_MODE22_COMMANDS

# ── Bluetooth / OBD Connection ────────────────────────────────────────────────
# Linux (Pi 3B+):
#   After pairing via bluetoothctl:
#     sudo rfcomm bind /dev/rfcomm0 <ADAPTER_MAC>
#   Then set OBD_PORT = "/dev/rfcomm0"
#
# macOS:
#   Pair in System Settings → Bluetooth. The port appears automatically.
#   Set OBD_PORT to the path shown by:  ls /dev/cu.* | grep -i obd
#   Typically: "/dev/cu.OBDLinkMXp-SPPDev"  (exact name varies by MX+ serial)
#
# OBDLink MX+ notes:
#   - STN2120 chipset — faster and more reliable than generic ELM327
#   - Supports all OBD-II protocols including Volvo's ISO 14230-4 (KWP2000)
#   - OBD_FAST = False is recommended for Pi 3B+ stability (re-enable after
#     confirming the connection is solid — it's faster when it works)
#   - 38400 baud is reliable over Bluetooth SPP on all platforms
#   - The MX+ draws < 1 mA in sleep mode — safe to leave plugged in
OBD_PORT = None          # Set to "/dev/rfcomm0" (Linux) or "/dev/cu.OBD..." (macOS)
OBD_BAUDRATE = 38400     # 38400 is reliable over Bluetooth SPP
OBD_TIMEOUT = 30         # Seconds to wait for initial ECU response
OBD_FAST = False         # Keep False on Pi 3B+ — tight ELM327 timing + slow CPU
                         # causes intermittent drops. Re-enable once stable.

# ── Connection Resilience ────────────────────────────────────────────────────
# The OBD reader will auto-reconnect if the Bluetooth link drops mid-session.
# This is especially important on Pi 3B+ where BT power management can cause
# brief disconnections.
OBD_RECONNECT_ATTEMPTS = 5     # How many times to retry before giving up
OBD_RECONNECT_BASE_WAIT = 3    # Initial wait in seconds (doubles each attempt,
                                # capped at 30s): 3, 6, 9, 12, 15 …

# ── Polling ───────────────────────────────────────────────────────────────────
# Pi 3B+ is slower than Pi 4 — keep the interval at 2s to avoid CPU saturation
# when Ollama is also running. Drop to 1.0s only after confirming stability.
POLL_INTERVAL_SECONDS = 2.0       # How often to sample all standard PIDs (s)
DTC_POLL_INTERVAL_SECONDS = 30.0  # How often to check for fault codes (s)

# Mode 22 PIDs — DISABLED by default on Pi 3B+.
# The startup probe sends 10+ blocking serial commands which can destabilise
# a fresh Bluetooth connection on slower hardware. Once your standard OBD
# connection is solid, change this to 5.0 to re-enable Mode 22 polling.
MODE22_POLL_INTERVAL_SECONDS = 0  # 0 = disabled. Change to 5.0 when ready.

# Standard OBD-II Mode 01 PIDs — sourced from vehicle_profile.py
MONITORED_PIDS = STANDARD_PIDS + [
    "SPEED",
]

# Volvo-specific Mode 22 custom commands — sourced from vehicle_profile.py.
# Overridden to empty list when MODE22_POLL_INTERVAL_SECONDS = 0, but kept
# here so switching back is a one-line change.
MODE22_COMMANDS = VOLVO_MODE22_COMMANDS

# ── Data Buffer ───────────────────────────────────────────────────────────────
# Smaller buffer on Pi 3B+ to keep RAM usage down.
BUFFER_SIZE = 30          # 30 samples = 1 minute at 2Hz polling
LLM_CONTEXT_SAMPLES = 8   # Last 8 samples (~16s) sent to LLM per analysis call

# ── LLM — Model Selection ─────────────────────────────────────────────────────
# Pi 3B+ (1 GB RAM) — use the smallest model that fits:
#
#   granite4:350m       — ~250 MB RAM, fastest option for Pi 3B+.
#                          Pull: ollama pull granite4:350m
#
#   granite3.1-moe:1b   — ~600 MB RAM, slightly better quality.
#                          Only viable if nothing else is running. RAM will
#                          be very tight — watch for OOM kills.
#                          Pull: ollama pull granite3.1-moe:1b
#
# Pi 4 (4–8 GB RAM):
#   granite3.1-moe:1b   — comfortable at 4 GB. Use granite3-dense:2b at 8 GB.
#
# Final mini PC (Beelink SER7 / Minisforum UM780):
#   granite3-dense:8b   — full quality, ~5–6 GB RAM, fast inference.
#
OLLAMA_BASE_URL = "http://localhost:11434"
LLM_MODEL = "granite4:350m"   # Fits comfortably in Pi 3B+'s 1 GB RAM
LLM_TEMPERATURE = 0.2
LLM_MAX_TOKENS = 256      # Reduced for Pi 3B+ — keeps responses snappy
LLM_STREAM = True

# Longer interval on Pi 3B+ — LLM inference is slower, give CPU breathing room
LLM_PERIODIC_SUMMARY_INTERVAL = 120  # seconds (0 = disable periodic summaries)

# ── Anomaly Detection Thresholds ──────────────────────────────────────────────
THRESHOLDS = THRESHOLDS_C30_T5

# ── Output / Logging ──────────────────────────────────────────────────────────
LOG_RAW_DATA = True
LOG_DIR = "./logs"
SHOW_LIVE_DASHBOARD = False  # Use browser dashboard: python main.py --no-terminal
VERBOSE = False
