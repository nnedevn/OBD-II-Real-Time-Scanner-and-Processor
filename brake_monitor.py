"""
Brake Event Monitor
====================
Detects braking events using the brake light switch signal and vehicle speed,
calculates deceleration metrics per event, and builds a persistent long-term
dataset for brake system health trend analysis.

How it works
-------------
A state machine watches two signals per OBD sample:
  - BRAKE_SWITCH  : binary pedal-pressed signal from Mode 22 (or inferred)
  - SPEED         : vehicle speed in km/h

State transitions:
  IDLE    → BRAKING   when brake switch fires OR decel > INFER_THRESHOLD_G
  BRAKING → COMPLETE  when brake switch releases AND decel < INFER_THRESHOLD_G
  COMPLETE → IDLE     after event is processed and logged

Per-event metrics captured:
  - Entry and exit speed (km/h)
  - Duration (seconds)
  - Peak and average deceleration (g)
  - Estimated stopping distance (m)
  - Whether the brake switch was confirmed or inferred from deceleration

Events are filtered: only those with peak decel > MIN_EVENT_DECEL_G are kept.
This removes gentle coasting slowdowns and focuses on genuine braking effort.

Persistence
-----------
Events are appended to brake_events.json in the log directory. This file
survives across sessions so trend analysis can span weeks or months of driving.

Trend analysis
--------------
The trend engine computes rolling averages over the last 10, 30, and 100 events
and flags declining brake efficiency for LLM analysis.
"""

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import config
from obd_reader import OBDSample

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# Only keep events where peak deceleration exceeds this (filters gentle slowdowns)
MIN_EVENT_DECEL_G = 0.25        # ~2.5 m/s² — a deliberate, purposeful stop

# Used to infer braking if no brake switch signal is available
INFER_THRESHOLD_G = 0.15        # ~1.5 m/s² sustained = probable braking

# Minimum event duration to log (filters out sensor noise blips)
MIN_EVENT_DURATION_S = 0.8

# How many samples of decel > threshold before we declare a braking event started
# (debounce — avoids false triggers on rough roads)
DEBOUNCE_SAMPLES = 2

# Persistent log file
EVENTS_FILE = Path(config.LOG_DIR) / "brake_events.json"

# Trend window sizes (number of qualifying events)
TREND_WINDOWS = {"recent": 10, "medium": 30, "long": 100}

# Flag for LLM if short-term efficiency drops this much below long-term baseline
EFFICIENCY_DROP_THRESHOLD = 0.10   # 10% decline triggers LLM analysis


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class BrakeEvent:
    """A single complete braking event with all calculated metrics."""
    timestamp: float                    # Unix timestamp of event start
    datetime_str: str                   # Human-readable datetime
    entry_speed_kmh: float              # Speed when braking began
    exit_speed_kmh: float               # Speed when braking ended
    duration_s: float                   # Total event duration in seconds
    peak_decel_g: float                 # Maximum deceleration achieved (g)
    avg_decel_g: float                  # Average deceleration over event (g)
    estimated_distance_m: float         # Estimated distance covered while braking
    switch_confirmed: bool              # True = brake switch saw it; False = inferred
    speed_samples: list[float] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("speed_samples")          # Don't persist raw sample arrays
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BrakeEvent":
        d.setdefault("speed_samples", [])
        return cls(**d)

    def summary(self) -> str:
        src = "switch" if self.switch_confirmed else "inferred"
        return (
            f"[{self.datetime_str}] "
            f"{self.entry_speed_kmh:.0f}→{self.exit_speed_kmh:.0f} km/h | "
            f"peak {self.peak_decel_g:.2f}g avg {self.avg_decel_g:.2f}g | "
            f"{self.duration_s:.1f}s | {self.estimated_distance_m:.0f}m | {src}"
        )


