"""
OBD II Real-Time Data Reader
=============================
Connects to an ELM327-compatible Bluetooth OBD adapter, polls a configurable
set of PIDs at a fixed interval, and emits each sample as a dict to any
registered subscriber callbacks.

Usage (standalone test):
    python obd_reader.py

Architecture:
    OBDReader
      └─ connect()          — pair and open serial connection
      └─ start_stream()     — begin async polling loop
      └─ stop()             — graceful shutdown
      └─ subscribe(cb)      — register a callback for new data samples
      └─ subscribe_dtc(cb)  — register a callback for DTC changes
"""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Optional

import obd
from obd import OBDCommand

import config
from vehicle_profile import compute_derived

logger = logging.getLogger(__name__)


# ── Data structures ───────────────────────────────────────────────────────────

class OBDSample:
    """A single timestamped snapshot of all polled PIDs."""

    def __init__(self, values: dict[str, Any], dtcs: list[str]):
        self.timestamp: float = time.time()
        self.datetime_str: str = datetime.fromtimestamp(self.timestamp).isoformat()
        self.values: dict[str, Any] = values   # {pid_name: numeric_value_or_None}
        self.dtcs: list[str] = dtcs            # Active DTC codes e.g. ["P0300", "P0171"]
        self.units: dict[str, str] = {}        # {pid_name: "°C"} etc — populated by reader

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "datetime": self.datetime_str,
            "values": self.values,
            "units": self.units,
            "dtcs": self.dtcs,
        }

    def summary_text(self) -> str:
        """Human-readable one-liner for logging."""
        parts = [f"{k}={v}{self.units.get(k,'')}" for k, v in self.values.items() if v is not None]
        dtc_str = f" | DTCs: {', '.join(self.dtcs)}" if self.dtcs else ""
        return f"[{self.datetime_str}] {' | '.join(parts)}{dtc_str}"


# ── OBD Reader ────────────────────────────────────────────────────────────────

