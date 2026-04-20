# OBD II Real-Time Scanner + IBM Granite LLM
### 2012 Volvo C30 T5 — B5254T2 2.5L Inline-5 Turbo

Real-time engine diagnostics powered by a local IBM Granite LLM. Reads live OBD-II data over Bluetooth, detects anomalies, interprets fault codes, generates repair guides, and displays everything on a touch-friendly browser dashboard.

---

## Hardware

| Component | What's being used |
|---|---|
| OBD-II Adapter | **OBDLink MX+** (Bluetooth, STN2120 chipset) |
| Prototype computer | **Raspberry Pi 3B+** (1 GB RAM) |
| Final computer | **Beelink SER7** or **Minisforum UM780** (Ryzen 7840HS, 32 GB RAM) |
| OS (Pi 3B+) | **Raspberry Pi OS 64-bit** (Bookworm) |
| OS (final) | **Ubuntu 24.04 LTS** |
| Touchscreen | Any HDMI/USB display, 7"–10" IPS |

> The Pi 3B+ runs `granite4:350m` — a 350M-parameter model that fits comfortably in 1 GB RAM.
> When you move to the final mini PC, switch `LLM_MODEL` in `config.py` to `granite3-dense:8b`
> for significantly better diagnostic quality.

---

## Raspberry Pi 3B+ Setup

The Pi 3B+ has 1 GB of RAM, which is tight but workable with the 350M model. The most important constraint is that you should not run other services alongside the scanner — keep the Pi dedicated to this task.

### Step 1 — Flash Raspberry Pi OS 64-bit

