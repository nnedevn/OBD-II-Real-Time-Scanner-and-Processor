"""
Hardware Profile: Fast PC (i7 / 16 GB RAM / 1.5 GB VRAM)
===========================================================
  Adapter:  OBDLink MX+ (Bluetooth, STN2120 chipset)
  Computer: Intel Core i7, 16 GB system RAM, 1.5 GB discrete VRAM
  Vehicle:  2012 Volvo C30 T5 (B5254T2)

Design priorities
-----------------
  1. Data density — poll as fast as the adapter allows, keep a full 2-minute
     rolling buffer, enable Mode 22 for Volvo-specific channels.

  2. LLM quality — granite4:3b (2.1 GB) fits easily in 16 GB RAM and brings
     a 128K context window, which means the LLM can reason over longer data
     windows than any Granite 3 model. ollama pull granite4:3b

  3. GPU note — 1.5 GB VRAM is not enough to offload granite4:3b, so Ollama
     runs it on CPU. An i7 typically delivers 15–30 tokens/sec for the 3b model
     on CPU — fast enough that streaming feels immediate.
     If you later add a GPU with 4+ GB VRAM, Ollama will use it automatically.

  4. Rich terminal dashboard — fast hardware can drive both the terminal UI
     and the browser dashboard simultaneously without any measurable impact.

Switching to this profile
--------------------------
  In config.py, set:   PROFILE = "fast"
"""

from vehicle_profile import STANDARD_PIDS, THRESHOLDS_C30_T5, VOLVO_MODE22_COMMANDS

# ── Hardware identity ─────────────────────────────────────────────────────────
HARDWARE_PROFILE = "Fast PC (i7/16GB)"

# ── OBD Connection ────────────────────────────────────────────────────────────
# Linux (Ubuntu 24.04): set to "/dev/rfcomm0"
# after: sudo rfcomm bind /dev/rfcomm0 <MAC>
OBD_PORT             = None   # Auto-detect. Override with the actual port if needed.
OBD_BAUDRATE         = 38400  # Reliable over BT SPP. Try 115200 for raw speed tests.
OBD_TIMEOUT          = 30
OBD_FAST             = True   # STN2120 fully supports fast init. CPU is not the
                               # bottleneck here, so tight ELM327 timing is safe.

# ── Connection resilience ─────────────────────────────────────────────────────
OBD_RECONNECT_ATTEMPTS   = 5
OBD_RECONNECT_BASE_WAIT  = 3

# ── Polling rates ─────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS        = 1.0   # Full 1 Hz — the adapter is the bottleneck, not the CPU
DTC_POLL_INTERVAL_SECONDS    = 15.0  # Check for new fault codes every 15 s
MODE22_POLL_INTERVAL_SECONDS = 2.0   # Volvo-specific channels at 0.5 Hz

# ── PID lists ─────────────────────────────────────────────────────────────────
MONITORED_PIDS  = STANDARD_PIDS + ["SPEED"]
MODE22_COMMANDS = VOLVO_MODE22_COMMANDS   # Full Volvo Mode 22 suite enabled

# ── Data buffer ───────────────────────────────────────────────────────────────
BUFFER_SIZE          = 120   # 120 samples = 2 min at 1 Hz — gives LLM good trend data
LLM_CONTEXT_SAMPLES  = 20    # 20-second window per analysis call

# ── LLM ───────────────────────────────────────────────────────────────────────
# granite4:3b is the right choice here:
#   - 2.1 GB RAM — trivially small for a 16 GB machine
#   - 128K context window — far larger than any Granite 3 model, allowing
#     the LLM to reason over a much longer sensor history if needed
#   - Typical speed on an i7 CPU: 15–30 tokens/sec — feels immediate
#   - Same model as granite4:latest
#
# Note: granite4:1b (3.3 GB) is paradoxically larger than granite4:3b (2.1 GB)
# due to higher-precision quantization, and offers no quality advantage here.
# Stick with granite4:3b.
OLLAMA_BASE_URL              = "http://localhost:11434"
LLM_MODEL                    = "granite4:3b"
LLM_TEMPERATURE              = 0.2
LLM_MAX_TOKENS               = 1024   # Full-length responses — detailed repair guides
LLM_STREAM                   = True
LLM_PERIODIC_SUMMARY_INTERVAL = 30    # Summarise every 30 s — fast enough to keep up

# ── Anomaly thresholds ────────────────────────────────────────────────────────
THRESHOLDS = THRESHOLDS_C30_T5

# ── Output / logging ─────────────────────────────────────────────────────────
LOG_RAW_DATA        = True
LOG_DIR             = "./logs"
SHOW_LIVE_DASHBOARD = True   # Rich terminal dashboard + browser dashboard simultaneously
VERBOSE             = False
