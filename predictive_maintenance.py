"""
Predictive Maintenance
=======================
Reads structured events out of scanner.db (see database.py) and derives
early-warning signals:

  1. Anomaly frequency drift    — per-PID, are threshold breaches happening
                                  more often now than they used to?
  2. DTC recurrence             — which fault codes keep coming back
                                  (set → cleared → set again)?
  3. Brake wear trend           — is peak deceleration drifting up or down
                                  across recent stops?
  4. Voltage slide              — is CONTROL_MODULE_VOLTAGE tripping low-voltage
                                  alerts more than it used to?
  5. Fuel-trim drift            — are SHORT/LONG_FUEL_TRIM_1 anomalies
                                  accelerating (vacuum leak, injector wear)?
  6. Vehicle weak-point         — static reminders from the vehicle profile,
     reminders                    each cross-referenced against recent
                                  evidence in the DB so we can prioritise.

Design notes
------------
• This module is **event-level only**. Raw sample telemetry lives in the CSV
  files and is too big to load here. All signals are derived from events the
  scanner has already classified as interesting (anomaly_events, dtc_events,
  brake_events).

• All queries go through Database.anomalies_by_pid / recent_anomalies /
  recent_dtcs / recent_brake_events. We do NOT reach past the Database API.

• Pure stdlib — no numpy, no pandas, no ML models. Trend detection is
  simple rate comparison ("recent X days rate" vs "baseline window rate").
  This keeps the analysis explainable and cheap; the LLM later does the
  plain-language framing.

• Every method is safe to call when the DB has no data. In that case the
  method returns an empty list / a "no_data" dict.

Typical usage
-------------
    from database import Database
    from predictive_maintenance import PredictiveMaintenance

    db = Database(); db.open()
    pm = PredictiveMaintenance(db)
    report = pm.generate_report()
    # report is a structured dict; pretty-print, ship to the dashboard,
    # or pass to LLMInterface.analyze_predictive_report(report) for a
    # plain-English summary.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from database import Database
from vehicle_profile import VEHICLE_INFO

logger = logging.getLogger(__name__)


# ── Tuning knobs ──────────────────────────────────────────────────────────────
# These are chosen so the signals mean something on a car that's driven a
# few hours per week and has been logging for at least a couple of weeks.

DEFAULT_RECENT_DAYS            = 14          # "recent" trailing window
DEFAULT_BASELINE_DAYS          = 90          # baseline window (ends where recent begins)
MIN_RECENT_EVENTS_FOR_TREND    = 3           # ignore PIDs with only 1–2 recent hits
RISING_RATIO                   = 1.5         # recent_rate / baseline_rate to flag "rising"
NEW_PID_DAYS                   = 30          # "brand new" means no events across this many days before
BRAKE_TREND_MIN_EVENTS         = 10          # need at least this many brake events in each window
BRAKE_PEAK_G_RISING_DELTA      = 0.05        # > 0.05g rise in avg peak-g = notable
DTC_RECURRENCE_MIN_APPEARANCES = 2           # seen at least twice to be "recurring"
DAY_SECONDS                    = 86400.0


# ── Severity vocabulary ───────────────────────────────────────────────────────
# Ordered from least to most urgent. Used throughout for consistent comparisons.

SEVERITY_ORDER = {"info": 0, "watch": 1, "warn": 2, "urgent": 3}


def _max_severity(*severities: str) -> str:
    return max(severities, key=lambda s: SEVERITY_ORDER.get(s, 0))


# ── Known weak-point signatures for the vehicle ──────────────────────────────
# Ties each item in VEHICLE_INFO["known_weak_points"] to the PIDs and DTCs
# that would indicate it is starting to fail.  When we have evidence, the
# report promotes that weak point from "informational reminder" to
# "active signal".
#
# The substring match below is lenient on purpose — VEHICLE_INFO strings are
# long descriptions and we just need a unique-enough key.

WEAK_POINT_SIGNATURES: dict[str, dict[str, list[str]]] = {
    "PCV oil trap": {
        # PCV failure → vacuum leak → lean trims → P1324 on Volvo whiteblocks
        "pids": ["LONG_FUEL_TRIM_1", "SHORT_FUEL_TRIM_1"],
        "dtcs": ["P1324", "P0171", "P0174"],
    },
    "Throttle body carbon": {
        "pids": ["THROTTLE_POS", "RELATIVE_THROTTLE_POS"],
        "dtcs": ["P0506", "P0507", "P2110", "P2111", "P2112"],
    },
    "Timing belt": {
        # Interference engine. No direct PID signal — this is a pure service
        # reminder that the driver must act on by date / mileage.
        "pids": [],
        "dtcs": [],
    },
    "Turbo inlet pipe": {
        "pids": ["BOOST_PSI", "INTAKE_PRESSURE", "VOLVO_BOOST_ACTUAL", "MAF"],
        "dtcs": ["P0299", "P0234", "P0106"],
    },
    "Upper engine mount": {
        "pids": [],
        "dtcs": [],
    },
    "Coolant expansion tank cap": {
        "pids": ["COOLANT_TEMP"],
        "dtcs": ["P0217"],
    },
    "MAP sensor": {
        "pids": ["INTAKE_PRESSURE", "BOOST_PSI", "VOLVO_BOOST_ACTUAL"],
        "dtcs": ["P0105", "P0106", "P0107", "P0108", "P0109"],
    },
    "CVVT solenoid": {
        "pids": [],
        "dtcs": ["P0011", "P0014", "P0015", "P0020", "P0021"],
    },
}


# ── Data shapes ───────────────────────────────────────────────────────────────

@dataclass
class AnomalyTrend:
    """Per-PID trend signal."""
    pid: str
    recent_count: int
    baseline_count: int
    recent_rate_per_day: float
    baseline_rate_per_day: float
    ratio: Optional[float]              # None when baseline is empty
    status: str                         # "rising" | "new" | "declining" | "stable"
    recent_severity_mix: dict[str, int] # {"warn": n, "critical": n}
    severity: str                       # info / watch / warn / urgent
    note: str


@dataclass
class DTCRecurrence:
    """A DTC that has been set more than once."""
    code: str
    total_new: int
    total_cleared: int
    first_seen: float
    last_seen: float
    currently_active: bool
    severity: str
    note: str


@dataclass
class BrakeTrend:
    """Brake wear signal from decel-g drift."""
    status: str                 # "rising" | "declining" | "stable" | "no_data"
    recent_count: int
    baseline_count: int
    recent_avg_peak_g: Optional[float]
    baseline_avg_peak_g: Optional[float]
    delta_peak_g: Optional[float]
    severity: str
    note: str


@dataclass
class VoltageSlide:
    status: str                 # "rising" | "stable" | "no_data"
    recent_count: int
    baseline_count: int
    severity: str
    note: str


@dataclass
class FuelTrimDrift:
    status: str
    short_trim_recent: int
    short_trim_baseline: int
    long_trim_recent: int
    long_trim_baseline: int
    severity: str
    note: str


@dataclass
class WeakPointSignal:
    """One entry from VEHICLE_INFO[known_weak_points] enriched with DB evidence."""
    label: str                  # The raw weak-point string from VEHICLE_INFO
    key: Optional[str]          # Matching key in WEAK_POINT_SIGNATURES, or None
    evidence_dtcs: list[str]    # Which of the known-signature DTCs have fired recently
    evidence_pids: list[str]    # Which of the known-signature PIDs have fired recently
    severity: str               # info (reminder only) / watch / warn
    note: str


@dataclass
class Report:
    """Top-level predictive maintenance report."""
    generated_at: float
    generated_iso: str
    recent_days: int
    baseline_days: int
    anomaly_trends: list[AnomalyTrend] = field(default_factory=list)
    dtc_recurrence: list[DTCRecurrence] = field(default_factory=list)
    brake_trend: Optional[BrakeTrend] = None
    voltage_slide: Optional[VoltageSlide] = None
    fuel_trim_drift: Optional[FuelTrimDrift] = None
    weak_points: list[WeakPointSignal] = field(default_factory=list)
    overall_severity: str = "info"

    def to_dict(self) -> dict[str, Any]:
        # Using vars() / __dict__ gives us plain dicts for nested dataclasses
        def _d(obj):
            if obj is None:
                return None
            if hasattr(obj, "__dataclass_fields__"):
                return {k: _d(v) for k, v in obj.__dict__.items()}
            if isinstance(obj, list):
                return [_d(x) for x in obj]
            return obj
        return _d(self)


# ── Predictor ─────────────────────────────────────────────────────────────────

class PredictiveMaintenance:
    """
    Cross-session trend analysis over the SQLite event store.

    Not thread-safe by design: expected to be invoked on-demand (CLI flag,
    dashboard endpoint, or a periodic coroutine), never in the hot polling
    path.
    """

    def __init__(
        self,
        db: Database,
        vehicle_info: Optional[dict] = None,
        recent_days: int = DEFAULT_RECENT_DAYS,
        baseline_days: int = DEFAULT_BASELINE_DAYS,
        now_ts: Optional[float] = None,
    ):
        self._db = db
        self._vehicle = vehicle_info if vehicle_info is not None else VEHICLE_INFO
        self._recent_days = recent_days
        self._baseline_days = baseline_days
        # now_ts is injectable for deterministic tests.
        self._now = now_ts if now_ts is not None else time.time()
        self._recent_cutoff = self._now - recent_days * DAY_SECONDS
        self._baseline_cutoff = self._now - baseline_days * DAY_SECONDS
        # Cache of anomalies-by-PID so we don't re-query per weak point.
        self._anomaly_cache: dict[str, list[dict]] = {}

    # ── Public entry points ───────────────────────────────────────────────────

    def anomaly_drift(self) -> list[AnomalyTrend]:
        """Per-PID drift signal. Only returns PIDs with at least one recent event."""
        pid_rows = self._distinct_anomaly_pids(self._baseline_cutoff)
        trends: list[AnomalyTrend] = []
        for pid in pid_rows:
            events = self._anomalies_for_pid(pid, since=self._baseline_cutoff)
            recent = [e for e in events if e["timestamp"] >= self._recent_cutoff]
            baseline = [e for e in events if e["timestamp"] < self._recent_cutoff]
            if not recent:
                continue  # Ignore PIDs that only have old noise

            recent_rate = len(recent) / self._recent_days
            baseline_rate = (
                len(baseline) / max(self._baseline_days - self._recent_days, 1)
            )
            ratio = (recent_rate / baseline_rate) if baseline_rate > 0 else None

            sev_mix: dict[str, int] = {}
            for e in recent:
                sev_mix[e["severity"]] = sev_mix.get(e["severity"], 0) + 1

            status, severity, note = self._classify_anomaly_trend(
                pid, recent, baseline, ratio, sev_mix
            )
            trends.append(
                AnomalyTrend(
                    pid=pid,
                    recent_count=len(recent),
                    baseline_count=len(baseline),
                    recent_rate_per_day=round(recent_rate, 3),
                    baseline_rate_per_day=round(baseline_rate, 3),
                    ratio=round(ratio, 2) if ratio is not None else None,
                    status=status,
                    recent_severity_mix=sev_mix,
                    severity=severity,
                    note=note,
                )
            )
        # Most urgent first, then by recent count
        trends.sort(
            key=lambda t: (-SEVERITY_ORDER.get(t.severity, 0), -t.recent_count)
        )
        return trends

    def dtc_recurrence(self) -> list[DTCRecurrence]:
        """DTCs set more than once within the baseline window."""
        # Pull a generous batch — DTCs happen rarely so 500 rows covers many months.
        rows = self._db.recent_dtcs(limit=500)
        rows = [r for r in rows if r["timestamp"] >= self._baseline_cutoff]
        # Group by code
        by_code: dict[str, list[dict]] = {}
        for r in rows:
            by_code.setdefault(r["code"], []).append(r)

        results: list[DTCRecurrence] = []
        for code, evts in by_code.items():
            evts.sort(key=lambda r: r["timestamp"])
            news = [r for r in evts if r["state"] == "new"]
            cleared = [r for r in evts if r["state"] == "cleared"]
            if len(news) < DTC_RECURRENCE_MIN_APPEARANCES:
                continue
            # "Currently active" = last state seen was 'new' (not cleared).
            last = evts[-1]
            currently_active = last["state"] == "new"
            severity, note = self._classify_dtc_recurrence(
                code, news, cleared, currently_active
            )
            results.append(
                DTCRecurrence(
                    code=code,
                    total_new=len(news),
                    total_cleared=len(cleared),
                    first_seen=news[0]["timestamp"],
                    last_seen=last["timestamp"],
                    currently_active=currently_active,
                    severity=severity,
                    note=note,
                )
            )
        results.sort(
            key=lambda d: (-SEVERITY_ORDER.get(d.severity, 0), -d.total_new)
        )
        return results

    def brake_wear_trend(self) -> BrakeTrend:
        """Compare recent vs baseline peak-g to catch harsh-braking drift."""
        rows = self._db.recent_brake_events(limit=1000)
        rows = [r for r in rows if r["timestamp"] >= self._baseline_cutoff]
        recent = [r for r in rows if r["timestamp"] >= self._recent_cutoff]
        baseline = [r for r in rows if r["timestamp"] < self._recent_cutoff]

        if (
            len(recent) < BRAKE_TREND_MIN_EVENTS
            or len(baseline) < BRAKE_TREND_MIN_EVENTS
        ):
            return BrakeTrend(
                status="no_data",
                recent_count=len(recent),
                baseline_count=len(baseline),
                recent_avg_peak_g=None,
                baseline_avg_peak_g=None,
                delta_peak_g=None,
                severity="info",
                note=(
                    "Not enough braking events in either window to compute a "
                    f"trend (need {BRAKE_TREND_MIN_EVENTS} recent and "
                    f"{BRAKE_TREND_MIN_EVENTS} baseline)."
                ),
            )

        recent_avg = _safe_mean(r["peak_decel_g"] for r in recent)
        baseline_avg = _safe_mean(r["peak_decel_g"] for r in baseline)
        if recent_avg is None or baseline_avg is None:
            return BrakeTrend(
                status="no_data",
                recent_count=len(recent),
                baseline_count=len(baseline),
                recent_avg_peak_g=None,
                baseline_avg_peak_g=None,
                delta_peak_g=None,
                severity="info",
                note="Brake events present but peak_decel_g missing on too many rows.",
            )

        delta = recent_avg - baseline_avg
        if delta >= BRAKE_PEAK_G_RISING_DELTA:
            status = "rising"
            severity = "watch"
            note = (
                f"Average peak braking-g has risen from {baseline_avg:.2f} to "
                f"{recent_avg:.2f} ({delta:+.2f}g). That's a harsher braking "
                "pattern — could be driver behaviour, reduced pad bite, or a "
                "scheduling / heavier load change."
            )
        elif delta <= -BRAKE_PEAK_G_RISING_DELTA:
            status = "declining"
            severity = "info"
            note = (
                f"Average peak braking-g has dropped from {baseline_avg:.2f} to "
                f"{recent_avg:.2f} ({delta:+.2f}g). Gentler driving, or reduced "
                "braking effectiveness — worth noting if the car pulls or the "
                "pedal feels soft."
            )
        else:
            status = "stable"
            severity = "info"
            note = (
                f"Braking profile is steady (avg peak-g {recent_avg:.2f} vs "
                f"{baseline_avg:.2f})."
            )
        return BrakeTrend(
            status=status,
            recent_count=len(recent),
            baseline_count=len(baseline),
            recent_avg_peak_g=round(recent_avg, 3),
            baseline_avg_peak_g=round(baseline_avg, 3),
            delta_peak_g=round(delta, 3),
            severity=severity,
            note=note,
        )

    def voltage_slide(self) -> VoltageSlide:
        """Rate of CONTROL_MODULE_VOLTAGE low-voltage alerts, recent vs baseline."""
        events = self._anomalies_for_pid(
            "CONTROL_MODULE_VOLTAGE", since=self._baseline_cutoff
        )
        recent = [e for e in events if e["timestamp"] >= self._recent_cutoff]
        baseline = [e for e in events if e["timestamp"] < self._recent_cutoff]

        if not recent and not baseline:
            return VoltageSlide(
                status="no_data",
                recent_count=0,
                baseline_count=0,
                severity="info",
                note=(
                    "No low-voltage alerts recorded. Alternator and battery look "
                    "healthy from the anomaly log."
                ),
            )

        recent_rate = len(recent) / self._recent_days
        baseline_rate = (
            len(baseline) / max(self._baseline_days - self._recent_days, 1)
            if baseline else 0.0
        )
        ratio = (recent_rate / baseline_rate) if baseline_rate > 0 else None

        if len(recent) >= MIN_RECENT_EVENTS_FOR_TREND and (
            ratio is None or ratio >= RISING_RATIO
        ):
            # Any critical events in the recent window bump the severity.
            has_critical = any(e["severity"] == "critical" for e in recent)
            severity = "warn" if has_critical else "watch"
            note = (
                f"{len(recent)} low-voltage event(s) in the last {self._recent_days} "
                f"days vs {len(baseline)} before that. "
                + (
                    "Critical-level events have appeared — check the battery "
                    "state-of-health and alternator output under load."
                    if has_critical
                    else "Trending the wrong way; charging system deserves a bench test."
                )
            )
            return VoltageSlide(
                status="rising",
                recent_count=len(recent),
                baseline_count=len(baseline),
                severity=severity,
                note=note,
            )

        return VoltageSlide(
            status="stable",
            recent_count=len(recent),
            baseline_count=len(baseline),
            severity="info",
            note="Low-voltage alerts (if any) are at or below the historical rate.",
        )

    def fuel_trim_drift(self) -> FuelTrimDrift:
        """
        Combined trim signal.  Either trim trending up materially is a
        classic symptom of a developing vacuum / PCV leak or fuel-delivery
        fault — i.e. the LLM fallback cache should have a pre-written
        answer if the user has already seen this pattern once.
        """
        s_evts = self._anomalies_for_pid(
            "SHORT_FUEL_TRIM_1", since=self._baseline_cutoff
        )
        l_evts = self._anomalies_for_pid(
            "LONG_FUEL_TRIM_1", since=self._baseline_cutoff
        )
        s_recent = sum(1 for e in s_evts if e["timestamp"] >= self._recent_cutoff)
        s_base = len(s_evts) - s_recent
        l_recent = sum(1 for e in l_evts if e["timestamp"] >= self._recent_cutoff)
        l_base = len(l_evts) - l_recent

        if not (s_recent + l_recent + s_base + l_base):
            return FuelTrimDrift(
                status="no_data",
                short_trim_recent=0, short_trim_baseline=0,
                long_trim_recent=0, long_trim_baseline=0,
                severity="info",
                note="No fuel-trim threshold breaches recorded.",
            )

        # A trim event is interesting; more of them, or new in the recent
        # window, is a rising signal.
        total_recent = s_recent + l_recent
        total_base = s_base + l_base
        recent_rate = total_recent / self._recent_days
        base_rate = (
            total_base / max(self._baseline_days - self._recent_days, 1)
            if total_base else 0.0
        )
        ratio = (recent_rate / base_rate) if base_rate > 0 else None

        if total_recent >= MIN_RECENT_EVENTS_FOR_TREND and (
            ratio is None or ratio >= RISING_RATIO
        ):
            # LONG trim drift is more diagnostic than SHORT trim drift — long
            # trim moves only after the ECU has averaged many samples.
            severity = "warn" if l_recent >= MIN_RECENT_EVENTS_FOR_TREND else "watch"
            note = (
                f"Fuel-trim anomalies rising (short: {s_recent} recent / {s_base} "
                f"baseline, long: {l_recent} recent / {l_base} baseline). "
                "Classic fingerprint of a developing vacuum leak, PCV failure, "
                "injector wear, or (on this B5254T2) the MAP sensor going soft."
            )
            return FuelTrimDrift(
                status="rising",
                short_trim_recent=s_recent, short_trim_baseline=s_base,
                long_trim_recent=l_recent, long_trim_baseline=l_base,
                severity=severity, note=note,
            )

        return FuelTrimDrift(
            status="stable",
            short_trim_recent=s_recent, short_trim_baseline=s_base,
            long_trim_recent=l_recent, long_trim_baseline=l_base,
            severity="info",
            note="Fuel trims holding steady — no lean/rich drift.",
        )

    def weak_point_reminders(self) -> list[WeakPointSignal]:
        """
        Static reminders from VEHICLE_INFO, each enriched with whatever
        evidence we can find in the event log.  The driver sees every
        reminder; those with evidence are escalated to 'watch' or higher.
        """
        reminders: list[WeakPointSignal] = []
        known = self._vehicle.get("known_weak_points", [])
        recent_dtc_codes = {
            r["code"] for r in self._db.recent_dtcs(limit=500)
            if r["timestamp"] >= self._recent_cutoff and r["state"] == "new"
        }

        for label in known:
            key = self._match_weak_point(label)
            sig = WEAK_POINT_SIGNATURES.get(key, {}) if key else {}
            dtc_sigs: list[str] = sig.get("dtcs", []) if sig else []
            pid_sigs: list[str] = sig.get("pids", []) if sig else []

            evidence_dtcs = [c for c in dtc_sigs if c in recent_dtc_codes]
            evidence_pids: list[str] = []
            for pid in pid_sigs:
                recent_events = [
                    e for e in self._anomalies_for_pid(pid, since=self._recent_cutoff)
                ]
                if recent_events:
                    evidence_pids.append(pid)

            severity, note = self._classify_weak_point(
                label, evidence_dtcs, evidence_pids
            )
            reminders.append(
                WeakPointSignal(
                    label=label,
                    key=key,
                    evidence_dtcs=evidence_dtcs,
                    evidence_pids=evidence_pids,
                    severity=severity,
                    note=note,
                )
            )
        reminders.sort(key=lambda r: -SEVERITY_ORDER.get(r.severity, 0))
        return reminders

    def generate_report(self) -> Report:
        """Assemble the full report with overall severity summarised."""
        anomaly_trends = self.anomaly_drift()
        dtc_rec = self.dtc_recurrence()
        brake = self.brake_wear_trend()
        voltage = self.voltage_slide()
        fuel = self.fuel_trim_drift()
        weak = self.weak_point_reminders()

        severities = (
            [t.severity for t in anomaly_trends]
            + [d.severity for d in dtc_rec]
            + [brake.severity, voltage.severity, fuel.severity]
            + [w.severity for w in weak]
        )
        overall = _max_severity(*severities) if severities else "info"

        return Report(
            generated_at=self._now,
            generated_iso=time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(self._now)
            ),
            recent_days=self._recent_days,
            baseline_days=self._baseline_days,
            anomaly_trends=anomaly_trends,
            dtc_recurrence=dtc_rec,
            brake_trend=brake,
            voltage_slide=voltage,
            fuel_trim_drift=fuel,
            weak_points=weak,
            overall_severity=overall,
        )

    # ── Classification helpers ────────────────────────────────────────────────

    def _classify_anomaly_trend(
        self,
        pid: str,
        recent: list[dict],
        baseline: list[dict],
        ratio: Optional[float],
        sev_mix: dict[str, int],
    ) -> tuple[str, str, str]:
        """Decide status + severity label + short note for one PID's trend."""
        has_critical = sev_mix.get("critical", 0) > 0
        if not baseline and len(recent) >= MIN_RECENT_EVENTS_FOR_TREND:
            severity = "warn" if has_critical else "watch"
            note = (
                f"{pid} started tripping anomalies this window ({len(recent)} "
                f"in {self._recent_days} days) with no prior history — "
                "investigate as a new developing issue."
            )
            return "new", severity, note

        if ratio is not None and ratio >= RISING_RATIO and len(recent) >= MIN_RECENT_EVENTS_FOR_TREND:
            if has_critical:
                severity = "urgent" if ratio >= 3.0 else "warn"
            else:
                severity = "warn" if ratio >= 3.0 else "watch"
            note = (
                f"{pid} anomaly rate is {ratio:.1f}× the baseline "
                f"({round(len(recent)/self._recent_days, 2)}/day vs "
                f"{round(len(baseline)/max(self._baseline_days - self._recent_days, 1), 2)}/day)."
            )
            return "rising", severity, note

        if ratio is not None and ratio <= 0.5 and len(recent) > 0:
            return (
                "declining", "info",
                f"{pid} anomalies are less frequent than before ({ratio:.1f}×)."
            )

        return (
            "stable", "info",
            f"{pid} anomaly rate is close to baseline."
        )

    def _classify_dtc_recurrence(
        self,
        code: str,
        news: list[dict],
        cleared: list[dict],
        active: bool,
    ) -> tuple[str, str]:
        """
        Severity rules:
          - currently active AND previously cleared at least once → 'warn'
          - seen 3+ times → 'warn'
          - seen 4+ times or active+high count → 'urgent'
          - otherwise → 'watch'
        """
        n = len(news)
        if active and cleared:
            if n >= 4:
                severity = "urgent"
            else:
                severity = "warn"
            note = (
                f"{code} is currently set again after being cleared "
                f"{len(cleared)}× — returning faults are rarely random. "
                "Permanent repair likely required."
            )
        elif n >= 4:
            severity = "urgent"
            note = f"{code} has appeared {n} times in the baseline window."
        elif n >= 3:
            severity = "warn"
            note = f"{code} has appeared {n} times — intermittent but repeating."
        else:
            severity = "watch"
            note = f"{code} seen {n} times; worth keeping an eye on."
        return severity, note

    def _classify_weak_point(
        self,
        label: str,
        evidence_dtcs: list[str],
        evidence_pids: list[str],
    ) -> tuple[str, str]:
        if evidence_dtcs:
            return (
                "warn",
                f"Active evidence in the DTC log: {', '.join(evidence_dtcs)}. "
                f"Known weak point \"{label}\" may be failing."
            )
        if evidence_pids:
            return (
                "watch",
                f"Recent PID anomalies ({', '.join(evidence_pids)}) align with "
                f"known weak point \"{label}\". Monitor closely."
            )
        return ("info", f"Service reminder — known weak point: {label}")

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _anomalies_for_pid(self, pid: str, since: float) -> list[dict]:
        """Cached pid-scoped anomaly fetch within the baseline window."""
        cache_key = f"{pid}@{since}"
        if cache_key in self._anomaly_cache:
            return self._anomaly_cache[cache_key]
        rows = self._db.anomalies_by_pid(pid, since=since, limit=5000)
        self._anomaly_cache[cache_key] = rows
        return rows

    def _distinct_anomaly_pids(self, since: float) -> list[str]:
        """Which PIDs have anomaly events within the baseline window?"""
        rows = self._db.recent_anomalies(limit=5000)
        return sorted({r["pid_name"] for r in rows if r["timestamp"] >= since})

    def _match_weak_point(self, label: str) -> Optional[str]:
        """Map a VEHICLE_INFO weak-point string to a WEAK_POINT_SIGNATURES key."""
        lower = label.lower()
        for key in WEAK_POINT_SIGNATURES:
            if key.lower() in lower:
                return key
        return None


