"""
Anomaly Detector
=================
Rule-based pre-filter that runs on every OBD sample WITHOUT calling the LLM.
Only breaches that pass this filter trigger a (much slower) LLM call.

This keeps the hot path fast and avoids LLM rate limiting on normal data.

Design:
  - AnomalyDetector.check(sample) returns a list of AnomalyEvent objects
  - Each AnomalyEvent carries enough context to build an LLM prompt
  - Severity levels: "warn" | "critical"
  - Debouncing: a breach must persist for MIN_BREACH_SAMPLES before firing
    (avoids false positives from single noisy readings)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from obd_reader import OBDSample
import config

logger = logging.getLogger(__name__)

# How many consecutive breaching samples before we fire an anomaly event.
# At 1Hz polling this is N seconds of sustained breach.
MIN_BREACH_SAMPLES = 3


@dataclass
class AnomalyEvent:
    """Describes a single threshold breach."""
    pid_name: str
    value: float
    unit: str
    severity: str            # "warn" | "critical"
    threshold_warn: Optional[float]
    threshold_critical: Optional[float]
    consecutive_count: int   # How many samples triggered this


class AnomalyDetector:
    """
    Stateful threshold checker.

    Maintains a per-PID breach counter so we can debounce noisy readings
    and distinguish sustained faults from transient spikes.
    """

    def __init__(self):
        # {pid_name: consecutive_breach_count}
        self._breach_counts: dict[str, int] = {}
        # {pid_name: last_severity_fired} — prevents re-firing same level
        self._fired_severity: dict[str, Optional[str]] = {}

    def check(self, sample: OBDSample) -> list[AnomalyEvent]:
        """
        Check a sample against all configured thresholds.

        Returns a (possibly empty) list of AnomalyEvents that should
        be forwarded to the LLM interface.
        """
        events: list[AnomalyEvent] = []

        for pid_name, thresholds in config.THRESHOLDS.items():
            warn_limit = thresholds.get("warn")
            crit_limit = thresholds.get("critical")
            mode = thresholds.get("mode", "high")  # "high" = alert above, "low" = alert below

            # Skip if no limits configured for this PID
            if warn_limit is None and crit_limit is None:
                continue

            value = sample.values.get(pid_name)
            if value is None or not isinstance(value, (int, float)):
                # No data for this PID — reset breach counter
                self._breach_counts[pid_name] = 0
                self._fired_severity[pid_name] = None
                continue

            value = float(value)
            unit = sample.units.get(pid_name, "")

            # Determine severity of this reading
            severity = self._classify(pid_name, value, warn_limit, crit_limit, mode)

            if severity is None:
                # Back within normal range — reset state
                self._breach_counts[pid_name] = 0
                self._fired_severity[pid_name] = None
                continue

            # Increment breach counter
            self._breach_counts[pid_name] = self._breach_counts.get(pid_name, 0) + 1
            count = self._breach_counts[pid_name]

            # Only fire once per MIN_BREACH_SAMPLES, and only if severity escalated
            prev_severity = self._fired_severity.get(pid_name)
            severity_escalated = (
                prev_severity is None or
                (prev_severity == "warn" and severity == "critical")
            )

            if count >= MIN_BREACH_SAMPLES and severity_escalated:
                self._fired_severity[pid_name] = severity
                events.append(AnomalyEvent(
                    pid_name=pid_name,
                    value=value,
                    unit=unit,
                    severity=severity,
                    threshold_warn=warn_limit,
                    threshold_critical=crit_limit,
                    consecutive_count=count,
                ))
                logger.warning(
                    f"Anomaly: {pid_name}={value}{unit} "
                    f"[{severity.upper()}] for {count} consecutive samples"
                )

        return events

    @staticmethod
    def _classify(
        pid_name: str,
        value: float,
        warn: Optional[float],
        critical: Optional[float],
        mode: str = "high",
    ) -> Optional[str]:
        """
        Return "critical", "warn", or None based on thresholds.

        mode="high"  — alert when value exceeds threshold (default)
                        e.g. coolant temp too high, boost too high
        mode="low"   — alert when value drops below threshold
                        e.g. battery voltage too low, knock retard too negative
        """
        # Fuel trims: alert on absolute value (positive or negative deviation)
        if "FUEL_TRIM" in pid_name:
            value = abs(value)

        if mode == "low":
            # For low-mode PIDs, "critical" means further below the limit
            if critical is not None and value <= critical:
                return "critical"
            if warn is not None and value <= warn:
                return "warn"
        else:
            if critical is not None and value >= critical:
                return "critical"
            if warn is not None and value >= warn:
                return "warn"
        return None

    def reset(self, pid_name: str):
        """Manually reset a PID's breach state (e.g. after user acknowledges)."""
        self._breach_counts.pop(pid_name, None)
        self._fired_severity.pop(pid_name, None)

    def reset_all(self):
        self._breach_counts.clear()
        self._fired_severity.clear()

    def breach_summary(self) -> dict:
        """Return current breach counts for all PIDs (for dashboard display)."""
        return {
            pid: {
                "count": count,
                "severity": self._fired_severity.get(pid),
            }
            for pid, count in self._breach_counts.items()
            if count > 0
        }
