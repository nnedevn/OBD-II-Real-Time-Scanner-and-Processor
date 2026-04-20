"""
Rolling Data Buffer
====================
Thread-safe circular buffer that stores the last N OBD samples.
Provides formatted context windows for LLM prompts.

The buffer is the bridge between the high-frequency OBD poll loop
and the lower-frequency LLM analysis cycle.
"""

import threading
from collections import deque
from typing import Optional

from obd_reader import OBDSample
import config


class DataBuffer:
    """
    Thread-safe rolling window of OBDSample objects.

    Attributes
    ----------
    capacity : int
        Maximum samples to retain (oldest are evicted).
    """

    def __init__(self, capacity: int = config.BUFFER_SIZE):
        self._buf: deque[OBDSample] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self.capacity = capacity

    # ── Public API ────────────────────────────────────────────────────────────

    def push(self, sample: OBDSample):
        """Add a new sample to the buffer. Called from the OBD poll callback."""
        with self._lock:
            self._buf.append(sample)

    def latest(self) -> Optional[OBDSample]:
        """Return the most recent sample, or None if empty."""
        with self._lock:
            return self._buf[-1] if self._buf else None

    def window(self, n: int = config.LLM_CONTEXT_SAMPLES) -> list[OBDSample]:
        """Return the last n samples (oldest first)."""
        with self._lock:
            samples = list(self._buf)
        return samples[-n:] if len(samples) >= n else samples

    def all(self) -> list[OBDSample]:
        with self._lock:
            return list(self._buf)

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    # ── LLM Context Formatting ────────────────────────────────────────────────

    def format_for_llm(
        self,
        n: int = config.LLM_CONTEXT_SAMPLES,
        include_dtcs: bool = True,
    ) -> str:
        """
        Format the last n samples as a compact, LLM-readable telemetry block.

        Example output:
            === OBD-II Telemetry (last 10 samples, 1s interval) ===
            Time         RPM    SPEED  COOLANT  LOAD  THROTTLE  MAF
            14:22:01     1450   55     91       42    18        12.3
            14:22:02     1480   56     91       44    19        12.6
            ...
            Active DTCs: P0300, P0171
        """
        samples = self.window(n)
        if not samples:
            return "No OBD data available yet."

        # Collect all PID keys present in the window
        all_keys: list[str] = []
        seen: set[str] = set()
        for s in samples:
            for k in s.values.keys():
                if k not in seen:
                    all_keys.append(k)
                    seen.add(k)

        # Build compact header
        interval = config.POLL_INTERVAL_SECONDS
        lines = [
            f"=== OBD-II Telemetry (last {len(samples)} samples, "
            f"{interval:.1f}s interval) ===",
        ]

        # Column headers
        col_width = 10
        header = f"{'Time':<10}" + "".join(f"{k[:col_width]:<{col_width}}" for k in all_keys)
        lines.append(header)
        lines.append("-" * len(header))

        # Data rows
        for s in samples:
            import time as _time
            from datetime import datetime
            t_str = datetime.fromtimestamp(s.timestamp).strftime("%H:%M:%S")
            row = f"{t_str:<10}"
            for k in all_keys:
                val = s.values.get(k)
                if val is None:
                    cell = "--"
                elif isinstance(val, float):
                    cell = f"{val:.1f}"
                else:
                    cell = str(val)[:col_width]
                row += f"{cell:<{col_width}}"
            lines.append(row)

        # DTC summary
        if include_dtcs:
            latest = samples[-1]
            if latest.dtcs:
                lines.append(f"\nActive DTCs: {', '.join(latest.dtcs)}")
            else:
                lines.append("\nActive DTCs: None")

        # Unit legend
        unit_parts = [
            f"{k}: {u}" for k, u in (samples[-1].units or {}).items() if k in all_keys
        ]
        if unit_parts:
            lines.append("Units: " + " | ".join(unit_parts))

        return "\n".join(lines)

    def format_latest_for_llm(self) -> str:
        """Single-sample snapshot — used for quick anomaly context."""
        sample = self.latest()
        if not sample:
            return "No data."
        lines = ["=== Current OBD Snapshot ==="]
        for k, v in sample.values.items():
            unit = sample.units.get(k, "")
            val_str = f"{v:.1f}" if isinstance(v, float) else str(v)
            lines.append(f"  {k:<25} {val_str} {unit}")
        if sample.dtcs:
            lines.append(f"  Active DTCs: {', '.join(sample.dtcs)}")
        return "\n".join(lines)

    def stats_summary(self) -> dict:
        """
        Compute min/max/avg for each numeric PID over the full buffer.
        Useful for health summaries.
        """
        from collections import defaultdict
        totals: dict[str, list[float]] = defaultdict(list)

        with self._lock:
            samples = list(self._buf)

        for s in samples:
            for k, v in s.values.items():
                if isinstance(v, (int, float)) and v is not None:
                    totals[k].append(float(v))

        result = {}
        for k, vals in totals.items():
            result[k] = {
                "min": round(min(vals), 2),
                "max": round(max(vals), 2),
                "avg": round(sum(vals) / len(vals), 2),
                "samples": len(vals),
            }
        return result
