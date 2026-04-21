"""
Hardware Profile: Raspberry Pi 3B+
====================================
  Adapter:  OBDLink MX+ (Bluetooth, STN2120 chipset)
  Computer: Raspberry Pi 3B+ — ARM Cortex-A53 @ 1.4 GHz, 1 GB RAM
  Vehicle:  2012 Volvo C30 T5 (B5254T2)

Design priorities
-----------------
  1. Connection stability over throughput — the Pi 3B+ is slow enough that
     OBD_FAST timing and Mode 22 probing can destabilise a fresh BT link.
     Start with the safe defaults here and only loosen them once confirmed stable.

  2. RAM headroom — OS (~250 MB) + granite4:350m (~250 MB) + Python (~150 MB)
     ≈ 650 MB out of 1 GB. Keep Mode 22 off initially to avoid extra probe traffic.

  3. Bluetooth — Linux enters BT sniff mode after ~10 s, which makes the
     OBDLink MX+ timeout and reset. Before running: sudo hciconfig hci0 nsniff
     See README Step 3 for how to make that permanent.

Switching to this profile
--------------------------
  In config.py, set:   PROFILE = "pi3b"
"""

from vehicle_profile import STANDARD_PIDS, THRESHOLDS_C30_T5, VOLVO_MODE22_COMMANDS

# ── Hardware identity ─────────────────────────────────────────────────────────
HARDWARE_PROFILE = "Pi 3B+"

# ── OBD Connection ────────────────────────────────────────────────────────────
# Linux (Pi OS / Ubuntu): set to "/dev/rfcomm0"
# after: sudo rfcomm bind /dev/rfcomm0 <MAC>
OBD_PORT             = None   # Auto-detect. Override if auto-detect is unreliable.
OBD_BAUDRATE         = 38400
OBD_TIMEOUT          = 30     # Seconds for initial ECU handshake
OBD_FAST             = False  # Keep False on Pi 3B+ — tight ELM327 timing + slow
                               # CPU scheduling causes intermittent BT drops.
                               # Re-enable only after confirming a stable connection.

# ── Connection resilience ─────────────────────────────────────────────────────
OBD_RECONNECT_ATTEMPTS   = 5  # Auto-reconnect retries after a drop
OBD_RECONNECT_BASE_WAIT  = 3  # Back-off seed in seconds: 3, 6, 9, 12, 15 …

# ── Polling rates ─────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS        = 2.0   # Relaxed — reduces CPU competition with Ollama
DTC_POLL_INTERVAL_SECONDS    = 30.0  # Less frequent; DTCs don't change rapidly
MODE22_POLL_INTERVAL_SECONDS = 0     # DISABLED — enable (set to 5.0) only after
                                      # standard OBD connection is stable for > 5 min

# ── PID lists ─────────────────────────────────────────────────────────────────
MONITORED_PIDS  = STANDARD_PIDS + ["SPEED"]
MODE22_COMMANDS = VOLVO_MODE22_COMMANDS  # Inactive when interval = 0

# ── Data buffer ───────────────────────────────────────────────────────────────
BUFFER_SIZE          = 30   # 30 samples ≈ 1 min at 2 Hz polling
LLM_CONTEXT_SAMPLES  = 8    # Samples sent to LLM per analysis call (~16 s window)

# ── LLM ───────────────────────────────────────────────────────────────────────
# granite4:350m is the only viable Granite 4 choice for 1 GB RAM.
# granite4:1b (3.3 GB) and granite4:3b (2.1 GB) are both too large.
# granite4:350m delivers useful diagnostic answers and fits comfortably.
OLLAMA_BASE_URL              = "http://localhost:11434"
LLM_MODEL                    = "granite4:350m"
LLM_TEMPERATURE              = 0.2
LLM_MAX_TOKENS               = 256   # Short responses — keeps inference time under ~15s
LLM_STREAM                   = True
LLM_PERIODIC_SUMMARY_INTERVAL = 120  # seconds between auto health summaries (0 = off)

# ── Anomaly thresholds ────────────────────────────────────────────────────────
THRESHOLDS = THRESHOLDS_C30_T5

# ── Output / logging ─────────────────────────────────────────────────────────
LOG_RAW_DATA        = True
LOG_DIR             = "./logs"
SHOW_LIVE_DASHBOARD = False   # Browser dashboard is lighter — use --no-terminal
VERBOSE             = False
