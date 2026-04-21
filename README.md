# OBD II Real-Time Scanner + IBM Granite LLM
### 2012 Volvo C30 T5 — B5254T2 2.5L Inline-5 Turbo

Real-time engine diagnostics powered by a local IBM Granite LLM. Reads live OBD-II data over Bluetooth, detects anomalies, interprets fault codes, generates repair guides, and displays everything on a touch-friendly browser dashboard.

---

## Hardware

| Component | What's being used |
|---|---|
| OBD-II Adapter | **OBDLink MX+** (Bluetooth, STN2120 chipset) |
| Prototype computer | **Raspberry Pi 3B+** (1 GB RAM) |
| Final computer | **i7 / 16 GB RAM desktop or small-form-factor PC** |
| OS (Pi 3B+) | **Raspberry Pi OS 64-bit** (Bookworm) |
| OS (final) | **Ubuntu 24.04 LTS** |
| Touchscreen | Any HDMI/USB display, 7"–10" IPS |

> The Pi 3B+ runs `granite4:350m` — a 350M-parameter model that fits in 1 GB RAM.
> The Ubuntu machine runs `granite4:3b` for full diagnostic quality.
> Switch between them by changing one line in `config.py` — see Configuration Reference.

---

## Raspberry Pi 3B+ Setup

The Pi 3B+ has 1 GB of RAM, which is tight but workable with the 350M model. Keep the Pi dedicated to this task — don't run other services alongside the scanner.

### Step 1 — Flash Raspberry Pi OS 64-bit

