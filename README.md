# OBD II Real-Time Scanner + IBM Granite LLM
### 2012 Volvo C30 T5 — B5254T2 2.5L Inline-5 Turbo

Real-time engine diagnostics powered by a local IBM Granite LLM. Reads live OBD-II data over Bluetooth, detects anomalies, interprets fault codes, generates repair guides, and displays everything on a touch-friendly browser dashboard.

---

## Hardware

| Component | What's being used |
|---|---|
| OBD-II Adapter | **OBDLink MX+** (Bluetooth, STN2120 chipset) |
| Prototype computer | **Raspberry Pi 4** (4 GB or 8 GB) |
| Final computer | **Beelink SER7** or **Minisforum UM780** (Ryzen 7840HS, 32 GB RAM) |
| OS (Pi) | **Raspberry Pi OS 64-bit** (Bookworm) |
| OS (final) | **Ubuntu 24.04 LTS** |
| Touchscreen | Any HDMI/USB display, 7"–10" |

> The Pi 4 runs the smaller `granite3.1-moe:1b` or `granite3-dense:2b` model.
> When you move to the final mini PC, switch `LLM_MODEL` in `config.py` to `granite3-dense:8b`
> for significantly better diagnostic quality.

---

## Raspberry Pi 4 Setup

### Step 1 — Flash Raspberry Pi OS 64-bit