Download the Raspberry Pi Imager from [raspberrypi.com/software](https://www.raspberrypi.com/software/) and flash **Raspberry Pi OS (64-bit) — Bookworm** to a microSD card (32 GB minimum; class A1 or A2 rated for better random I/O).

In the Imager's advanced settings, pre-configure your Wi-Fi credentials, hostname, and enable SSH.

### Step 2 — First boot, update, and enable swap

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-dev git
```

The Pi 3B+ has 1 GB RAM. Adding a swap file gives the kernel somewhere to put overflow and prevents OOM kills if RAM gets tight:

```bash
sudo dphys-swapfile swapoff
sudo nano /etc/dphys-swapfile   # Change CONF_SWAPSIZE=100 to CONF_SWAPSIZE=512
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```

### Step 3 — Fix Bluetooth power management (critical on Pi 3B+)

This is the most important step for connection stability. Linux's Bluetooth stack enters **sniff mode** after ~5–10 seconds, which causes the OBDLink MX+ to treat the silence as an idle timeout and reset the connection. Disabling it prevents the drops:

```bash
sudo apt install -y bluetooth bluez rfkill
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
```

Disable sniff mode immediately:
```bash
sudo hciconfig hci0 nsniff
```

Make it permanent on every boot by creating a udev rule:
```bash
sudo nano /etc/udev/rules.d/10-bt-nsniff.rules
```
Add this single line:
```
ACTION=="add", KERNEL=="hci[0-9]*", RUN+="/bin/hciconfig %k nsniff"
```

Then reload udev:
```bash
sudo udevadm control --reload-rules
```

Also edit the Bluetooth main config to keep the radio enabled at all times:
```bash
sudo nano /etc/bluetooth/main.conf
```
In the `[Policy]` section (add the section if it doesn't exist):
```ini
[Policy]
AutoEnable=true
```

### Step 4 — Install Chromium

Chromium is included in the full desktop image. If you're using the Lite image:

```bash
sudo apt install -y chromium-browser
```

### Step 5 — Install Ollama (ARM64)

Ollama supports Pi 3B+ running 64-bit OS via the standard install script:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Verify it's running:
```bash
ollama list
```

### Step 6 — Pull the Granite 350M model

```bash
ollama pull granite4:350m
```

Verify it runs:
```bash
ollama run granite4:350m "What does high coolant temp mean?"
```

`config.py` is already set to `granite4:350m`.

> **Pi 3B+ cooling note:** LLM inference generates heat even on a small model. A basic heatsink on the CPU is enough for this model size — an active fan is not required but helps if the console area gets warm.

### Step 7 — Install Python dependencies

```bash
cd ~/obd-scanner
pip install -r requirements.txt --break-system-packages
```

### Step 8 — Power supply for the Pi 3B+ in the car

The Pi 3B+ requires **5V / 2.5A (12.5W) via micro-USB** (note: micro-USB, not USB-C like the Pi 4).

Tap fuse slot 45 (ignition-switched, 20A) in the interior fuse box under the glove compartment using an **add-a-fuse adapter**. Wire to a quality **12V → 5V car power module** rated at least 2.5A. A good-quality unit is essential — cheap adapters drop voltage under load and cause the Pi to brown-out and reboot.

> As with the Pi 4, the Pi 3B+ has no hardware shutdown signal. Enable **Overlay FS** in raspi-config to protect the SD card from sudden power cuts:
> `sudo raspi-config` → **Performance Options → Overlay File System → Enable**

---

## macOS Setup (2013 MacBook Pro or later)

macOS is a convenient development and testing platform — Ollama runs natively and Bluetooth serial just works without any kernel modules or rfcomm binding.

> These steps are tested on macOS Ventura (13.x), the latest version supported on 2013 MacBook Pro.

### Step 1 — Install Homebrew and Python 3

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python@3.12
```

### Step 2 — Install Ollama for macOS

Download the macOS app from [ollama.com](https://ollama.com) and install it, or install via Homebrew:

```bash
brew install ollama
```

Start Ollama:
```bash
ollama serve &
```

Pull the model:
```bash
ollama pull granite4:350m
```

The 2013 MacBook Pro has enough RAM and CPU to run the larger models too — `granite3.1-moe:1b` or even `granite3-dense:2b` are fine if you want better diagnostic quality during development.

### Step 3 — Pair the OBDLink MX+

1. Plug the OBDLink MX+ into the OBD-II port and turn ignition to ON.
2. Open **System Settings → Bluetooth**.
3. Wait for **OBDLink MX+** to appear in the device list and click **Connect**.
4. macOS automatically creates a virtual serial port for the SPP connection — no rfcomm or driver needed.

### Step 4 — Find the serial port

```bash
ls /dev/cu.* | grep -i obd
```

The port will be something like `/dev/cu.OBDLinkMXp-SPPDev` (the exact suffix includes the adapter's serial number). Use the `cu.*` variant, not `tty.*`.

Update `config.py`:
```python
OBD_PORT = "/dev/cu.OBDLinkMXp-SPPDev"   # Replace with your actual port name
```

> **macOS Ventura Bluetooth permissions:** The first time you run the scanner, macOS will ask for Bluetooth access permission for Terminal (or whatever IDE you're using). Grant it — this is required for serial port access on Ventura and later.

### Step 5 — Install Python dependencies

```bash
cd ~/obd-scanner
pip3 install -r requirements.txt
```

No `--break-system-packages` flag is needed on macOS when using Homebrew Python.

### Step 6 — Run the scanner

```bash
python3 main.py --no-terminal
```

Open the dashboard at `http://localhost:8080` in Safari or Chrome.

> **macOS tip:** The browser dashboard is the best way to view data on macOS. The Rich terminal UI works fine too but the browser view is more polished.

---

## Pairing the OBDLink MX+ (Linux / Pi)

### Find the MAC address and pair

Plug the OBDLink MX+ into the OBD-II port. Turn the ignition to **ON**.

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

> **OBDLink MX+ tip:** The adapter uses Bluetooth Classic SPP, not BLE. If `scan on` doesn't find it, make sure the MX+ LED is flashing blue — a solid blue means it's already connected to another device. Unplug briefly to reset it.

### Bind to a serial port

```bash
sudo rfcomm bind /dev/rfcomm0 AA:BB:CC:DD:EE:FF
```

### Update config.py

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

### Browser dashboard only (recommended for Pi 3B+)

```bash
python3 main.py --no-terminal
```

Then open the dashboard in kiosk mode:

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
python3 main.py --ask "Do my brakes need bleeding?"
python3 main.py --ask "What does P0171 mean on a Volvo T5?"
```

---

## Auto-Start on Boot (Pi 3B+)

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
ExecStartPre=/bin/bash -c 'hciconfig hci0 nsniff || true'
ExecStart=/usr/bin/python3 main.py --no-terminal
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable obd-scanner
sudo systemctl start obd-scanner
```

Note the `hciconfig hci0 nsniff` ExecStartPre line — this ensures BT power management is off before the scanner tries to connect, even if the udev rule didn't fire in time.

### Kiosk browser on desktop login

```bash
mkdir -p ~/.config/autostart
nano ~/.config/autostart/obd-dashboard.desktop
```

```ini
[Desktop Entry]
Type=Application
Name=OBD Dashboard
Exec=bash -c 'sleep 12 && chromium-browser --kiosk --app=http://localhost:8080 --disable-infobars --noerrdialogs'
X-GNOME-Autostart-enabled=true
```

> The 12-second delay on Pi 3B+ gives Ollama extra time to load the 350M model on the slower hardware. Reduce to 8s if it feels too long.

---

## Dashboard Overview

Open `http://localhost:8080` in any browser, or it fills the screen in kiosk mode.

| Section | What it shows |
|---|---|
| **Header** | OBD + Granite connection status, active DTC count, clock |
| **Gauge row** | Circular gauges for RPM, Boost (PSI), Coolant Temp, Speed, Oil Temp, AFR, Throttle + Brake Efficiency card |
| **Graph row** | Scrolling line charts for RPM, Boost, Coolant, AFR, Throttle |
| **Granite panel** | Streaming LLM analysis — anomaly alerts, DTC diagnoses, periodic health summaries |
| **Ask bar** | Type any question; Granite answers using live sensor data |

Tapping the DTC badge shows active fault codes. Tapping the Brake Efficiency card requests a full brake health analysis.

---

## Configuration Reference

All settings are in `config.py`.

| Setting | Pi 3B+ default | Final PC value | Description |
|---|---|---|---|
| `OBD_PORT` | `None` (auto) | `None` (auto) | Set to `/dev/rfcomm0` (Linux) or `/dev/cu.OBD…` (macOS) |
| `OBD_FAST` | `False` | `True` | Disable on Pi 3B+ for stable BT connection |
| `LLM_MODEL` | `granite4:350m` | `granite3-dense:8b` | Ollama model |
| `LLM_MAX_TOKENS` | `256` | `1024` | Max tokens per response |
| `LLM_PERIODIC_SUMMARY_INTERVAL` | `120` | `30` | Seconds between health summaries |
| `POLL_INTERVAL_SECONDS` | `2.0` | `1.0` | OBD polling rate |
| `MODE22_POLL_INTERVAL_SECONDS` | `0` (disabled) | `2.0` | Volvo Mode 22 polling rate |
| `BUFFER_SIZE` | `30` | `120` | Rolling window in samples |
| `OBD_RECONNECT_ATTEMPTS` | `5` | `5` | Auto-reconnect retries on drop |

---

## Project File Structure

```
.
├── main.py               # Entry point — run this
├── config.py             # All settings (hardware-specific values live here)
├── vehicle_profile.py    # C30 T5 PIDs, Mode 22 commands, thresholds, LLM context
├── obd_reader.py         # Async OBD-II connection, polling loop, auto-reconnect
├── data_buffer.py        # Thread-safe rolling window + LLM context formatting
├── anomaly_detector.py   # Rule-based threshold checker (no LLM on hot path)
├── llm_interface.py      # Granite prompts + Ollama streaming client
├── brake_monitor.py      # Braking event logger + brake efficiency trend analysis
├── dashboard_server.py   # FastAPI WebSocket server
├── dashboard.html        # Browser dashboard (gauges + graphs + LLM panel)
├── requirements.txt      # Python dependencies
└── logs/                 # Auto-created — timestamped CSV data + brake_events.json
```

---

## Upgrading from Pi 3B+ to the Final Mini PC

When you're ready to move off the Pi:

1. Follow standard Ubuntu 24.04 setup (same as Pi steps 2–4, without the swap and BT power-management steps)
2. Pull the full model: `ollama pull granite3-dense:8b`
3. In `config.py`, update:
   ```python
   LLM_MODEL = "granite3-dense:8b"
   LLM_MAX_TOKENS = 1024
   LLM_PERIODIC_SUMMARY_INTERVAL = 30
   BUFFER_SIZE = 120
   MODE22_POLL_INTERVAL_SECONDS = 2.0
   POLL_INTERVAL_SECONDS = 1.0
   OBD_FAST = True
   ```
4. Switch power from micro-USB (5V/2.5A) to the DCDC-NUC unit (12V → 19V, wired to fuse slot 45)

Everything else transfers unchanged.

---

## Troubleshooting

### OBD connection drops after a few seconds (Pi 3B+)

This is the most common issue on Pi 3B+ and is almost always caused by **Bluetooth power management**, not the CPU. The Linux kernel puts the BT radio into sniff mode after ~5–10 seconds of low activity, which makes the rfcomm link go quiet. The OBDLink MX+'s STN chip interprets that silence as an idle timeout and resets.

Fix in order of likelihood:

**1. Disable BT sniff mode immediately (test this first):**
```bash
sudo hciconfig hci0 nsniff
```
If the connection holds after this, the BT power management was the cause. Follow Step 3 of the Pi 3B+ setup above to make it permanent.

**2. Disable Mode 22 probing temporarily:**
In `config.py`, set:
```python
MODE22_POLL_INTERVAL_SECONDS = 0
```
The startup probe queries 10+ serial addresses in a row. On slow hardware this can block the poll loop long enough for the adapter to timeout. Standard OBD-II PIDs still work fine without Mode 22.

**3. Confirm `OBD_FAST = False`:**
Fast init uses tight ELM327 timing. On Pi 3B+, CPU scheduling jitter can cause timing failures that look like disconnects. Keep it False until the connection is solid.

**4. Check for memory pressure:**
```bash
free -h
```
If available RAM is under ~100 MB, the kernel may be interfering with Bluetooth to free memory. This is why the swap setup in Step 2 matters.

**5. Auto-reconnect:**
The OBD reader will now attempt automatic reconnection if the link drops (up to 5 attempts with exponential back-off). Watch the log output — if you see "Reconnect attempt 1/5…" the BT fix in step 1 above will stop it from happening in the first place.

---

### OBDLink MX+ not appearing in bluetoothctl scan
- LED should flash blue when ready; solid blue means connected elsewhere — unplug to reset
- Make sure ignition is ON
- Run `sudo rfkill unblock bluetooth` before scanning

### OBDLink MX+ connects but no data
- Confirm `OBD_PORT = "/dev/rfcomm0"` in `config.py`
- Check binding: `ls -la /dev/rfcomm*`
- Try `OBD_FAST = False` if not already set

### OBDLink MX+ not appearing on macOS
- Make sure it shows as "Connected" in System Settings → Bluetooth, not just "Paired"
- Check the port exists: `ls /dev/cu.* | grep -i obd`
- If no port appears after connecting, try unpairing and re-pairing the device
- On Ventura: go to System Settings → Privacy & Security → Bluetooth and ensure Terminal has access

### "Model not found" or Ollama not running
- Check: `ollama list` — if empty, model hasn't been pulled
- Pull it: `ollama pull granite4:350m`
- If Ollama isn't running: `ollama serve` (or `sudo systemctl start ollama`)

### Pi 3B+ is slow / Granite responses take a long time
- The 350M model should respond in ~10–20 seconds on Pi 3B+; longer suggests memory pressure
- Disable periodic summaries: `LLM_PERIODIC_SUMMARY_INTERVAL = 0` in `config.py`
- Reduce `LLM_MAX_TOKENS` to `128` for faster but shorter responses
- Check CPU temperature: `vcgencmd measure_temp` — throttling starts at 80°C

### Dashboard not loading at http://localhost:8080
- Check the scanner is running: `sudo systemctl status obd-scanner`
- Check for port conflicts: `sudo ss -tlnp | grep 8080`
- Try a different port: `python3 main.py --port 9090`

### Mode 22 PIDs all showing `--`
- Expected on first run with Mode 22 enabled — the reader probes each address and skips non-responders
- Start with `MODE22_POLL_INTERVAL_SECONDS = 0` (disabled) and only enable after standard OBD is confirmed stable

### SD card corruption after power cut
- Enable Overlay FS: `sudo raspi-config` → **Performance Options → Overlay File System → Enable**
- Or use a UPS HAT for graceful shutdown when 12V drops
