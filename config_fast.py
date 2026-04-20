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

  2. LLM quality — 16 GB RAM is plenty for granite3-dense:8b on CPU. The 8B
     model gives substantially better diagnostic reasoning than the small models.

  3. GPU note — 1.5 GB VRAM is not enough to offload a useful portion of the
     8B model, so Ollama will run it on CPU. An i7 typically delivers 5–12
     tokens/sec for the 8B model on CPU, which is fine for streaming output.
     If you later upgrade to 8+ GB VRAM, Ollama will use the GPU automatically
     and inference will be ~3–5x faster — no config change needed.

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
# Linux: set to "/dev/rfcomm0" after: sudo rfcomm bind /dev/rfcomm0 <MAC>
# macOS: set to the path shown by:  ls /dev/cu.* | grep -i obd
# Windows: set to "COM3" (or whichever COM port the adapter is assigned)
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
# granite3-dense:8b is the top-tier local diagnostic model. It understands
# Volvo-specific fault patterns, gives detailed repair procedures, and reasons
# well about multi-PID correlations (e.g. boost + MAF + fuel trim together).
#
# With 16 GB RAM, this model runs comfortably on CPU. Typical speed on an i7:
#   ~5–12 tokens/sec (stream feels responsive at 5+ tok/s)
#
# If you want faster responses at some quality cost, use granite3.1-moe:3b
# (~2 GB RAM, ~15–25 tok/s on i7 CPU).
OLLAMA_BASE_URL              = "http://localhost:11434"
LLM_MODEL                    = "granite3-dense:8b"
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