Download the Raspberry Pi Imager from [raspberrypi.com/software](https://www.raspberrypi.com/software/) and flash **Raspberry Pi OS (64-bit) — Bookworm** to a microSD card (32 GB minimum; use a class A1 or A2 card for better random I/O).

In the Imager's advanced settings, pre-configure Wi-Fi credentials, hostname, and enable SSH.

### Step 2 — First boot, update, and enable swap

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-dev git
```

The Pi 3B+ has 1 GB RAM. A 512 MB swap file prevents OOM kills when RAM gets tight:

```bash
sudo dphys-swapfile swapoff
sudo nano /etc/dphys-swapfile   # Change CONF_SWAPSIZE=100 to CONF_SWAPSIZE=512
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```

### Step 3 — Fix Bluetooth power management (critical on Pi 3B+)

This is the most important step for a stable OBD connection. Linux's Bluetooth stack enters **sniff mode** after ~5–10 seconds of low activity, which causes the OBDLink MX+ to treat the silence as an idle timeout and reset. Disabling sniff mode stops this entirely.

```bash
sudo apt install -y bluetooth bluez rfkill
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
```

Disable sniff mode immediately (test that it holds the connection):
```bash
sudo hciconfig hci0 nsniff
```

Make it permanent by creating a udev rule:
```bash
sudo nano /etc/udev/rules.d/10-bt-nsniff.rules
```
Add this single line:
```
ACTION=="add", KERNEL=="hci[0-9]*", RUN+="/bin/hciconfig %k nsniff"
```

Reload udev:
```bash
sudo udevadm control --reload-rules
```

Keep the Bluetooth radio enabled at boot — edit `/etc/bluetooth/main.conf` and ensure the `[Policy]` section contains:
```ini
[Policy]
AutoEnable=true
```

### Step 4 — Install Chromium

Chromium is included in the full desktop image. On the Lite image:

```bash
sudo apt install -y chromium-browser
```

### Step 5 — Install Ollama (ARM64)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama list   # Verify it's running
```

### Step 6 — Pull the Granite 350M model

```bash
ollama pull granite4:350m
ollama run granite4:350m "What does high coolant temp mean?"   # Verify
```

`config.py` is already set to `PROFILE = "pi3b"` which uses `granite4:350m`.

> **Cooling note:** A basic heatsink on the CPU is enough for this model size. An active fan is not required but helps if the console area gets warm.

### Step 7 — Install Python dependencies

```bash
cd ~/obd-scanner
pip install -r requirements.txt --break-system-packages
```

### Step 8 — Power supply for the Pi 3B+ in the car

The Pi 3B+ requires **5V / 2.5A (12.5W) via micro-USB** (micro-USB, not USB-C like the Pi 4).

Tap fuse slot 45 (ignition-switched, 20A) in the interior fuse box under the glove compartment using an **add-a-fuse adapter**. Wire to a quality **12V → 5V car power module** rated at least 2.5A.

> The Pi 3B+ has no hardware shutdown signal. Enable **Overlay FS** to protect the SD card from sudden power cuts:
> `sudo raspi-config` → **Performance Options → Overlay File System → Enable**

---

## Ubuntu 24.04 LTS Setup

These steps target the final-install machine — an i7 / 16 GB RAM desktop or SFF PC running **Ubuntu 24.04 LTS (Noble Numbat)**. This is a full-power setup using `granite4:3b`.

### Step 1 — Fresh install and update

Download Ubuntu 24.04 LTS from [ubuntu.com/download](https://ubuntu.com/download/desktop) and perform a standard installation. After first boot:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git curl python3 python3-pip python3-venv python3-dev build-essential
```

### Step 2 — Install Bluetooth tools

```bash
sudo apt install -y bluetooth bluez bluez-tools rfkill
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
```

Add your user to the `dialout` group so the scanner can access the serial port without sudo:

```bash
sudo usermod -a -G dialout $USER
```

**Log out and back in** (or reboot) for the group change to take effect. If you skip this, you will get `Permission denied` errors when the scanner tries to open `/dev/rfcomm0`.

> **BT sniff mode on Ubuntu:** The same sniff-mode issue that affects Pi 3B+ can also occur on Ubuntu desktop machines. If the connection drops after a few seconds, apply the same fix:
> ```bash
> sudo hciconfig hci0 nsniff
> ```
> And create the same udev rule as described in the Pi 3B+ Step 3 above.

### Step 3 — Install Chromium

```bash
sudo apt install -y chromium-browser
```

On Ubuntu 24.04 this installs the Chromium snap. Alternatively, install directly via snap:

```bash
sudo snap install chromium
```

Either way, the `chromium-browser` command will work.

### Step 4 — Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Ollama installs as a systemd service and starts automatically. Verify:

```bash
systemctl status ollama
ollama list
```

### Step 5 — Pull the Granite 8B model

```bash
ollama pull granite4:3b
```

This downloads approximately 2.1 GB. Verify it runs:

```bash
ollama run granite4:3b "Diagnose P0171 on a turbocharged 5-cylinder engine."
```

Expected speed on an i7 with no GPU offload: **15–30 tokens/second** — fast enough that streaming feels immediate. `granite4:3b` is the same model as `granite4:latest`.

> **GPU note:** Your machine has 1.5 GB of VRAM, which is not enough to offload `granite4:3b`. Ollama will use CPU inference automatically. If you later add a GPU with 4+ GB VRAM, Ollama will use it without any config change needed.

> **Why not granite4:1b?** Despite the name, `granite4:1b` is 3.3 GB on disk — larger than `granite4:3b` at 2.1 GB due to higher-precision quantization. `granite4:3b` offers better capability at a smaller footprint, so it's the right pick here.

### Step 6 — Set up a Python virtual environment

Ubuntu 24.04 enforces PEP 668 — direct `pip install` into the system Python is blocked. Use a virtual environment (recommended) or pass `--break-system-packages` (less clean).

**Recommended — virtual environment:**
```bash
cd ~/obd-scanner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Add the activate line to your shell profile so it's active in new terminals:
```bash
echo 'source ~/obd-scanner/.venv/bin/activate' >> ~/.bashrc
```

**Alternative — system install:**
```bash
pip install -r requirements.txt --break-system-packages
```

### Step 7 — Set the config profile

In `config.py`, change the profile to `"fast"`:

```python
PROFILE = "fast"   # Uses granite4:3b, 1Hz polling, Mode 22 enabled
```

---

## Pairing the OBDLink MX+ (Linux)

These steps are the same on both Pi 3B+ (Raspberry Pi OS) and Ubuntu 24.04.

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

> **OBDLink MX+ tip:** The adapter uses Bluetooth Classic SPP, not BLE. If `scan on` doesn't find it, check the LED — a flashing blue means ready to pair; a solid blue means it's already connected to another device. Unplug briefly to reset it.

### Bind to a serial port

```bash
sudo rfcomm bind /dev/rfcomm0 AA:BB:CC:DD:EE:FF
```

### Update the active config profile

In `config_pi3b.py` or `config_fast.py` (whichever you're using), set:

```python
OBD_PORT = "/dev/rfcomm0"
```

### Persist the binding across reboots

On both Pi OS and Ubuntu, add the bind to the systemd service file (see Auto-Start below) using `ExecStartPre`. This is more reliable than `/etc/rc.local`.

---

## Running the Scanner

### Browser dashboard only

```bash
python3 main.py --no-terminal
```

Then open in kiosk mode:
```bash
chromium-browser --kiosk --app=http://localhost:8080 --disable-infobars --noerrdialogs
```

### Full mode (terminal UI + browser dashboard)

```bash
python3 main.py
```

> On the fast profile, `SHOW_LIVE_DASHBOARD = True` enables the Rich terminal UI by default alongside the browser view.

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

## Auto-Start on Boot

### Pi 3B+ — systemd service

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

The `hciconfig hci0 nsniff` line ensures BT power management is off before each connection attempt, even if the udev rule didn't fire in time.

### Ubuntu 24.04 — systemd service

Replace `your-username` and adjust the path to the project folder. If you're using a virtual environment, point `ExecStart` at the venv Python:

```bash
sudo nano /etc/systemd/system/obd-scanner.service
```

```ini
[Unit]
Description=OBD II + Granite LLM Scanner
After=network.target bluetooth.target ollama.service

[Service]
Type=simple
User=your-username
WorkingDirectory=/home/your-username/obd-scanner
ExecStartPre=/bin/bash -c 'rfcomm bind /dev/rfcomm0 AA:BB:CC:DD:EE:FF || true'
ExecStartPre=/bin/bash -c 'hciconfig hci0 nsniff || true'
ExecStart=/home/your-username/obd-scanner/.venv/bin/python3 main.py --no-terminal
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

### Kiosk browser on desktop login (both platforms)

```bash
mkdir -p ~/.config/autostart
nano ~/.config/autostart/obd-dashboard.desktop
```

```ini
[Desktop Entry]
Type=Application
Name=OBD Dashboard
Exec=bash -c 'sleep 10 && chromium-browser --kiosk --app=http://localhost:8080 --disable-infobars --noerrdialogs'
X-GNOME-Autostart-enabled=true
```

> Pi 3B+: increase the delay to `sleep 12` — the 350M model takes longer to load. Ubuntu: `sleep 8` is usually enough for the 8B model on an i7.

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

Tapping the DTC badge shows active fault codes. Tapping the Brake Efficiency card triggers a full brake health analysis.

---

## Configuration Reference

Switch hardware profiles in `config.py` by changing one line:

```python
PROFILE = "pi3b"   # Raspberry Pi 3B+ — granite4:350m, conservative settings
PROFILE = "fast"   # Ubuntu / i7 desktop — granite4:3b, full settings
```

| Setting | Pi 3B+ (`config_pi3b.py`) | Fast PC (`config_fast.py`) | Description |
|---|---|---|---|
| `OBD_PORT` | `None` (auto) | `None` (auto) | Set to `/dev/rfcomm0` after pairing |
| `OBD_FAST` | `False` | `True` | Tight ELM327 timing — only safe on fast hardware |
| `LLM_MODEL` | `granite4:350m` | `granite4:3b` | Ollama model |
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
├── config.py             # Profile selector — change PROFILE here to switch hardware
├── config_pi3b.py        # Pi 3B+ settings (granite4:350m, conservative)
├── config_fast.py        # Fast PC settings (granite4:3b, full)
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

## Troubleshooting

### OBD connection drops after a few seconds

Almost always caused by **Bluetooth power management (sniff mode)**, not the CPU. The kernel lets the BT radio sleep after ~5–10 seconds of low activity, and the OBDLink MX+ interprets the resulting silence as an idle timeout.

Fix in order of likelihood:

**1. Disable BT sniff mode (test this first — applies to both Pi and Ubuntu):**
```bash
sudo hciconfig hci0 nsniff
```
If the connection holds, sniff mode was the cause. Make it permanent using the udev rule in Pi Step 3 / Ubuntu Step 2.

**2. Disable Mode 22 probing temporarily:**
```python
# In the active config profile file:
MODE22_POLL_INTERVAL_SECONDS = 0
```
The startup probe sends 10+ blocking serial commands. On slower hardware this can stall the poll loop long enough for the adapter to time out.

**3. Confirm `OBD_FAST = False` (Pi 3B+ only):**
Fast init uses tight ELM327 timing. Pi 3B+ CPU scheduling jitter can cause timing failures that look like disconnects.

**4. Check for memory pressure (Pi 3B+ only):**
```bash
free -h
```
If available RAM is under ~100 MB, enable the swap file (Pi Step 2).

**5. Auto-reconnect is active:** The OBD reader will silently retry up to 5 times on any drop. Watch the log — if you see "Reconnect attempt 1/5…" repeatedly, fix the sniff mode issue in step 1 above.

---

### OBDLink MX+ not appearing in bluetoothctl scan
- LED should flash blue when ready; solid blue means connected elsewhere — unplug to reset
- Ignition must be ON for the adapter to power up
- Run `sudo rfkill unblock bluetooth` before scanning

### OBDLink MX+ connects but no data
- Confirm `OBD_PORT = "/dev/rfcomm0"` in the active config profile
- Check the binding is active: `ls -la /dev/rfcomm*`
- Check serial port permissions: `ls -la /dev/rfcomm0` — your user must be in the `dialout` group (`groups $USER` to verify; add with `sudo usermod -a -G dialout $USER` then log out/in)
- Try `OBD_FAST = False` if not already set

### "Model not found" or Ollama not running
- Check: `ollama list` — if empty, pull the model first
- Pi 3B+: `ollama pull granite4:350m`
- Ubuntu: `ollama pull granite4:3b`
- If Ollama isn't running: `ollama serve` or `sudo systemctl start ollama`
- Ubuntu: check Ollama service status: `systemctl status ollama`

### Ubuntu — `pip install` fails with "externally-managed-environment"
Ubuntu 24.04 blocks direct system pip installs. Use the virtual environment approach:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
Then update the systemd service `ExecStart` to use `.venv/bin/python3`.

### Granite responses are slow
- Pi 3B+: 10–25 seconds is normal for 350M model; if longer, check `free -h` for memory pressure
- Ubuntu i7: 5–12 tokens/sec is normal for the 8B model on CPU; if much slower, check `htop` for competing processes
- Either platform: reduce `LLM_MAX_TOKENS` to `128` for faster but shorter responses
- Disable periodic summaries: `LLM_PERIODIC_SUMMARY_INTERVAL = 0`

### Dashboard not loading at http://localhost:8080
- Check scanner is running: `sudo systemctl status obd-scanner`
- Check for port conflicts: `sudo ss -tlnp | grep 8080`
- Try a different port: `python3 main.py --port 9090`
- Ubuntu with UFW enabled: `sudo ufw allow 8080/tcp` (only needed if accessing from another device)

### Mode 22 PIDs all showing `--`
- Expected — the probe tests each address and silently skips non-responders
- Enable Mode 22 only after standard OBD is confirmed stable for several minutes: set `MODE22_POLL_INTERVAL_SECONDS = 5.0`

### SD card corruption after power cut (Pi 3B+ only)
- Enable Overlay FS: `sudo raspi-config` → **Performance Options → Overlay File System → Enable**
- Or use a UPS HAT for a clean shutdown when 12V drops