@dataclass
class BrakeTrend:
    """Rolling statistics across N recent events."""
    window_size: int
    event_count: int                    # How many events actually in window
    avg_peak_decel_g: float
    min_peak_decel_g: float
    max_peak_decel_g: float
    avg_entry_speed_kmh: float
    declining: bool                     # True if trend is heading down
    decline_pct: float                  # % below the next larger window's average


# ── Brake Monitor ──────────────────────────────────────────────────────────────

class BrakeMonitor:
    """
    State machine that watches each OBD sample and assembles BrakeEvents.

    Call push_sample(sample) on every OBD poll tick.
    Register callbacks with on_event(cb) to receive completed events.
    Register callbacks with on_alert(cb) to receive trend degradation alerts.
    """

    # Internal states
    _IDLE = "idle"
    _ARMED = "armed"        # Debounce period — collecting evidence
    _BRAKING = "braking"
    _COOLDOWN = "cooldown"  # Brief settle after event ends

    def __init__(self):
        self._state = self._IDLE
        self._event_start_time: float = 0
        self._event_start_speed: float = 0
        self._debounce_count: int = 0
        self._speed_samples: list[float] = []
        self._decel_samples: list[float] = []
        self._prev_speed: Optional[float] = None
        self._prev_ts: Optional[float] = None
        self._cooldown_count: int = 0

        self._event_callbacks: list[Callable[[BrakeEvent], None]] = []
        self._alert_callbacks: list[Callable[[str, BrakeTrend, BrakeTrend], None]] = []

        # In-memory event log (loaded from disk + new events this session)
        self._events: list[BrakeEvent] = []
        self._load_events()

        # Track whether brake switch Mode 22 is available
        self._switch_available: bool = False
        self._last_switch_state: bool = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def on_event(self, callback: Callable[[BrakeEvent], None]):
        """Register a callback that receives each completed BrakeEvent."""
        self._event_callbacks.append(callback)

    def on_alert(self, callback: Callable[[str, BrakeTrend, BrakeTrend], None]):
        """Register a callback for brake efficiency decline alerts."""
        self._alert_callbacks.append(callback)

    def notify_switch_available(self, available: bool):
        """Called by obd_reader after Mode 22 probe — tells us if switch works."""
        self._switch_available = available
        if available:
            logger.info("Brake switch Mode 22 confirmed — using switch for event detection.")
        else:
            logger.info("Brake switch Mode 22 unavailable — inferring events from deceleration.")

    def push_sample(self, sample: OBDSample):
        """Process one OBD sample through the state machine."""
        speed = sample.values.get("SPEED")
        ts = sample.timestamp

        # Read brake switch if available (value 1.0 = pressed, 0.0 = released)
        brake_switch = False
        if self._switch_available:
            raw = sample.values.get("VOLVO_BRAKE_SWITCH")
            if raw is not None:
                brake_switch = bool(int(raw))
                self._last_switch_state = brake_switch

        if speed is None:
            return

        speed = float(speed)

        # Calculate instantaneous deceleration (g) from speed delta
        decel_g = 0.0
        if self._prev_speed is not None and self._prev_ts is not None:
            dt = ts - self._prev_ts
            if dt > 0:
                delta_v_ms = (self._prev_speed - speed) / 3.6  # km/h → m/s
                decel_ms2 = delta_v_ms / dt
                decel_g = decel_ms2 / 9.81

        self._prev_speed = speed
        self._prev_ts = ts

        # Run state machine
        braking_signal = brake_switch or (decel_g > INFER_THRESHOLD_G and speed > 3)

        if self._state == self._IDLE:
            if braking_signal:
                self._debounce_count += 1
                if self._debounce_count >= DEBOUNCE_SAMPLES:
                    self._state = self._BRAKING
                    self._event_start_time = ts - (DEBOUNCE_SAMPLES * config.POLL_INTERVAL_SECONDS)
                    self._event_start_speed = self._prev_speed or speed
                    self._speed_samples = [speed]
                    self._decel_samples = [decel_g]
                    logger.debug(f"Braking event started at {speed:.0f} km/h")
            else:
                self._debounce_count = 0

        elif self._state == self._BRAKING:
            self._speed_samples.append(speed)
            self._decel_samples.append(max(0, decel_g))

            if not braking_signal or speed < 1.0:
                # Event is ending
                self._state = self._COOLDOWN
                self._cooldown_count = 0
                self._finalise_event(
                    end_time=ts,
                    exit_speed=speed,
                    switch_confirmed=self._switch_available and self._last_switch_state,
                )

        elif self._state == self._COOLDOWN:
            self._cooldown_count += 1
            if self._cooldown_count >= 2:
                self._state = self._IDLE
                self._debounce_count = 0

    def events(self, n: Optional[int] = None) -> list[BrakeEvent]:
        """Return the last n events (all events if n is None)."""
        return self._events[-n:] if n else list(self._events)

    def trend(self, window: int) -> Optional[BrakeTrend]:
        """Compute rolling statistics for the last `window` qualifying events."""
        qualifying = [e for e in self._events if e.peak_decel_g >= MIN_EVENT_DECEL_G]
        subset = qualifying[-window:]
        if len(subset) < 3:
            return None
        peak_decels = [e.peak_decel_g for e in subset]
        return BrakeTrend(
            window_size=window,
            event_count=len(subset),
            avg_peak_decel_g=round(sum(peak_decels) / len(peak_decels), 3),
            min_peak_decel_g=round(min(peak_decels), 3),
            max_peak_decel_g=round(max(peak_decels), 3),
            avg_entry_speed_kmh=round(
                sum(e.entry_speed_kmh for e in subset) / len(subset), 1
            ),
            declining=False,    # Updated below
            decline_pct=0.0,
        )

    def trends(self) -> dict[str, Optional[BrakeTrend]]:
        """Return all three window trends, with decline flags populated."""
        result = {
            name: self.trend(size) for name, size in TREND_WINDOWS.items()
        }

        # Populate declining / decline_pct relative to next larger window
        windows = list(TREND_WINDOWS.items())   # [("recent",10), ("medium",30), ("long",100)]
        for i, (name, _) in enumerate(windows[:-1]):
            t_short = result[name]
            t_long  = result[windows[i + 1][0]]
            if t_short and t_long and t_long.avg_peak_decel_g > 0:
                drop = (t_long.avg_peak_decel_g - t_short.avg_peak_decel_g) / t_long.avg_peak_decel_g
                result[name].decline_pct = round(drop * 100, 1)
                result[name].declining = drop > EFFICIENCY_DROP_THRESHOLD

        return result

    def format_for_llm(self) -> str:
        """Format trend data as a structured text block for Granite."""
        t = self.trends()
        lines = ["=== Brake System Efficiency Report ==="]

        for name, trend in t.items():
            if trend is None:
                lines.append(f"{name.capitalize()} ({TREND_WINDOWS[name]} events): insufficient data")
                continue
            flag = ""
            if trend.declining:
                flag = f"  ⚠️  DOWN {trend.decline_pct:.1f}% vs longer baseline"
            lines.append(
                f"{name.capitalize()} ({trend.event_count} events): "
                f"avg peak {trend.avg_peak_decel_g:.2f}g | "
                f"range {trend.min_peak_decel_g:.2f}–{trend.max_peak_decel_g:.2f}g | "
                f"avg entry {trend.avg_entry_speed_kmh:.0f} km/h"
                f"{flag}"
            )

        # Last 5 events detail
        recent = self.events(5)
        if recent:
            lines.append("\nLast 5 braking events:")
            for e in reversed(recent):
                lines.append(f"  {e.summary()}")

        src = "brake switch confirmed" if self._switch_available else "inferred from deceleration"
        lines.append(f"\nDetection method: {src}")
        lines.append(f"Total events logged (all sessions): {len(self._events)}")
        return "\n".join(lines)

    def dashboard_stats(self) -> dict:
        """Compact stats dict for the browser dashboard widget."""
        t = self.trends()
        recent = t.get("recent")
        medium = t.get("medium")
        return {
            "total_events": len(self._events),
            "recent_avg_g": recent.avg_peak_decel_g if recent else None,
            "medium_avg_g": medium.avg_peak_decel_g if medium else None,
            "declining": recent.declining if recent else False,
            "decline_pct": recent.decline_pct if recent else 0.0,
            "switch_confirmed": self._switch_available,
            "last_event": self._events[-1].to_dict() if self._events else None,
        }

    # ── Internal ───────────────────────────────────────────────────────────────

    def _finalise_event(self, end_time: float, exit_speed: float, switch_confirmed: bool):
        duration = end_time - self._event_start_time
        if duration < MIN_EVENT_DURATION_S:
            logger.debug(f"Braking event too short ({duration:.1f}s) — discarded")
            self._speed_samples = []
            self._decel_samples = []
            return

        # Calculate metrics
        valid_decels = [d for d in self._decel_samples if d > 0]
        peak_decel = max(valid_decels) if valid_decels else 0.0
        avg_decel = sum(valid_decels) / len(valid_decels) if valid_decels else 0.0

        if peak_decel < MIN_EVENT_DECEL_G:
            logger.debug(f"Braking event peak {peak_decel:.2f}g below threshold — discarded")
            self._speed_samples = []
            self._decel_samples = []
            return

        # Estimated distance: average speed × duration
        avg_speed_ms = (self._event_start_speed + exit_speed) / 2 / 3.6
        distance = avg_speed_ms * duration

        event = BrakeEvent(
            timestamp=self._event_start_time,
            datetime_str=datetime.fromtimestamp(self._event_start_time).isoformat(),
            entry_speed_kmh=round(self._event_start_speed, 1),
            exit_speed_kmh=round(exit_speed, 1),
            duration_s=round(duration, 2),
            peak_decel_g=round(peak_decel, 3),
            avg_decel_g=round(avg_decel, 3),
            estimated_distance_m=round(distance, 1),
            switch_confirmed=switch_confirmed,
            speed_samples=[],
        )

        self._events.append(event)
        self._persist_event(event)
        logger.info(event.summary())

        for cb in self._event_callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Brake event callback error: {e}")

        self._check_trend_alert()
        self._speed_samples = []
        self._decel_samples = []

    def _check_trend_alert(self):
        """Fire alert callbacks if short-term efficiency has dropped significantly."""
        t = self.trends()
        recent = t.get("recent")
        medium = t.get("medium")
        if recent and medium and recent.declining:
            for cb in self._alert_callbacks:
                try:
                    cb(
                        f"Brake efficiency down {recent.decline_pct:.1f}% "
                        f"(recent avg {recent.avg_peak_decel_g:.2f}g vs "
                        f"baseline {medium.avg_peak_decel_g:.2f}g)",
                        recent,
                        medium,
                    )
                except Exception as e:
                    logger.error(f"Brake alert callback error: {e}")

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load_events(self):
        """Load historical events from disk on startup."""
        if not EVENTS_FILE.exists():
            return
        try:
            with open(EVENTS_FILE, "r") as f:
                raw = json.load(f)
            self._events = [BrakeEvent.from_dict(d) for d in raw]
            logger.info(f"Loaded {len(self._events)} historical brake events from {EVENTS_FILE}")
        except Exception as e:
            logger.warning(f"Could not load brake events: {e}")
            self._events = []

    def _persist_event(self, event: BrakeEvent):
        """Append a new event to the persistent JSON log."""
        try:
            os.makedirs(EVENTS_FILE.parent, exist_ok=True)
            # Load existing, append, write back
            existing = []
            if EVENTS_FILE.exists():
                with open(EVENTS_FILE, "r") as f:
                    existing = json.load(f)
            existing.append(event.to_dict())
            # Cap file at 1000 events to prevent unbounded growth
            if len(existing) > 1000:
                existing = existing[-1000:]
            with open(EVENTS_FILE, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to persist brake event: {e}")
