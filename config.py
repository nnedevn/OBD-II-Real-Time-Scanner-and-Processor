"""
OBD II + LLM Scanner — Configuration
=====================================
Edit this file to match your hardware setup before running.

Current hardware profile:
  Adapter:  OBDLink MX+ (Bluetooth, STN chipset)
  Computer: Raspberry Pi 4 (prototype)
  Vehicle:  2012 Volvo C30 T5 (B5254T2)

Vehicle profile is loaded from vehicle_profile.py.
To switch vehicles, replace that file with a different profile.
"""

from vehicle_profile import STANDARD_PIDS, THRESHOLDS_C30_T5, VOLVO_MODE22_COMMANDS

# ── Bluetooth / OBD Connection ────────────────────────────────────────────────
# OBDLink MX+ pairs as a standard Bluetooth SPP device on Linux.
# After pairing via bluetoothctl:
#   sudo rfcomm bind /dev/rfcomm0 <ADAPTER_MAC>
# Then set OBD_PORT = "/dev/rfcomm0"
#
# OBDLink MX+ notes:
#   - Uses ScanTool STN2120 chipset — faster and more reliable than generic ELM327
#   - Supports all OBD-II protocols including Volvo's ISO 14230-4 (KWP2000)
#   - OBD_FAST = True is fully supported and recommended
#   - Higher baud rates are possible (115200) but 38400 is stable over BT SPP
#   - The MX+ has built-in cable relief and a 16-pin connector lock — leave it
#     plugged in permanently; it draws < 1 mA in sleep mode
OBD_PORT = None          # Set to "/dev/rfcomm0" after pairing
OBD_BAUDRATE = 38400     # 38400 is reliable over Bluetooth SPP on Pi 4
                         # Try 115200 if you want faster raw throughput (test first)
OBD_TIMEOUT = 30         # Seconds to wait for initial ECU response
OBD_FAST = True          # STN chipset fully supports fast init

# ── Polling ───────────────────────────────────────────────────────────────────
# The OBDLink MX+ is faster than generic adapters, but the Pi 4's CPU is the
# bottleneck at this polling rate. 1.0s is safe; drop to 0.5s cautiously.
POLL_INTERVAL_SECONDS = 1.0       # How often to sample all standard PIDs (s)
DTC_POLL_INTERVAL_SECONDS = 15.0  # How often to check for fault codes (s)

# Mode 22 PIDs — poll less frequently on Pi 4 to avoid CPU contention with LLM
# Set to 0 to disable Mode 22 entirely during initial testing.
MODE22_POLL_INTERVAL_SECONDS = 3.0

# Standard OBD-II Mode 01 PIDs — sourced from vehicle_profile.py
MONITORED_PIDS = STANDARD_PIDS + [
    "SPEED",
]

# Volvo-specific Mode 22 custom commands — sourced from vehicle_profile.py
# Set to [] to disable during initial adapter testing.
MODE22_COMMANDS = VOLVO_MODE22_COMMANDS

# ── Data Buffer ───────────────────────────────────────────────────────────────
# Reduced from 120 to 60 on Pi 4 to keep memory usage modest.
BUFFER_SIZE = 60          # 60 samples = 1 minute at 1Hz
LLM_CONTEXT_SAMPLES = 10  # Last 10 seconds sent to LLM per analysis call

# ── LLM — Raspberry Pi 4 Model Selection ──────────────────────────────────────
# The Pi 4 cannot run the 8B model at useful speed. Use one of:
#
#   granite3.1-moe:1b   — 1B MoE model, ~600 MB RAM, ~6-10 tok/s on Pi 4
#                          Best choice for Pi 4 with 4 GB RAM.
#                          Pull: ollama pull granite3.1-moe:1b
#
#   granite3-dense:2b   — 2B dense model, ~1.5 GB RAM, ~3-5 tok/s on Pi 4
#                          Better diagnostic quality, needs Pi 4 with 8 GB RAM.
#                          Pull: ollama pull granite3-dense:2b
#
#   granite3-dense:8b   — DO NOT USE on Pi 4. Needs 6+ GB RAM and runs at
#                          < 1 tok/s — too slow for real-time use.
#                          Save this for the final mini PC install.
#
# Recommendation: start with granite3.1-moe:1b to confirm everything works,
# then try granite3-dense:2b if you have the 8 GB Pi 4.
OLLAMA_BASE_URL = "http://localhost:11434"
LLM_MODEL = "granite3.1-moe:1b"   # Change to granite3-dense:2b on 8 GB Pi 4
LLM_TEMPERATURE = 0.2
LLM_MAX_TOKENS = 512      # Reduced from 1024 — keeps responses snappy on Pi 4
LLM_STREAM = True

# Longer interval on Pi 4 — LLM calls are slower so give more breathing room
LLM_PERIODIC_SUMMARY_INTERVAL = 60  # seconds (0 = disable)

# ── Anomaly Detection Thresholds ──────────────────────────────────────────────
THRESHOLDS = THRESHOLDS_C30_T5

# ── Output / Logging ──────────────────────────────────────────────────────────
LOG_RAW_DATA = True
LOG_DIR = "./logs"
SHOW_LIVE_DASHBOARD = False  # Disabled by default on Pi 4 — use browser dashboard
                             # instead: python main.py --no-terminal
VERBOSE = False