# ── Utilities ─────────────────────────────────────────────────────────────────

def _safe_mean(values) -> Optional[float]:
    """Mean of an iterable, ignoring None/NaN, returning None if empty."""
    total, count = 0.0, 0
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f):
            continue
        total += f
        count += 1
    return total / count if count else None


# ── Human-friendly formatter (optional helper) ────────────────────────────────

def format_report_text(report: Report) -> str:
    """
    Convert a Report into a compact multi-section text block suitable for
    pretty-printing in a terminal or emailing.  The LLM narrative pass also
    accepts this as its input.
    """
    lines: list[str] = []
    lines.append(
        f"Predictive Maintenance Report  —  {report.generated_iso}"
    )
    lines.append(
        f"  Windows: recent {report.recent_days}d, baseline {report.baseline_days}d"
    )
    lines.append(f"  Overall severity: {report.overall_severity.upper()}")
    lines.append("")

    if report.anomaly_trends:
        lines.append("Anomaly-rate trends:")
        for t in report.anomaly_trends:
            lines.append(
                f"  [{t.severity:>5}] {t.pid:<26s} status={t.status:<10s} "
                f"recent={t.recent_count:>3d} baseline={t.baseline_count:>3d}"
                + (f" ratio={t.ratio:.2f}×" if t.ratio is not None else "")
            )
            lines.append(f"          └─ {t.note}")
    else:
        lines.append("Anomaly-rate trends: none notable.")
    lines.append("")

    if report.dtc_recurrence:
        lines.append("DTC recurrence:")
        for d in report.dtc_recurrence:
            lines.append(
                f"  [{d.severity:>5}] {d.code}  set×{d.total_new} / cleared×{d.total_cleared}"
                f"  {'ACTIVE' if d.currently_active else 'cleared'}"
            )
            lines.append(f"          └─ {d.note}")
    else:
        lines.append("DTC recurrence: none.")
    lines.append("")

    if report.brake_trend:
        lines.append(
            f"Brake wear: [{report.brake_trend.severity:>5}] status={report.brake_trend.status}"
        )
        lines.append(f"          └─ {report.brake_trend.note}")
    if report.voltage_slide:
        lines.append(
            f"Voltage slide: [{report.voltage_slide.severity:>5}] status={report.voltage_slide.status}"
        )
        lines.append(f"          └─ {report.voltage_slide.note}")
    if report.fuel_trim_drift:
        lines.append(
            f"Fuel-trim drift: [{report.fuel_trim_drift.severity:>5}] status={report.fuel_trim_drift.status}"
        )
        lines.append(f"          └─ {report.fuel_trim_drift.note}")
    lines.append("")

    if report.weak_points:
        lines.append("Vehicle weak-point reminders:")
        for w in report.weak_points:
            tag = f"[{w.severity:>5}]"
            lines.append(f"  {tag} {w.label}")
            if w.evidence_dtcs or w.evidence_pids:
                lines.append(
                    "          └─ "
                    + (f"DTC: {', '.join(w.evidence_dtcs)}  " if w.evidence_dtcs else "")
                    + (f"PIDs: {', '.join(w.evidence_pids)}" if w.evidence_pids else "")
                )
    return "\n".join(lines)
