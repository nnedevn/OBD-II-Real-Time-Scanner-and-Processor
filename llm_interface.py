"""
LLM Interface — IBM Granite via Ollama
========================================
All communication with the local Granite model goes through this module.

It provides three high-level entry points:
  - analyze_telemetry()   — periodic health summary from a data window
  - analyze_anomaly()     — immediate alert explanation for a threshold breach
  - analyze_dtc()         — full diagnostic + repair guide for a fault code

Each method builds a structured prompt, calls Ollama, and streams the
response back via a callback (or returns it as a string).

Granite system prompt strategy
--------------------------------
Granite 3.x performs best with a clear system role + structured user prompt.
We use a single persistent system prompt that establishes the assistant as
an expert automotive diagnostic AI, and vary only the user prompt per call.
"""

import json
import logging
import time
from typing import TYPE_CHECKING, Callable, Generator, Optional

import requests

import config
import dtc_dictionary
from vehicle_profile import get_llm_system_prompt_extension

if TYPE_CHECKING:
    # Type-only import — avoids a runtime dep, since LLMInterface works fine
    # with db=None (fallbacks just shrink to "static-only").
    from database import Database


# ── Circuit Breaker Tuning ────────────────────────────────────────────────────
# Fail this many consecutive LLM calls before opening the breaker.
# Low number = fail fast; high number = tolerate flaky links.
CIRCUIT_FAILURE_THRESHOLD = 3
# After opening, stay open this many seconds before a probe call is allowed.
# One minute balances 'don't spam a down service' against 'recover quickly
# when it comes back.'
CIRCUIT_RECOVERY_SECONDS = 60.0


# ── Failure sentinel detection ────────────────────────────────────────────────

def _is_failure_response(response: str) -> bool:
    """
    GraniteClient.chat() never raises — it returns a string starting with
    '⚠️  LLM offline' or '⚠️  LLM error' on failure. This helper gives us
    a single source of truth for detecting that.
    """
    if not response:
        return False   # empty means "rate-limit suppressed" not "failure"
    return response.startswith("⚠️  LLM offline") or response.startswith("⚠️  LLM error")

logger = logging.getLogger(__name__)

# ── System Prompt ─────────────────────────────────────────────────────────────
# Injected once per conversation. Establishes tone, domain, and output format.
# The vehicle profile extension appends make/model/engine context + known weak points.

SYSTEM_PROMPT = """You are an expert automotive diagnostic AI assistant embedded in a real-time OBD-II scanner. Your role is to interpret live engine telemetry and fault codes from a vehicle's ECU.

You have deep expertise in:
- OBD-II PIDs and what their values indicate about engine health
- DTC (Diagnostic Trouble Code) interpretation across all OBD-II compliant vehicles
- Mechanical and electrical fault diagnosis
- Repair procedures, torque specs, and part identification
- Cost estimation for DIY vs professional repair

Your response style:
- Be concise and direct — the driver may be watching this in real time
- Use plain language, but include technical terms where they add precision
- When describing faults: state severity (informational / warning / critical), explain the root cause, and give actionable next steps
- For repair guides: provide step-by-step instructions, list required parts with common part numbers where possible, estimate difficulty (beginner/intermediate/advanced), and flag any safety precautions
- Never speculate beyond what the data supports — if more diagnosis is needed, say so clearly
""" + get_llm_system_prompt_extension()


# ── Prompt Templates ──────────────────────────────────────────────────────────

def _build_telemetry_prompt(telemetry_text: str, stats: dict) -> str:
    stats_lines = "\n".join(
        f"  {pid}: min={s['min']}, max={s['max']}, avg={s['avg']}"
        for pid, s in stats.items()
        if pid in ("RPM", "COOLANT_TEMP", "ENGINE_LOAD", "SPEED", "MAF",
                   "SHORT_FUEL_TRIM_1", "LONG_FUEL_TRIM_1")
    )
    return f"""Analyze the following OBD-II telemetry window and provide a brief engine health summary.

{telemetry_text}

Rolling stats (full buffer):
{stats_lines or '  (not yet available)'}

Respond with:
1. Overall engine status (one of: ✅ Normal | ⚠️ Attention needed | 🚨 Action required)
2. Key observations (2–4 bullet points on notable readings)
3. Any recommended actions (if none, say "No action needed")

Keep the summary under 150 words."""