Download the Raspberry Pi Imager from [raspberrypi.com/software](https://www.raspberrypi.com/software/) and flash **Raspberry Pi OS (64-bit) — Bookworm** to a microSD card (32 GB minimum; a USB SSD is faster and more reliable for long-term use).

In the Imager's advanced settings, pre-configure your Wi-Fi credentials, hostname, and enable SSH — this saves time during first boot.

### Step 2 — First boot and update

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-dev git
```

### Step 3 — Install Bluetooth tools

Bluetooth is pre-installed on Pi OS but confirm the service is running:

```bash
sudo apt install -y bluetooth bluez rfkill
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
```

### Step 4 — Install Chromium

Chromium is included in the full Raspberry Pi OS desktop image. If you're using the Lite image, install it manually:

```bash
sudo apt install -y chromium-browser
```

> **Note:** On Raspberry Pi OS the command is `chromium-browser`, same as Ubuntu.

### Step 5 — Install Ollama (ARM64)

Ollama supports ARM64 (Pi 4) natively via the same install script:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Verify it's running:

```bash
ollama list
```

### Step 6 — Pull the right Granite model for Pi 4

The 8B model is too large and too slow for the Pi 4. Use one of these instead:

**Pi 4 with 4 GB RAM — use the 1B MoE model (~600 MB, ~6–10 tokens/sec):**
```bash
ollama pull granite3.1-moe:1b
```

**Pi 4 with 8 GB RAM — use the 2B dense model (~1.5 GB, ~3–5 tokens/sec):**
```bash
ollama pull granite3-dense:2b
```

`config.py` is already set to `granite3.1-moe:1b`. Change `LLM_MODEL` to `granite3-dense:2b` if you're on the 8 GB board.

Verify the model works:
```bash
ollama run granite3.1-moe:1b "What does a high coolant temp reading mean?"
```

### Step 7 — Cooling

Running LLM inference on a Pi 4 generates significant heat. **Do not skip this.** Without a heatsink and fan, the Pi 4 will thermal-throttle within minutes of starting Granite.

Use a case with an active fan (e.g. the official Pi 4 case fan, or an Argon ONE case). If you're mounting it in the car's console, ensure airflow to the case — the console area gets warm on its own.

### Step 8 — Install Python dependencies

Clone or copy the project folder to the Pi, then:

```bash
cd ~/obd-scanner
pip install -r requirements.txt --break-system-packages
```

### Step 9 — Power supply for the Pi 4 in the car

The Pi 4 requires **5V / 3A (15W) via USB-C**. This is different from the mini PC setup (which needs 19V DC). A standard car USB-C charger works but quality matters — avoid cheap units that drop voltage under load, as they cause the Pi to throttle or reboot.

**Recommended approach for in-car use:**

Tap fuse slot 45 (cigarette lighter / accessory socket circuit, ignition-switched, 20A) in the interior fuse box under the glove compartment using an **add-a-fuse adapter**. Wire this to a quality **12V → 5V USB-C car power module** capable of at least 3A output. Good options are the Anker 30W USB-C car charger or the Geekworm X728 UPS HAT (adds battery-backed graceful shutdown, which is useful in a car).

> Unlike a mini PC, the Pi 4 does not support a hardware shutdown signal from a DC-DC converter. To avoid SD card corruption from sudden power cuts, configure the Pi to auto-shutdown after a brief idle period, or use a UPS HAT that provides a clean shutdown when 12V drops.

---

## Pairing the OBDLink MX+

### Find the MAC address and pair

Plug the OBDLink MX+ into the OBD-II port (under the dash, driver's side). Turn the ignition to the **ON** position.

```bash
sudo rfkill unblock bluetooth
bluetoothctl
```

Inside the `bluetoothctl` prompt:

```
scan on
```

Wait for `OBDLink MX+` to appear and note the MAC address (`AA:BB:CC:DD:EE:FF`).

```
scan off
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
connect AA:BB:CC:DD:EE:FF
quit
```

> **OBDLink MX+ tip:** The adapter uses Bluetooth Classic SPP (Serial Port Profile), not BLE. If `scan on` doesn't find it, make sure the MX+ LED is flashing blue — a solid blue means it's already connected to another device. Power-cycle the adapter by unplugging it briefly.

### Bind to a serial port

```bash
sudo rfcomm bind /dev/rfcomm0 AA:BB:CC:DD:EE:FF
```

### Update config.py

Open `config.py` and set:

```python
OBD_PORT = "/dev/rfcomm0"
```

### Persist the binding across reboots

```bash
sudo nano /etc/rc.local
```

Add before `exit 0`:

```bash
rfcomm bind /dev/rfcomm0 AA:BB:CC:DD:EE:FF
```

---

## Running the Scanner

### Browser dashboard only (recommended)

```bash
python3 main.py --no-terminal
```

Then open the dashboard:

```bash
chromium-browser --kiosk --app=http://localhost:8080 --disable-infobars --noerrdialogs
```

### Full mode (terminal UI + browser dashboard)

```bash
python3 main.py
```

### Terminal / logging only

```bash
python3 main.py --no-web
```

### Ask a one-off question

```bash
python3 main.py --ask "Is my boost pressure normal?"
python3 main.py --ask "What does P0171 mean on a Volvo T5?"
python3 main.py --ask "Why is my coolant temp higher than usual?"
```

### Change the dashboard port

```bash
python3 main.py --port 9090
```

---

## Auto-Start on Boot (Pi 4)

### Scanner service

```bash
sudo nano /etc/systemd/system/obd-scanner.service
```

```ini
[Unit]
Description=OBD II + Granite LLM Scanner
After=network.target bluetooth.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/obd-scanner
ExecStartPre=/bin/bash -c 'rfcomm bind /dev/rfcomm0 AA:BB:CC:DD:EE:FF || true'
ExecStart=/usr/bin/python3 main.py --no-terminal
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable obd-scanner
sudo systemctl start obd-scanner
```

### Kiosk browser on desktop login

```bash
mkdir -p ~/.config/autostart
nano ~/.config/autostart/obd-dashboard.desktop
```

```ini
[Desktop Entry]
Type=Application
Name=OBD Dashboard
Exec=bash -c 'sleep 8 && chromium-browser --kiosk --app=http://localhost:8080 --disable-infobars --noerrdialogs'
X-GNOME-Autostart-enabled=true
```

> The 8-second delay on Pi 4 (vs 5s on the mini PC) gives Ollama extra time to load the model on the slower hardware.

---

## Dashboard Overview

Open `http://localhost:8080` in any browser, or it fills the screen automatically in kiosk mode.

| Section | What it shows |
|---|---|
| **Header** | OBD + Granite connection status, active DTC count, clock |
| **Gauge row** | Circular gauges for RPM, Boost (PSI), Coolant Temp, Speed, Oil Temp, AFR, Throttle — yellow at warn, pulsing red at critical |
| **Graph row** | 60-second scrolling line charts for RPM, Boost, Coolant, AFR, Throttle |
| **Granite panel** | Streaming LLM analysis — anomaly alerts, DTC diagnoses, periodic health summaries |
| **Ask bar** | Type any question; Granite answers using live sensor data |

Tapping the DTC badge in the header shows all active fault codes in an overlay.

---

## Configuration Reference

All settings are in `config.py`.

| Setting | Pi 4 default | Final PC value | Description |
|---|---|---|---|
| `OBD_PORT` | `None` (auto) | `None` (auto) | Set to `"/dev/rfcomm0"` after pairing |
| `LLM_MODEL` | `granite3.1-moe:1b` | `granite3-dense:8b` | Ollama model |
| `LLM_MAX_TOKENS` | `512` | `1024` | Max tokens per response |
| `LLM_PERIODIC_SUMMARY_INTERVAL` | `60` | `30` | Seconds between health summaries |
| `POLL_INTERVAL_SECONDS` | `1.0` | `1.0` | OBD polling rate |
| `MODE22_POLL_INTERVAL_SECONDS` | `3.0` | `2.0` | Volvo Mode 22 polling rate |
| `BUFFER_SIZE` | `60` | `120` | Rolling window in samples |
| `LOG_RAW_DATA` | `True` | `True` | Write CSV logs to `./logs/` |

---

## Project File Structure

```
.
├── main.py               # Entry point — run this
├── config.py             # All settings (hardware-specific values live here)
├── vehicle_profile.py    # C30 T5 PIDs, Mode 22 commands, thresholds, LLM context
├── obd_reader.py         # Async OBD-II connection and polling loop
├── data_buffer.py        # Thread-safe rolling window + LLM context formatting
├── anomaly_detector.py   # Rule-based threshold checker (no LLM on hot path)
├── llm_interface.py      # Granite prompts + Ollama streaming client
├── dashboard_server.py   # FastAPI WebSocket server
├── dashboard.html        # Browser dashboard (gauges + graphs + LLM panel)
├── requirements.txt      # Python dependencies
└── logs/                 # Auto-created — timestamped CSV data logs
```

---

## Upgrading from Pi 4 to the Final Mini PC

When you're ready to move off the Pi, the only changes needed are:

1. Follow the standard Ubuntu 24.04 setup steps (same as Pi steps 2–4 above)
2. Pull the full model: `ollama pull granite3-dense:8b`
3. In `config.py`, update:
   ```python
   LLM_MODEL = "granite3-dense:8b"
   LLM_MAX_TOKENS = 1024
   LLM_PERIODIC_SUMMARY_INTERVAL = 30
   BUFFER_SIZE = 120
   MODE22_POLL_INTERVAL_SECONDS = 2.0
   SHOW_LIVE_DASHBOARD = True
   ```
4. Switch the power supply from USB-C (5V/3A) to the DCDC-NUC unit (12V → 19V, wired to fuse slot 45)

Everything else — the OBD adapter, Bluetooth pairing, vehicle profile, and dashboard — transfers over unchanged.

---

## Troubleshooting

**OBDLink MX+ not appearing in bluetoothctl scan**
- The MX+ LED should be flashing blue when ready to pair; solid blue means it's connected to another device — unplug and replug the adapter to reset it
- Make sure the ignition is ON; the adapter needs car power to operate
- Run `sudo rfkill unblock bluetooth` before scanning

**OBDLink MX+ connects but no data**
- Confirm `OBD_PORT = "/dev/rfcomm0"` in `config.py`
- Check the rfcomm binding is active: `ls -la /dev/rfcomm*`
- Try `OBD_FAST = False` in `config.py` as a test, then re-enable it

**"Model not found" or Ollama not running**
- Check: `ollama list` — if empty, the model hasn't been pulled
- Pull it: `ollama pull granite3.1-moe:1b`
- If Ollama isn't running: `ollama serve` (or `sudo systemctl start ollama`)

**Pi 4 is slow / Granite responses take > 30 seconds**
- This is expected for the 2B model under load; the 1B MoE model is faster
- Check CPU temperature: `vcgencmd measure_temp` — if above 80°C, improve cooling
- Disable periodic summaries: set `LLM_PERIODIC_SUMMARY_INTERVAL = 0` in `config.py`
- Reduce `LLM_MAX_TOKENS` to `256` for quicker but shorter responses

**Dashboard not loading at http://localhost:8080**
- Check the scanner is running: `sudo systemctl status obd-scanner`
- Check for port conflicts: `sudo ss -tlnp | grep 8080`
- Try a different port: `python3 main.py --port 9090`

**Mode 22 PIDs all showing `--`**
- Expected on first run — the reader probes each address and silently skips non-responders
- The `[LIKELY]` addresses in `vehicle_profile.py` need verification against a Volvo VIDA/DiCE or Autel MaxiSys to confirm
- Standard OBD-II PIDs (RPM, boost via MAP, coolant, speed, etc.) will always work regardless

**SD card corruption after power cut**
- The Pi 4 doesn't get a graceful shutdown signal when car power cuts
- Either add a UPS HAT, or configure systemd to mark the filesystem read-only at idle:
  `sudo systemctl enable --now overlayfs.service` (Pi OS has this built in under **raspi-config → Performance → Overlay FS**)