class OBDReader:
    """
    Async wrapper around python-OBD.

    The main loop runs as an asyncio task. On each tick it:
      1. Queries all configured PIDs
      2. Packages results into an OBDSample
      3. Calls all registered data subscribers
      4. Periodically queries for DTCs and calls DTC subscribers if changed
    """

    def __init__(self):
        self._connection: Optional[obd.OBD] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._dtc_task: Optional[asyncio.Task] = None

        # Registered callbacks
        self._data_subscribers: list[Callable[[OBDSample], None]] = []
        self._dtc_subscribers: list[Callable[[list[str], list[str]], None]] = []

        # Track previous DTC state to detect changes
        self._last_dtcs: set[str] = set()

        # Cache supported commands after first connection
        self._supported_pids: list[str] = []
        # Mode 22 custom commands that the ECU responds to
        self._supported_mode22: list[OBDCommand] = []
        self._mode22_task: Optional[asyncio.Task] = None

        # Shared state for Mode 22 values (written by mode22 loop, read by poll)
        import threading
        self._mode22_lock = threading.Lock()
        self._mode22_values: dict[str, Any] = {}
        self._mode22_units: dict[str, str] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def subscribe(self, callback: Callable[[OBDSample], None]):
        """Register a callback that receives every new OBDSample."""
        self._data_subscribers.append(callback)

    def subscribe_dtc(self, callback: Callable[[list[str], list[str]], None]):
        """
        Register a callback for DTC changes.
        Called with (new_dtcs, cleared_dtcs) when the DTC list changes.
        """
        self._dtc_subscribers.append(callback)

    def connect(self) -> bool:
        """
        Open the OBD connection. Returns True on success.
        Tries auto-detection if OBD_PORT is None.
        """
        port = config.OBD_PORT
        logger.info(f"Connecting to OBD adapter on port: {port or 'auto-detect'}")

        try:
            self._connection = obd.OBD(
                portstr=port,
                baudrate=config.OBD_BAUDRATE,
                timeout=config.OBD_TIMEOUT,
                fast=config.OBD_FAST,
            )
        except Exception as e:
            logger.error(f"OBD connection failed: {e}")
            return False

        if not self._connection.is_connected():
            logger.error("OBD adapter found but vehicle ECU not responding. "
                         "Is the ignition on?")
            return False

        logger.info(f"Connected! Protocol: {self._connection.protocol_name()}")

        # Discover which configured PIDs the vehicle actually supports
        self._supported_pids = self._discover_supported_pids()
        logger.info(f"Supported standard PIDs ({len(self._supported_pids)}): "
                    f"{', '.join(self._supported_pids)}")

        # Probe Mode 22 custom commands
        if config.MODE22_COMMANDS and config.MODE22_POLL_INTERVAL_SECONDS > 0:
            self._supported_mode22 = self._probe_mode22_commands()
            logger.info(
                f"Supported Mode 22 PIDs ({len(self._supported_mode22)}): "
                + ", ".join(c.name for c in self._supported_mode22)
            )
        else:
            logger.info("Mode 22 polling disabled.")

        return True

    async def start_stream(self):
        """Start the async polling loop. Call after connect()."""
        if not self._connection or not self._connection.is_connected():
            raise RuntimeError("Not connected. Call connect() first.")
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        self._dtc_task = asyncio.create_task(self._dtc_loop())
        if self._supported_mode22:
            self._mode22_task = asyncio.create_task(self._mode22_loop())
        else:
            self._mode22_task = None
        logger.info("OBD data stream started.")

    async def stop(self):
        """Gracefully stop streaming and close the connection."""
        self._running = False
        for task in (self._task, self._dtc_task, self._mode22_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._connection:
            self._connection.close()
        logger.info("OBD reader stopped.")

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and self._connection.is_connected()

    @property
    def supported_pids(self) -> list[str]:
        return list(self._supported_pids)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _discover_supported_pids(self) -> list[str]:
        """Filter config.MONITORED_PIDS down to those the ECU actually supports."""
        supported = []
        for pid_name in config.MONITORED_PIDS:
            cmd = getattr(obd.commands, pid_name, None)
            if cmd is None:
                logger.warning(f"Unknown PID '{pid_name}' in config — skipping.")
                continue
            if cmd in self._connection.supported_commands:
                supported.append(pid_name)
            else:
                logger.debug(f"PID '{pid_name}' not supported by this vehicle — skipping.")
        return supported

    def _probe_mode22_commands(self) -> list[OBDCommand]:
        """
        Test each Mode 22 command with a single query to check if the ECU responds.
        Commands that return null or error are excluded from the polling loop.
        This prevents repeated failures from slowing down the poll cycle.
        """
        working = []
        for cmd in config.MODE22_COMMANDS:
            try:
                resp = self._connection.query(cmd, force=True)
                if not resp.is_null() and resp.value is not None:
                    working.append(cmd)
                    logger.info(f"Mode 22 OK: {cmd.name} = {resp.value}")
                else:
                    logger.debug(f"Mode 22 no response: {cmd.name} — skipping")
            except Exception as e:
                logger.debug(f"Mode 22 probe failed for {cmd.name}: {e}")
        return working

    async def _poll_loop(self):
        """Main PID polling coroutine."""
        while self._running:
            loop_start = asyncio.get_event_loop().time()

            sample = await asyncio.get_event_loop().run_in_executor(
                None, self._poll_once
            )

            if sample:
                for cb in self._data_subscribers:
                    try:
                        cb(sample)
                    except Exception as e:
                        logger.error(f"Data subscriber error: {e}")

            # Sleep for remainder of the poll interval
            elapsed = asyncio.get_event_loop().time() - loop_start
            sleep_time = max(0.0, config.POLL_INTERVAL_SECONDS - elapsed)
            await asyncio.sleep(sleep_time)

    def _poll_once(self) -> Optional[OBDSample]:
        """Query all supported PIDs synchronously (runs in thread pool)."""
        if not self._connection or not self._connection.is_connected():
            logger.warning("Lost OBD connection during poll.")
            return None

        values = {}
        units = {}

        for pid_name in self._supported_pids:
            cmd = getattr(obd.commands, pid_name, None)
            if cmd is None:
                continue
            try:
                response = self._connection.query(cmd)
                if not response.is_null():
                    val = response.value
                    # Extract numeric magnitude if it's a pint Quantity
                    if hasattr(val, 'magnitude'):
                        values[pid_name] = round(float(val.magnitude), 2)
                        units[pid_name] = str(val.units)
                    elif isinstance(val, (int, float)):
                        values[pid_name] = round(float(val), 2)
                    else:
                        # String values (e.g. FUEL_STATUS, OBD_COMPLIANCE)
                        values[pid_name] = str(val)
                else:
                    values[pid_name] = None
            except Exception as e:
                logger.debug(f"Error querying {pid_name}: {e}")
                values[pid_name] = None

        # Merge in the latest Mode 22 values (written by the separate Mode 22 loop)
        with self._mode22_lock:
            values.update(self._mode22_values)
            units.update(self._mode22_units)

        # Compute derived channels (boost PSI, AFR, vacuum, etc.)
        derived_vals, derived_units = compute_derived(values, units)
        values.update(derived_vals)
        units.update(derived_units)

        sample = OBDSample(values=values, dtcs=list(self._last_dtcs))
        sample.units = units
        return sample

    async def _dtc_loop(self):
        """Periodically query for DTCs and notify subscribers on changes."""
        while self._running:
            await asyncio.sleep(config.DTC_POLL_INTERVAL_SECONDS)
            try:
                current_dtcs = await asyncio.get_event_loop().run_in_executor(
                    None, self._fetch_dtcs
                )
                current_set = set(current_dtcs)
                new_dtcs = list(current_set - self._last_dtcs)
                cleared_dtcs = list(self._last_dtcs - current_set)

                if new_dtcs or cleared_dtcs:
                    self._last_dtcs = current_set
                    for cb in self._dtc_subscribers:
                        try:
                            cb(new_dtcs, cleared_dtcs)
                        except Exception as e:
                            logger.error(f"DTC subscriber error: {e}")
            except Exception as e:
                logger.error(f"DTC poll error: {e}")

    async def _mode22_loop(self):
        """
        Poll Volvo Mode 22 custom commands at a slower cadence.
        Results are stored in _mode22_values and merged into every standard sample.
        """
        while self._running:
            await asyncio.sleep(config.MODE22_POLL_INTERVAL_SECONDS)
            new_vals, new_units = await asyncio.get_event_loop().run_in_executor(
                None, self._poll_mode22_once
            )
            with self._mode22_lock:
                self._mode22_values.update(new_vals)
                self._mode22_units.update(new_units)

    def _poll_mode22_once(self) -> tuple[dict, dict]:
        """Query all working Mode 22 commands synchronously."""
        values: dict[str, Any] = {}
        units: dict[str, str] = {}
        for cmd in self._supported_mode22:
            try:
                resp = self._connection.query(cmd, force=True)
                if not resp.is_null() and resp.value is not None:
                    val = resp.value
                    if hasattr(val, 'magnitude'):
                        values[cmd.name] = round(float(val.magnitude), 2)
                        units[cmd.name] = str(val.units)
                    elif isinstance(val, (int, float)):
                        values[cmd.name] = round(float(val), 2)
                    else:
                        values[cmd.name] = val
                else:
                    values[cmd.name] = None
            except Exception as e:
                logger.debug(f"Mode 22 poll error {cmd.name}: {e}")
                values[cmd.name] = None
        return values, units

    def _fetch_dtcs(self) -> list[str]:
        """Fetch active DTCs from the ECU."""
        if not self._connection or not self._connection.is_connected():
            return []
        try:
            response = self._connection.query(obd.commands.GET_DTC)
            if response.is_null():
                return []
            # response.value is a list of (code, description) tuples
            return [str(code) for code, _ in response.value]
        except Exception as e:
            logger.error(f"Error fetching DTCs: {e}")
            return []


# ── Standalone test ───────────────────────────────────────────────────────────

async def _test_main():
    """Quick sanity check — connect, stream 10 samples, print them."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    reader = OBDReader()

    def on_sample(sample: OBDSample):
        print(sample.summary_text())

    def on_dtc(new_codes: list[str], cleared_codes: list[str]):
        if new_codes:
            print(f"🚨 NEW DTCs detected: {new_codes}")
        if cleared_codes:
            print(f"✅ DTCs cleared: {cleared_codes}")

    reader.subscribe(on_sample)
    reader.subscribe_dtc(on_dtc)

    if not reader.connect():
        print("Could not connect to OBD adapter. Check pairing and ignition.")
        return

    await reader.start_stream()

    try:
        print("Streaming OBD data (Ctrl+C to stop)...")
        await asyncio.sleep(30)  # Stream for 30 seconds in test mode
    except KeyboardInterrupt:
        pass
    finally:
        await reader.stop()


if __name__ == "__main__":
    asyncio.run(_test_main())