def _build_anomaly_prompt(
    pid_name: str,
    value: float,
    unit: str,
    severity: str,
    snapshot: str,
    threshold_warn: Optional[float],
    threshold_critical: Optional[float],
) -> str:
    return f"""🚨 ANOMALY DETECTED

Parameter:  {pid_name}
Value:      {value} {unit}
Severity:   {severity.upper()}
Warn threshold:     {threshold_warn} {unit}
Critical threshold: {threshold_critical} {unit}

Current vehicle snapshot:
{snapshot}

Please:
1. Explain what this reading means and why it's concerning
2. List the most likely root causes (ordered by probability)
3. Describe the immediate risk to the vehicle if driving continues
4. State what the driver should do RIGHT NOW
5. Outline the diagnostic/repair steps

Be direct — this is a live alert."""


def _build_dtc_prompt(dtc_code: str, snapshot: str) -> str:
    return f"""A new Diagnostic Trouble Code has been detected:

DTC Code: {dtc_code}

Current vehicle telemetry at time of detection:
{snapshot}

Provide a complete diagnostic report:

## Code Definition
What this code means in plain language.

## Symptoms
What the driver may notice (if anything).

## Root Causes
List the most common causes ranked by likelihood, including:
- Sensor failures
- Wiring/connector issues
- Mechanical failures
- Software/calibration issues

## Diagnostic Steps
Step-by-step procedure to pinpoint the exact cause. Include:
- Which components to inspect first
- Any live data PIDs to watch
- Expected values vs what was observed

## Repair Procedure
Step-by-step repair instructions for the most likely cause. Include:
- Required tools
- Parts needed (with common OEM and aftermarket part numbers if known)
- Estimated repair difficulty: Beginner / Intermediate / Advanced
- Estimated time
- Any safety precautions (disconnect battery, depressurize fuel system, etc.)

## Cost Estimate
- DIY parts cost range
- Professional labor estimate (hours + typical shop rate)

## Can I drive?
Clear statement on whether it's safe to continue driving and for how far/long."""


def _build_brake_health_prompt(trend_text: str) -> str:
    return f"""Analyze the following braking performance data collected from the vehicle's OBD-II speed sensor and brake light switch. This data is being used as a proxy to assess brake system health — specifically to detect whether brake fade or a need for brake fluid bleeding may be developing over time.

{trend_text}

Provide a brake system health assessment covering:

## Braking Performance Summary
Interpret the deceleration figures. For context:
- Panic stop (dry): ~1.0 g
- Normal hard braking: 0.6–0.8 g
- Moderate braking: 0.3–0.5 g
- Light braking: 0.1–0.25 g
State whether the recorded values are consistent with a healthy brake system.

## Trend Analysis
Comment on whether the trend across the recorded windows (recent / medium / long) suggests degradation. A 10%+ drop in peak deceleration over comparable entry speeds is a meaningful indicator.

## Bleeding Diagnosis
Based on the trend data, assess the likelihood that brake fluid bleeding is indicated:
- **Bleeding likely needed** — if there is a consistent, sustained decline in peak g that cannot be attributed to entry speed variation
- **Monitor closely** — if there is a moderate decline or inconsistent pattern
- **System appears healthy** — if deceleration is stable or within normal variation

Note: This is a behavioral estimate only. Low g values alone may also indicate worn pads, rotor glazing, or driver technique. Brake fluid condition cannot be confirmed without a moisture/boiling-point test.

## Recommended Next Steps
Give 2–4 concrete actions the driver should take, ordered by urgency. If bleeding is indicated, note that bleeding the 2012 Volvo C30 T5 requires a pressure bleeder or a helper, starting at the caliper furthest from the master cylinder (right rear), and that Volvo specifies DOT 4+ fluid (e.g. Pentosin Super DOT4 or ATE SL.6).

Keep the response under 250 words."""


def _build_predictive_report_prompt(report: dict) -> str:
    """
    Turn a predictive_maintenance.Report.to_dict() payload into a prompt the
    LLM can narrate in plain English.  We feed the LLM a compact JSON-shaped
    summary rather than prose, so the model can reason directly over counts
    and ratios without us pre-digesting them.
    """
    import json as _json

    # Keep only the fields the LLM can act on — skip noise like timestamps
    # and zeroed counters.  The JSON blob keeps the structure compact but
    # preserves every signal that matters.
    trimmed: dict[str, Any] = {
        "recent_window_days": report.get("recent_days"),
        "baseline_window_days": report.get("baseline_days"),
        "overall_severity": report.get("overall_severity"),
    }
    anomaly = [
        {
            "pid": t["pid"],
            "status": t["status"],
            "severity": t["severity"],
            "recent_count": t["recent_count"],
            "baseline_count": t["baseline_count"],
            "ratio_vs_baseline": t["ratio"],
            "severity_mix": t["recent_severity_mix"],
            "note": t["note"],
        }
        for t in (report.get("anomaly_trends") or [])
    ]
    if anomaly:
        trimmed["anomaly_trends"] = anomaly

    dtcs = [
        {
            "code": d["code"],
            "set_count": d["total_new"],
            "cleared_count": d["total_cleared"],
            "currently_active": d["currently_active"],
            "severity": d["severity"],
            "note": d["note"],
        }
        for d in (report.get("dtc_recurrence") or [])
    ]
    if dtcs:
        trimmed["dtc_recurrence"] = dtcs

    for k in ("brake_trend", "voltage_slide", "fuel_trim_drift"):
        v = report.get(k)
        if v and v.get("status") != "no_data":
            trimmed[k] = v

    weak = [
        {
            "label": w["label"],
            "severity": w["severity"],
            "evidence_dtcs": w["evidence_dtcs"],
            "evidence_pids": w["evidence_pids"],
        }
        for w in (report.get("weak_points") or [])
        if w["severity"] in ("watch", "warn", "urgent")
        or (w["evidence_dtcs"] or w["evidence_pids"])
    ]
    if weak:
        trimmed["weak_points_with_evidence"] = weak

    return f"""A cross-session predictive maintenance report has been compiled
from the vehicle's event history. Narrate it for the driver in plain English.

Report (structured):
```json
{_json.dumps(trimmed, indent=2, default=str)}
```

Produce a response in this exact shape:

## Bottom line
One sentence describing how the car is doing overall, using the overall_severity
as an anchor but allowing nuance.

## What to watch
Pick the 2–4 most material findings (highest severity first). For each, explain
in one short paragraph what it means mechanically and why it matters — tie it to
known B5254T2 weak points where relevant.

## What to do now
A prioritised action list. Each item: concrete action + urgency label
(THIS WEEK / NEXT MONTH / AT NEXT SERVICE). Skip anything marked 'info'
unless it genuinely warrants mention.

## What looked fine
One short line noting anything that *was* tested and found steady, so the
driver knows the green lights too.

Be direct, avoid hedging, stay under 350 words. Do not invent findings that
aren't in the structured report."""


def _build_natural_language_prompt(question: str, snapshot: str, stats: dict) -> str:
    stats_lines = "\n".join(
        f"  {pid}: min={s['min']}, max={s['max']}, avg={s['avg']}"
        for pid, s in stats.items()
    )
    return f"""The driver is asking a question about their vehicle. Answer it using the live telemetry data below.

Driver's question: "{question}"

Current telemetry snapshot:
{snapshot}

Session statistics:
{stats_lines or '  (not yet available)'}

Answer conversationally and accurately. If the data doesn't contain enough information to answer definitively, say so and suggest what additional checks would help."""


# ── Ollama Client ─────────────────────────────────────────────────────────────

class GraniteClient:
    """
    Thin wrapper around the Ollama REST API for IBM Granite.

    Supports both streaming and non-streaming responses.
    Falls back gracefully if Ollama is not running.
    """

    def __init__(self):
        self.base_url = config.OLLAMA_BASE_URL
        self.model = config.LLM_MODEL
        self._available = None  # Cached availability check

    def is_available(self) -> bool:
        """Check if Ollama is running and the model is loaded."""
        if self._available is not None:
            return self._available
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", [])]
                self._available = any(self.model in m for m in models)
                if not self._available:
                    logger.warning(
                        f"Model '{self.model}' not found in Ollama. "
                        f"Run: ollama pull {self.model}"
                    )
                return self._available
        except requests.exceptions.ConnectionError:
            logger.error(
                "Cannot reach Ollama. Is it running? Start with: ollama serve"
            )
        self._available = False
        return False

    def chat(
        self,
        user_prompt: str,
        stream_callback: Optional[Callable[[str], None]] = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> str:
        """
        Send a prompt to Granite and return the full response.

        If stream_callback is provided, tokens are passed to it as they arrive
        (and the full text is still returned at the end).
        """
        self._available = None  # Reset cache on each call to re-check

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {
                "temperature": config.LLM_TEMPERATURE,
                "num_predict": config.LLM_MAX_TOKENS,
            },
            "stream": stream_callback is not None,
        }

        try:
            if stream_callback is not None:
                return self._stream_chat(payload, stream_callback)
            else:
                return self._blocking_chat(payload)
        except requests.exceptions.ConnectionError:
            msg = "⚠️  LLM offline — Ollama is not reachable."
            logger.error(msg)
            return msg
        except Exception as e:
            msg = f"⚠️  LLM error: {e}"
            logger.error(msg)
            return msg

    def _blocking_chat(self, payload: dict) -> str:
        payload["stream"] = False
        r = requests.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=120,
        )
        r.raise_for_status()
        return r.json()["message"]["content"]

    def _stream_chat(
        self, payload: dict, callback: Callable[[str], None]
    ) -> str:
        payload["stream"] = True
        full_text = []
        with requests.post(
            f"{self.base_url}/api/chat",
            json=payload,
            stream=True,
            timeout=120,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line:
                    chunk = json.loads(line)
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        full_text.append(token)
                        callback(token)
                    if chunk.get("done"):
                        break
        return "".join(full_text)


# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Three-state breaker for the LLM endpoint.

    States:
      CLOSED    — healthy, all calls pass through to the LLM.
      OPEN      — failing, all calls skip the LLM and go straight to fallback.
      HALF_OPEN — after CIRCUIT_RECOVERY_SECONDS, allow one probe call through;
                  on success it closes, on failure it reopens.

    This prevents piling up blocked analyses when Ollama is down: after three
    consecutive failures we stop trying for a minute, serve cached / static
    responses, and then probe once to see if the service came back.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        recovery_seconds: float = CIRCUIT_RECOVERY_SECONDS,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.state: str = self.CLOSED
        self.failure_count: int = 0
        self.opened_at: Optional[float] = None

    def can_call(self) -> bool:
        """Return True if the caller should attempt a real LLM call."""
        if self.state == self.CLOSED:
            return True
        if self.state == self.OPEN:
            if (
                self.opened_at is not None
                and (time.time() - self.opened_at) >= self.recovery_seconds
            ):
                self.state = self.HALF_OPEN
                logger.info(
                    "Circuit breaker: OPEN → HALF_OPEN (probing LLM availability)"
                )
                return True
            return False
        return True  # HALF_OPEN — allow the probe call

    def record_success(self) -> None:
        if self.state != self.CLOSED:
            logger.info(f"Circuit breaker: {self.state} → CLOSED (LLM recovered)")
        self.state = self.CLOSED
        self.failure_count = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failure_count += 1
        # Any failure in HALF_OPEN state re-opens immediately.
        # Otherwise we need to hit failure_threshold.
        should_open = (
            self.state == self.HALF_OPEN
            or self.failure_count >= self.failure_threshold
        )
        if should_open and self.state != self.OPEN:
            logger.warning(
                f"Circuit breaker: OPEN (consecutive failures={self.failure_count}); "
                f"LLM calls will be suppressed for {self.recovery_seconds:.0f}s"
            )
            self.state = self.OPEN
            self.opened_at = time.time()


# ── High-Level Interface ──────────────────────────────────────────────────────

class LLMInterface:
    """
    High-level interface used by the main orchestration loop.

    Wraps GraniteClient with:
      - Domain-specific prompt methods (analyze_anomaly, analyze_dtc, ...).
      - Rate limiting to prevent hammering the LLM on every sensor tick.
      - A circuit breaker + cache-from-DB + static DTC fallback for resilience:
        if Ollama is unreachable the scanner keeps producing useful output
        instead of blank panels.

    Resilience behaviour per analysis type:
      DTC       → static J2012 dictionary + most recent cached LLM analysis
                  of the same code (from the database).
      Anomaly   → most recent cached LLM analysis of the same (PID, severity)
                  pair (from the database).
      Summary / brake-health / Q&A → no fallback (too context-dependent to
                  cache meaningfully); caller receives a clear degraded message.
    """

    def __init__(self, db: Optional["Database"] = None):
        self._client = GraniteClient()
        self._db = db                          # May be None — fallbacks degrade gracefully
        self._breaker = CircuitBreaker()
        self._last_summary_time: float = 0
        self._last_anomaly_times: dict[str, float] = {}
        self._anomaly_cooldown = 30.0          # Seconds between repeated anomaly alerts

    # ── Public analysis methods ───────────────────────────────────────────────

    def analyze_telemetry(
        self,
        telemetry_text: str,
        stats: dict,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Periodic health summary. Called every LLM_PERIODIC_SUMMARY_INTERVAL seconds."""
        now = time.time()
        if now - self._last_summary_time < config.LLM_PERIODIC_SUMMARY_INTERVAL:
            return ""  # Not time yet
        self._last_summary_time = now

        prompt = _build_telemetry_prompt(telemetry_text, stats)
        logger.debug("Sending telemetry to LLM for analysis...")
        return self._resilient_chat(
            prompt=prompt,
            stream_callback=stream_callback,
            fallback_provider=lambda: None,  # Too context-specific to cache
        )

    def analyze_anomaly(
        self,
        pid_name: str,
        value: float,
        unit: str,
        severity: str,
        snapshot: str,
        threshold_warn: Optional[float] = None,
        threshold_critical: Optional[float] = None,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Triggered immediately when a threshold is breached. Has per-PID cooldown."""
        now = time.time()
        last = self._last_anomaly_times.get(pid_name, 0)
        if now - last < self._anomaly_cooldown:
            return ""  # Suppress repeated alerts for same PID

        self._last_anomaly_times[pid_name] = now
        prompt = _build_anomaly_prompt(
            pid_name, value, unit, severity, snapshot,
            threshold_warn, threshold_critical
        )
        logger.warning(f"Anomaly: {pid_name}={value}{unit} ({severity}). Querying LLM...")
        return self._resilient_chat(
            prompt=prompt,
            stream_callback=stream_callback,
            fallback_provider=lambda: self._fallback_anomaly(pid_name, severity),
        )

    def analyze_dtc(
        self,
        dtc_code: str,
        snapshot: str,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Full diagnostic + repair guide for a newly detected DTC."""
        prompt = _build_dtc_prompt(dtc_code, snapshot)
        logger.warning(f"New DTC detected: {dtc_code}. Querying LLM for diagnosis...")
        return self._resilient_chat(
            prompt=prompt,
            stream_callback=stream_callback,
            fallback_provider=lambda: self._fallback_dtc(dtc_code),
        )

    def answer_question(
        self,
        question: str,
        snapshot: str,
        stats: dict,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Answer a natural language question about the vehicle using live data."""
        prompt = _build_natural_language_prompt(question, snapshot, stats)
        return self._resilient_chat(
            prompt=prompt,
            stream_callback=stream_callback,
            fallback_provider=lambda: None,  # Too dynamic to cache
        )

    def analyze_brake_health(
        self,
        trend_text: str,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        On-demand brake system health analysis.

        Call this with the output of BrakeMonitor.format_for_llm() after the
        user explicitly requests a brake check, or automatically once the
        monitor has accumulated enough events (e.g. >= 10) and a declining
        trend has been flagged.

        Not rate-limited here — callers are responsible for deciding when to
        trigger this (it is relatively expensive at 250 tokens).
        """
        if not trend_text or "No braking events" in trend_text:
            return (
                "Not enough braking event data has been collected yet to perform "
                "a brake health analysis. Drive the vehicle normally for a while "
                "so the system can record sufficient braking events, then ask again."
            )
        prompt = _build_brake_health_prompt(trend_text)
        logger.info("Requesting brake health analysis from LLM...")
        return self._resilient_chat(
            prompt=prompt,
            stream_callback=stream_callback,
            fallback_provider=lambda: None,
        )

    def analyze_predictive_report(
        self,
        report: dict,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Narrate a predictive_maintenance.Report (as a dict via Report.to_dict()).

        Routes through _resilient_chat so the circuit breaker applies.  If
        the LLM is down and we have no cached narrative, the user gets the
        structured report only — the caller is expected to have already
        printed that before calling us.
        """
        if not report:
            return ""
        prompt = _build_predictive_report_prompt(report)
        logger.info("Requesting LLM narrative for predictive maintenance report...")
        return self._resilient_chat(
            prompt=prompt,
            stream_callback=stream_callback,
            # A canned fallback isn't useful here because the report contents
            # change from run to run; degrade quietly.
            fallback_provider=lambda: None,
        )

    @property
    def is_available(self) -> bool:
        return self._client.is_available()

    @property
    def circuit_state(self) -> str:
        """Exposed for the dashboard: 'closed' | 'open' | 'half_open'."""
        return self._breaker.state

    # ── Resilient call + fallback machinery ───────────────────────────────────

    def _resilient_chat(
        self,
        prompt: str,
        stream_callback: Optional[Callable[[str], None]],
        fallback_provider: Callable[[], Optional[str]],
    ) -> str:
        """
        Core resilience wrapper.

        Flow:
          1. If the breaker is OPEN and cooldown hasn't elapsed, skip the LLM
             entirely and serve the fallback (cached or static).
          2. Otherwise call Ollama once. If it responds successfully, record
             success and return the response unchanged.
          3. If the response looks like a failure sentinel
             (_is_failure_response), record the failure, potentially open the
             breaker, and attempt the fallback.
          4. If no fallback is available, return a clear degraded message so
             the caller (and the driver) knows why the panel is empty.

        Never raises — designed to be a drop-in replacement for a bare
        client.chat() call.
        """
        # 1. Breaker says no — skip the LLM, go straight to fallback
        if not self._breaker.can_call():
            logger.info("Circuit breaker OPEN — using fallback (LLM call suppressed).")
            return self._deliver_fallback(
                fallback_provider(), stream_callback,
                reason="LLM circuit breaker open",
            )

        # 2 & 3. Attempt the LLM call
        response = self._client.chat(prompt, stream_callback=stream_callback)

        if _is_failure_response(response):
            self._breaker.record_failure()
            logger.warning(
                f"LLM call failed (state={self._breaker.state}, "
                f"consecutive_failures={self._breaker.failure_count}). "
                "Attempting fallback."
            )
            return self._deliver_fallback(
                fallback_provider(), stream_callback,
                reason=response.splitlines()[0] if response else "LLM error",
            )

        # 4. Success
        self._breaker.record_success()
        return response

    def _deliver_fallback(
        self,
        fallback_text: Optional[str],
        stream_callback: Optional[Callable[[str], None]],
        reason: str,
    ) -> str:
        """Emit a fallback response (if any) through the stream callback and return it."""
        if not fallback_text:
            # No fallback available — return a clear degraded message so the
            # UI shows *something* rather than a blank panel.
            msg = (
                f"⚠️  LLM analysis unavailable ({reason}). "
                "No cached response or static entry was found for this query. "
                "The scanner will automatically retry the LLM shortly."
            )
            if stream_callback:
                stream_callback(msg)
            return msg

        # Deliver the fallback as a single chunk so the dashboard's token-accumulator
        # displays it identically to a live response.
        if stream_callback:
            stream_callback(fallback_text)
        return fallback_text

    def _fallback_dtc(self, dtc_code: str) -> Optional[str]:
        """
        DTC fallback: static J2012 entry, optionally augmented with the most
        recent cached LLM analysis of the same code from the database.

        Returns None only if dtc_dictionary somehow returns nothing (shouldn't
        happen — it always returns at least an 'unknown' record), so this
        always produces something usable.
        """
        info = dtc_dictionary.lookup(dtc_code)
        parts = [dtc_dictionary.format_for_display(info)]

        cached = self._find_cached_llm("dtc", lambda t: t.get("dtc_code") == dtc_code)
        if cached:
            parts.append(
                f"\n--- Previous LLM analysis for {dtc_code} "
                f"({cached.get('datetime', '?')}) ---\n"
                f"{cached.get('output', '')}"
            )
        return "\n".join(parts)

    def _fallback_anomaly(self, pid_name: str, severity: str) -> Optional[str]:
        """
        Anomaly fallback: find the most recent cached analysis with the same
        (pid_name, severity) pair. If nothing matches, return None and let
        the caller emit the generic degraded message.
        """
        cached = self._find_cached_llm(
            "anomaly",
            lambda t: t.get("pid_name") == pid_name and t.get("severity") == severity,
        )
        if not cached:
            return None
        return (
            f"⚠️  LLM unavailable — showing cached analysis from "
            f"{cached.get('datetime', '?')} for {pid_name} [{severity}]:\n\n"
            f"{cached.get('output', '')}"
        )

    def _find_cached_llm(
        self,
        analysis_type: str,
        match: Callable[[dict], bool],
        search_depth: int = 50,
    ) -> Optional[dict]:
        """
        Walk the last `search_depth` LLM analyses of `analysis_type` (most
        recent first) and return the first row whose trigger dict satisfies
        `match` and whose output is non-empty.
        """
        if self._db is None or not self._db.is_open:
            return None
        try:
            rows = self._db.recent_llm_analyses(
                analysis_type=analysis_type, limit=search_depth
            )
        except Exception as e:
            logger.error(f"Cache lookup failed: {e}")
            return None
        for row in rows:
            if row.get("output_empty"):
                continue
            trigger = row.get("trigger") or {}
            try:
                if match(trigger):
                    return row
            except Exception:
                continue
        return None
